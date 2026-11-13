"""studio API 路由与 app 工厂（P1-6b）。

约定：
- 业务错误（ReviewError/PromptError）→ 400 带原因；资源不存在 → 404。
- 管线 run 为后台线程 + 取消事件；同项目运行中重复 run → 409。
- 进度查询走 task 端点（SSE 在 P1-6c 前端接线时按需加，先轮询）。
"""
from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.engines.base import EngineContext
from studio.engines.cache import file_fingerprint
from studio.engines.registry import EngineRegistry, EngineUnavailable
from studio.media import probe_media
from studio.models import StudioArtifact, StudioMaterial, StudioProject, StudioTask
from studio.pipeline.orchestrator import Pipeline
from studio.prompts import (
    PromptError,
    create_prompt,
    delete_prompt,
    list_prompts,
    update_prompt,
)
from studio.review_service import (
    ReviewError,
    adopt_candidate,
    get_transcript,
    list_candidates,
    reject_candidate,
    update_clips1,
)
from studio.settings import Settings


class AdoptBody(BaseModel):
    user: str
    start_ms: int | None = None
    end_ms: int | None = None
    title: str | None = None
    caption: str | None = None


class RejectBody(BaseModel):
    user: str
    reason: str


class PatchBody(BaseModel):
    user: str
    start_ms: int | None = None
    end_ms: int | None = None
    title: str | None = None
    caption: str | None = None
    hook_plan: dict | None = None
    adopted: bool | None = None


class PromptBody(BaseModel):
    name: str
    body: str
    live_type: str | None = None


class PromptPatch(BaseModel):
    name: str | None = None
    body: str | None = None
    live_type: str | None = None
    active: bool | None = None


class MaterialBody(BaseModel):
    file_path: str
    danmaku_path: str | None = None


class DraftBody(BaseModel):
    base_url: str  # capcut-mate 服务地址（用户部署，本机默认 http://127.0.0.1:30000）
    video_urls: dict[str, str] | None = None  # candidate_id → 服务可访问的视频 URL
    portrait: bool = True


class PackBody(BaseModel):
    platforms: list[str] = ["douyin"]


class EvidenceBody(BaseModel):
    platform: str | None = None


class HookEstimateBody(BaseModel):
    level: int  # 3=云生图 B-roll 4=云生视频
    provider: str | None = None  # 默认 mock_*（零费用）；cloud_* 需配置+付费开关
    candidate_ids: list[str] | None = None


class HookGenerateBody(HookEstimateBody):
    confirm: bool = False  # 必须 true 才执行（人工确认，绝不静默计费）
    max_cost: float | None = None


class FeedbackImportBody(BaseModel):
    csv_path: str


class WatchBody(BaseModel):
    directory: str


class PumpBody(BaseModel):
    interval_s: int = 300
    jitter_s: int = 60
    seed: int = 0
    dry_run: bool = True  # 真实发布 gated：dry_run=False 直接拒绝（许可证+授权未满足）


class ProjectBody(BaseModel):
    material_id: str
    name: str
    live_type: str
    intent: str | None = None
    background: str | None = None
    prompt_id: str | None = None


class RenderBody(BaseModel):
    platform: str = "douyin"
    portrait: bool = True
    subtitles: bool = True
    # P2-2/P2-3 剪辑开关
    debreath: bool = False          # 去气口三重判定
    filler_cut: bool = False        # 口头禅保守剪除（默认关）
    keep_breath_ms: int = 250       # 保留呼吸口
    cold_open: bool = True          # 原生钩子：quote 定位冷开场（定位失败自动跳过）
    cold_open_jumpcut: bool = False
    flash_forward: bool = False     # 高能闪现+whoosh
    # P2-4/P2-5 制作钩子
    l1_overlay: bool = True         # L1 文字贴片（原生钩子已用时自动不叠加）
    l2_tts: bool = False            # L2 TTS 配音钩子（原声闪避）
    l2_rank_threshold: float = 80.0
    use_gen_hooks: bool = False     # P3：接入已生成素材（mock 禁入真实渲染）
    gen_asset_ids: list[str] | None = None  # 显式勾选接入的素材；None=自动取最新非 mock


def create_app(
    settings: Settings,
    ctx: EngineContext,
    sf: sessionmaker[Session],
    registry: EngineRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="SliceStudio API", version="0.1.0")
    # 本地开发 CORS：vite dev server → studio API（仅本机回环来源）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    reg = registry or EngineRegistry(settings, ctx)
    running: dict[str, threading.Event] = {}
    lock = threading.Lock()

    def _bad_request(e: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(e))

    # ---------- 素材与项目 ----------

    @app.post("/studio/materials")
    def ingest_material(body: MaterialBody):
        from pathlib import Path

        p = Path(body.file_path)
        if not p.is_file():
            raise HTTPException(status_code=400, detail=f"文件不存在: {body.file_path}")
        if body.danmaku_path and not Path(body.danmaku_path).is_file():
            raise HTTPException(status_code=400, detail=f"弹幕文件不存在: {body.danmaku_path}")
        try:
            info = probe_media(p)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"媒体探测失败（需 ffmpeg/ffprobe）: {e}")
        with sf() as s:
            m = StudioMaterial(
                source_type="local_file", source_uri=str(p), file_path=str(p),
                fingerprint=file_fingerprint(str(p)),
                duration_ms=info["duration_ms"], width=info["width"],
                height=info["height"], fps=info["fps"],
                danmaku_path=body.danmaku_path,
            )
            s.add(m)
            s.commit()
            return {"id": m.id, "file_path": m.file_path, "duration_ms": m.duration_ms,
                    "width": m.width, "height": m.height, "fps": m.fps}

    @app.get("/studio/materials")
    def get_materials():
        with sf() as s:
            rows = s.execute(select(StudioMaterial).order_by(StudioMaterial.created_at.desc())).scalars().all()
            return [{"id": m.id, "file_path": m.file_path, "duration_ms": m.duration_ms,
                     "danmaku_path": m.danmaku_path} for m in rows]

    @app.get("/studio/projects")
    def get_projects():
        with sf() as s:
            rows = s.execute(select(StudioProject).order_by(StudioProject.created_at.desc())).scalars().all()
            return [{"id": p.id, "name": p.name, "live_type": p.live_type,
                     "status": p.status, "material_id": p.material_id} for p in rows]

    @app.post("/studio/projects")
    def create_project(body: ProjectBody):
        with sf() as s:
            if s.get(StudioMaterial, body.material_id) is None:
                raise HTTPException(status_code=400, detail=f"素材不存在: {body.material_id}")
            p = StudioProject(
                material_id=body.material_id, name=body.name, live_type=body.live_type,
                intent=body.intent, background=body.background, prompt_id=body.prompt_id,
                profile=settings.profile,
            )
            s.add(p)
            s.commit()
            return {"id": p.id, "name": p.name, "status": p.status}

    # ---------- 审核工作流 ----------

    @app.get("/studio/projects/{pid}/candidates")
    def get_candidates(pid: str, layer: str | None = None):
        try:
            return list_candidates(sf, pid, layer=layer)
        except ReviewError as e:
            raise _bad_request(e)

    @app.post("/studio/candidates/{cid}/adopt")
    def adopt(cid: str, body: AdoptBody):
        try:
            return {"clips1_id": adopt_candidate(
                sf, cid, user=body.user, start_ms=body.start_ms, end_ms=body.end_ms,
                title=body.title, caption=body.caption,
            )}
        except ReviewError as e:
            raise _bad_request(e)

    @app.post("/studio/candidates/{cid}/reject")
    def reject(cid: str, body: RejectBody):
        try:
            return {"clips1_id": reject_candidate(sf, cid, user=body.user, reason=body.reason)}
        except ReviewError as e:
            raise _bad_request(e)

    @app.patch("/studio/clips1/{cid}")
    def patch_clips1(cid: str, body: PatchBody):
        try:
            return update_clips1(
                sf, cid, user=body.user, start_ms=body.start_ms, end_ms=body.end_ms,
                title=body.title, caption=body.caption, hook_plan=body.hook_plan,
                adopted=body.adopted,
            )
        except ReviewError as e:
            raise _bad_request(e)

    @app.get("/studio/projects/{pid}/transcript")
    def transcript(pid: str):
        try:
            return get_transcript(sf, pid)
        except ReviewError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/studio/projects/{pid}/signals")
    def project_signals(pid: str):
        """审核台时间轴曲线数据源：最新 analyze 任务回执中的信号轨。"""
        with sf() as s:
            t = s.execute(
                select(StudioTask).where(
                    StudioTask.project_id == pid, StudioTask.kind == "analyze",
                ).order_by(StudioTask.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            if t is None or not t.receipt:
                return None
            return t.receipt.get("signals")

    @app.get("/studio/materials/{mid}/file")
    def material_file(mid: str):
        """素材预览（审核台源视频）：路径仅从 DB 记录取，不接受任意路径。"""
        from pathlib import Path

        from fastapi.responses import FileResponse

        with sf() as s:
            m = s.get(StudioMaterial, mid)
            if m is None:
                raise HTTPException(status_code=404, detail=f"素材不存在: {mid}")
            path = m.file_path
        if not path or not Path(path).is_file():
            raise HTTPException(status_code=404, detail=f"素材文件缺失: {path}")
        return FileResponse(path, media_type="video/mp4", filename=Path(path).name)

    @app.get("/studio/projects/{pid}/waveform")
    def project_waveform(pid: str):
        """音频振幅波形（W-1）：按素材指纹磁盘缓存，跨项目/重跑复用。"""
        import json
        from pathlib import Path

        from studio.media import waveform_peaks

        with sf() as s:
            p = s.get(StudioProject, pid)
            if p is None:
                raise HTTPException(status_code=404, detail=f"项目不存在: {pid}")
            m = s.get(StudioMaterial, p.material_id)
            if m is None or not m.file_path or not Path(m.file_path).is_file():
                raise HTTPException(status_code=404, detail="素材文件缺失")
            path, fp, dur = m.file_path, m.fingerprint, m.duration_ms or 0
        cache_dir = settings.data_dir / "waveform"
        cache_file = cache_dir / f"{fp}.json"
        if cache_file.is_file():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            data["cached"] = True
            return data
        try:
            peaks = waveform_peaks(path)
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        data = {"peaks": peaks, "buckets": len(peaks), "duration_ms": dur, "cached": False}
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    # ---------- 管线 ----------

    @app.get("/studio/projects/{pid}/task")
    def latest_task(pid: str):
        with sf() as s:
            t = s.execute(
                select(StudioTask).where(StudioTask.project_id == pid)
                .order_by(StudioTask.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            if t is None:
                return None
            return {
                "id": t.id, "kind": t.kind, "stage": t.stage, "status": t.status,
                "progress": t.progress, "receipt": t.receipt, "error": t.error,
            }

    @app.post("/studio/projects/{pid}/run")
    def run_pipeline(pid: str):
        with lock:
            if pid in running:
                raise HTTPException(status_code=409, detail="项目管线运行中")
            cancel = threading.Event()
            running[pid] = cancel

        def worker():
            try:
                Pipeline(settings, ctx, sf, registry=reg).run(pid, cancel=cancel)
            finally:
                with lock:
                    running.pop(pid, None)

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "project_id": pid}

    @app.post("/studio/projects/{pid}/cancel")
    def cancel_pipeline(pid: str):
        with lock:
            ev = running.get(pid)
        if ev is None:
            return {"cancelled": False, "reason": "无运行中任务"}
        ev.set()
        return {"cancelled": True}

    # ---------- 渲染（P2-1d：只读人工采纳的 clips1） ----------

    @app.post("/studio/projects/{pid}/render")
    def render_project(pid: str, body: RenderBody):
        with sf() as s:
            if s.get(StudioProject, pid) is None:
                raise HTTPException(status_code=404, detail=f"项目不存在: {pid}")
        with lock:
            if pid in running:
                raise HTTPException(status_code=409, detail="项目管线运行中")
            cancel = threading.Event()
            running[pid] = cancel

        def worker():
            try:
                Pipeline(settings, ctx, sf, registry=reg).render_project(
                    pid, platform=body.platform, portrait=body.portrait,
                    subtitles=body.subtitles, debreath=body.debreath,
                    filler_cut=body.filler_cut, keep_breath_ms=body.keep_breath_ms,
                    cold_open=body.cold_open, cold_open_jumpcut=body.cold_open_jumpcut,
                    flash_forward=body.flash_forward,
                    l1_overlay=body.l1_overlay, l2_tts=body.l2_tts,
                    l2_rank_threshold=body.l2_rank_threshold,
                    use_gen_hooks=body.use_gen_hooks, gen_asset_ids=body.gen_asset_ids,
                    cancel=cancel,
                )
            except ValueError as e:
                # 无采纳候选等业务错误：落任务回执供前端展示
                with sf() as s:
                    t = StudioTask(
                        project_id=pid, kind="render", stage="rendering",
                        status="failed", error=str(e),
                    )
                    s.add(t)
                    s.commit()
            finally:
                with lock:
                    running.pop(pid, None)

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "project_id": pid}

    @app.get("/studio/projects/{pid}/artifacts")
    def get_artifacts(pid: str):
        with sf() as s:
            rows = s.execute(
                select(StudioArtifact).where(StudioArtifact.project_id == pid)
                .order_by(StudioArtifact.created_at.desc())
            ).scalars().all()
            return [
                {"id": a.id, "candidate_id": a.candidate_id, "kind": a.kind,
                 "platform": a.platform, "file_path": a.file_path,
                 "size_bytes": a.size_bytes, "meta": a.meta}
                for a in rows
            ]

    # ---------- 剪映草稿（P2-6：capcut-mate Apache-2.0，独立服务对接） ----------

    @app.post("/studio/projects/{pid}/draft")
    def export_project_draft(pid: str, body: DraftBody):
        from studio.export.capcut_mate import CapCutMateClient, DraftError
        from studio.export.draft_service import export_draft

        try:
            receipt = export_draft(
                sf, pid, client=CapCutMateClient(body.base_url),
                video_urls=body.video_urls, portrait=body.portrait,
            )
            return receipt
        except DraftError as e:
            code = 502 if "不可达" in str(e) else 400
            raise HTTPException(status_code=code, detail=str(e))

    # ---------- 合规与发布（P2-7/P2-8） ----------

    @app.get("/studio/projects/{pid}/compliance")
    def compliance_report(pid: str, platform: str | None = None):
        from studio.compliance.report import build_compliance_report

        try:
            return build_compliance_report(sf, pid, platform=platform)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/studio/projects/{pid}/evidence")
    def evidence_pack(pid: str, body: EvidenceBody):
        from studio.compliance.report import write_evidence

        out_dir = settings.data_dir / "projects" / pid / "evidence"
        try:
            return write_evidence(sf, pid, out_dir, platform=body.platform)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/studio/projects/{pid}/pack")
    def publish_pack(pid: str, body: PackBody):
        from studio.export.pack import PackError, build_pack

        out_dir = settings.data_dir / "projects" / pid / "pack"
        try:
            return build_pack(sf, pid, body.platforms, out_dir)
        except PackError as e:
            raise _bad_request(e)
        except ValueError as e:
            raise _bad_request(e)

    @app.get("/studio/platforms")
    def platform_presets():
        from studio.export.platforms import PLATFORMS

        return PLATFORMS

    # ---------- 生成式钩子（P3：费用预估先行，人工确认才生成） ----------

    @app.post("/studio/projects/{pid}/hooks/estimate")
    def hooks_estimate(pid: str, body: HookEstimateBody):
        from studio.pipeline.hook_gen_service import HookGenError, estimate_hooks

        try:
            return estimate_hooks(
                sf, pid, level=body.level, provider_name=body.provider,
                candidate_ids=body.candidate_ids, ctx=ctx,
            )
        except HookGenError as e:
            raise _bad_request(e)

    @app.post("/studio/projects/{pid}/hooks/generate")
    def hooks_generate(pid: str, body: HookGenerateBody):
        from studio.pipeline.hook_gen_service import HookGenError, generate_hooks

        try:
            return generate_hooks(
                sf, settings, ctx, pid, level=body.level, provider_name=body.provider,
                candidate_ids=body.candidate_ids, confirm=body.confirm,
                max_cost=body.max_cost,
            )
        except HookGenError as e:
            raise _bad_request(e)

    # ---------- 回流对账（P3：CSV 导入 + A/B 报告） ----------

    @app.post("/studio/feedback/import")
    def feedback_import(body: FeedbackImportBody):
        from studio.feedback.reconcile import FeedbackError, import_feedback

        try:
            return import_feedback(sf, body.csv_path)
        except FeedbackError as e:
            raise _bad_request(e)

    @app.get("/studio/projects/{pid}/ab-report")
    def ab_report(pid: str):
        from studio.feedback.reconcile import FeedbackError, build_ab_report

        try:
            return build_ab_report(sf, pid)
        except FeedbackError as e:
            raise _bad_request(e)

    # ---------- P4 工厂化核心（真实发布 gated） ----------

    @app.post("/studio/factory/watch")
    def factory_watch(body: WatchBody):
        from studio.factory.watcher import ingest_stable

        state = settings.data_dir / "factory_watch_state.json"
        return ingest_stable(sf, body.directory, state)

    @app.post("/studio/projects/{pid}/gate")
    def factory_gate(pid: str):
        from studio.compliance.report import build_compliance_report
        from studio.factory.gate import gate_project

        try:
            blockers = build_compliance_report(sf, pid)["blockers"]
            c1 = list_candidates(sf, pid, layer="clips1")
            return gate_project(c1, blockers)
        except (ReviewError, ValueError) as e:
            raise _bad_request(e)

    @app.post("/studio/projects/{pid}/pump")
    def factory_pump(pid: str, body: PumpBody):
        from studio.factory.pump import PublishBlocked, plan_pump

        try:
            return plan_pump(
                sf, pid, interval_s=body.interval_s, jitter_s=body.jitter_s,
                seed=body.seed, dry_run=body.dry_run,
            )
        except PublishBlocked as e:
            raise HTTPException(status_code=403, detail=str(e))

    @app.get("/studio/battle-report")
    def factory_battle_report(project_id: str | None = None):
        from studio.factory.report import battle_report

        try:
            return battle_report(sf, project_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ---------- 产物文件服务（前端预览/下载；路径仅从 DB 记录取，不接受任意路径） ----------

    @app.get("/studio/artifacts/{aid}/file")
    def artifact_file(aid: str):
        from pathlib import Path

        from fastapi.responses import FileResponse

        with sf() as s:
            a = s.get(StudioArtifact, aid)
            if a is None:
                raise HTTPException(status_code=404, detail=f"产物不存在: {aid}")
            path, kind = a.file_path, a.kind
        p = Path(path)
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"产物文件缺失: {path}")
        media = {
            "mp4": "video/mp4", "cover": "image/jpeg",
            "publish_pack": "application/zip", "evidence": "application/json",
        }.get(kind, "application/octet-stream")
        return FileResponse(p, media_type=media, filename=p.name)

    # ---------- 提示词资源 ----------

    @app.get("/studio/prompts")
    def get_prompts(live_type: str | None = None, active_only: bool = False):
        return list_prompts(sf, live_type=live_type, active_only=active_only)

    @app.post("/studio/prompts")
    def post_prompt(body: PromptBody):
        try:
            return {"id": create_prompt(sf, name=body.name, body=body.body, live_type=body.live_type)}
        except PromptError as e:
            raise _bad_request(e)

    @app.patch("/studio/prompts/{prompt_id}")
    def patch_prompt(prompt_id: str, body: PromptPatch):
        try:
            return {"version": update_prompt(
                sf, prompt_id, name=body.name, body=body.body,
                live_type=body.live_type, active=body.active,
            )}
        except PromptError as e:
            raise _bad_request(e)

    @app.delete("/studio/prompts/{prompt_id}")
    def remove_prompt(prompt_id: str):
        try:
            delete_prompt(sf, prompt_id)
            return {"deleted": True}
        except PromptError as e:
            raise _bad_request(e)

    # ---------- 引擎探测（设置页数据源） ----------

    @app.get("/studio/engines")
    def engines():
        out: dict = {}
        for stage in ("asr", "diarize", "llm", "hooks", "render"):
            try:
                res = reg.resolve(stage)
                out[stage] = {
                    "engine": res.name,
                    "degraded": [{"engine": d.engine, "reason": d.reason} for d in res.degraded],
                }
            except EngineUnavailable as e:
                out[stage] = {"engine": None, "error": str(e)}
        signals = {}
        for name in settings.chains.signals:
            effective = "mock_signal" if name == "mock" else name
            try:
                signals[name] = {"available": True, "engine": reg.resolve_name(effective).engine.name}
            except EngineUnavailable as e:
                signals[name] = {"available": False, "reason": str(e)}
        out["signals"] = signals
        return out

    return app


def dev_app() -> FastAPI:
    """本地开发入口：uvicorn studio.api.app:dev_app --port 26761

    数据目录 .data-dev/studio.db（与上游示例服务隔离）；cpu-cloud 档案。
    """
    from pathlib import Path

    from sqlalchemy import create_engine

    from studio.models import Base
    from studio.settings import load_settings

    root = Path(__file__).resolve().parents[2]
    data = root / ".data-dev"
    data.mkdir(exist_ok=True)
    engine = create_engine(f"sqlite:///{(data / 'studio.db').as_posix()}")
    Base.metadata.create_all(engine)
    settings = load_settings(root / "profiles", "cpu-cloud", data_dir=data)
    ctx = EngineContext(
        profile="cpu-cloud", data_dir=data, models_dir=data / "models", gpu_available=False
    )
    return create_app(settings, ctx, sessionmaker(bind=engine))