"""管线编排（04 号方案 §6/§7）。

P0 范围：transcribe → signals → analyze → candidates_ready（人工审核前停下）。
- 同步 in-process 执行（沿上游桌面/CLI 模式，不依赖 Celery/Redis）。
- 进度：每阶段数字事件（done/total/unit）+ 降级显式记录，写 StudioTask.progress。
- 取消：cancel 事件在阶段边界与进度回调中检查，状态 cancelled，已完成缓存保留。
- 续跑：转写按缓存键命中即跳过（跨机续跑同键）；analyze 廉价可重跑。
- 失败：任务 failed + 错误落库，已完成阶段缓存保留，重试从失败阶段续。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.contracts.selection import process_candidates
from studio.engines.base import (
    ClipPlan,
    EngineContext,
    MaterialRef,
    ProgressEvent,
    ProgressFn,
    RenderPlan,
    TranscriptResult,
    Word,
)
from studio.engines.cache import TranscriptCache, file_fingerprint, make_cache_key
from studio.engines.registry import EngineRegistry, EngineUnavailable
from studio.models import (
    StudioArtifact,
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTask,
    StudioTranscript,
)
from studio.pipeline.review import review_candidates
from studio.pipeline.signals_context import apply_resonance, summarize_tracks
from studio.prompts import resolve_prompt_body
from studio.settings import Settings


class PipelineCancelled(Exception):
    pass


@dataclass
class PipelineResult:
    project_id: str
    task_id: str
    status: str
    candidate_ids: list[str] = field(default_factory=list)
    degraded: list[dict] = field(default_factory=list)
    discarded: list[dict] = field(default_factory=list)
    error: str | None = None


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        ctx: EngineContext,
        session_factory: sessionmaker[Session],
        registry: EngineRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._ctx = ctx
        self._sf = session_factory
        self._registry = registry or EngineRegistry(settings, ctx)
        self._cache = TranscriptCache(session_factory)

    # ---------- 内部工具 ----------

    def _check_cancel(self, cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise PipelineCancelled()

    def _emit(
        self,
        task_id: str,
        event: ProgressEvent,
        sink: ProgressFn | None,
        degraded: list[dict],
    ) -> None:
        payload = {
            "stage": event.stage,
            "done": event.done,
            "total": event.total,
            "unit": event.unit,
            "message": event.message,
            "degraded": degraded,
        }
        with self._sf() as s:
            task = s.get(StudioTask, task_id)
            if task is not None:
                task.progress = payload
                task.stage = event.stage
                s.commit()
        if sink is not None:
            sink(event)

    # ---------- 各阶段 ----------

    def _stage_transcribe(
        self,
        material: MaterialRef,
        task_id: str,
        cancel: threading.Event | None,
        sink: ProgressFn | None,
        degraded: list[dict],
    ) -> TranscriptResult:
        res = self._registry.resolve("asr")
        degraded.extend({"engine": d.engine, "reason": d.reason} for d in res.degraded)
        fp = file_fingerprint(material.file_path)
        # diarize 开关纳入缓存键：开/关两种转写互不污染（P1-7）
        params = {"language": "zh", "hotwords": [], "diarize": self._settings.diarize_enabled}
        key = make_cache_key(fp, res.engine.name, getattr(res.engine, "model_size", "default"), params)
        hit = self._cache.get(key)
        if hit is not None:
            self._emit(
                task_id,
                ProgressEvent("transcribing", 1, 1, "段", "转写缓存命中，跳过"),
                sink,
                degraded,
            )
            return hit

        def progress(ev: ProgressEvent) -> None:
            self._check_cancel(cancel)
            self._emit(task_id, ev, sink, degraded)

        audio_path = material.file_path  # P0 直传媒体文件；抽音频优化在 P1
        tr = res.engine.transcribe(
            audio_path, hotwords=[], language="zh", progress=progress
        )
        if self._settings.diarize_enabled:
            self._check_cancel(cancel)
            dres = self._registry.resolve("diarize")
            degraded.extend({"engine": d.engine, "reason": d.reason} for d in dres.degraded)
            tr = dres.engine.assign_speakers(tr, audio_path, progress)
        # 分离完成后才写缓存：说话人标签随转写一起落库
        self._cache.put(material.material_id, key, tr)
        return tr

    def _stage_signals(
        self,
        material: MaterialRef,
        task_id: str,
        cancel: threading.Event | None,
        sink: ProgressFn | None,
        degraded: list[dict],
    ) -> dict:
        tracks: dict = {}
        names = self._settings.chains.signals
        for i, name in enumerate(names):
            self._check_cancel(cancel)
            try:
                # signals 链尾 mock 显式映射到信号抽取器（resolve_name 不做阶段映射）
                res = self._registry.resolve_name("mock_signal" if name == "mock" else name)
            except EngineUnavailable as e:
                degraded.append({"engine": name, "reason": str(e)})
                continue
            track = res.engine.extract(material, lambda ev: self._check_cancel(cancel))
            if not track.series:
                # 无弹幕文件/解析为空 → 显式降维，不伪造数据（P1-4）
                degraded.append({"engine": name, "reason": "无有效数据（如无弹幕文件），自动降维"})
                continue
            tracks[name] = {
                "bucket_ms": track.bucket_ms,
                "series": {k: list(v) for k, v in track.series.items()},
            }
            self._emit(
                task_id,
                ProgressEvent("signaling", i + 1, len(names), "路", f"信号 {name}"),
                sink,
                degraded,
            )
        return tracks

    def _stage_analyze(
        self,
        project: StudioProject,
        tr: TranscriptResult,
        tracks: dict,
        task_id: str,
        cancel: threading.Event | None,
        sink: ProgressFn | None,
        degraded: list[dict],
    ) -> tuple[list[str], list[dict], list[dict], bool]:
        self._check_cancel(cancel)
        res = self._registry.resolve("llm")
        degraded.extend({"engine": d.engine, "reason": d.reason} for d in res.degraded)
        timeline = "\n".join(
            f"{i}|{s.start_ms}|{s.end_ms}|{s.text}" for i, s in enumerate(tr.sentences)
        )
        system = (
            getattr(project, "prompt_body", None)
            or "你是直播切片选段助手。从时间轴中挑选值得独立传播的高光片段。"
        ) + "\n只输出 JSON。时间戳必须来自给定时间轴，quote 必须是转写原文子串。"
        self._emit(task_id, ProgressEvent("analyzing", 0, 1, "批", "LLM 选段中"), sink, degraded)
        # 偏好回流（P3-6）：人工采纳/否决 → 方向性偏置（无记录不注入噪声）
        from studio.feedback.preference import preference_prompt_block, preference_summary

        pref_block = preference_prompt_block(
            preference_summary(self._sf, live_type=project.live_type)
        )
        user = (
            f"直播类型: {project.live_type}\n定向: {project.intent or '无'}\n"
            f"背景: {project.background or '无'}\n"
            + (f"{pref_block}\n" if pref_block else "")
            + f"时间轴:\n{timeline}\n"
            f"信号摘要（高能段优先关注）:\n{summarize_tracks(tracks)}\n"
            '输出 {"candidates":[{start_ms,end_ms,clip_type,quote,reason,scores,title,caption}]}'
        )
        out = res.engine.chat_json(system, user, {})
        # 契约铁律全部在 studio.contracts（P1-1）：schema/吸附/时长/quote/NMS/归一
        kept, discarded = process_candidates(out, tr, live_type=project.live_type)
        # 复审环（P1-2）：judge→repair→rejudge，每轮落回执；开关在 Profile
        kept, rev_discarded, review_log = review_candidates(
            kept, tr, res.engine, live_type=project.live_type,
            enabled=self._settings.review_loop,
        )
        discarded.extend(rev_discarded)
        # 多信号共振加成（P1-5）：≥2 路同桶高位才加分，证据落 scores
        apply_resonance(kept, tracks)

        ids: list[str] = []
        with self._sf() as s:
            for c in kept:
                row = StudioCandidate(
                    project_id=project.id,
                    layer="clips0",
                    start_ms=c["start_ms"],
                    end_ms=c["end_ms"],
                    clip_type=c.get("clip_type"),
                    scores=c.get("scores"),
                    rank_score=c.get("rank_score"),
                    quote=c.get("quote"),
                    reason=c.get("reason"),
                    title=c.get("title"),
                    caption=c.get("caption"),
                    created_by="ai",
                )
                s.add(row)
                s.flush()
                ids.append(row.id)
            s.commit()
        self._emit(task_id, ProgressEvent("analyzing", 1, 1, "批", f"候选 {len(ids)} 条"), sink, degraded)
        return ids, discarded, review_log, bool(pref_block)

    # ---------- 入口 ----------

    def run(
        self,
        project_id: str,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressFn | None = None,
    ) -> PipelineResult:
        with self._sf() as s:
            project = s.get(StudioProject, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")
            material = s.get(StudioMaterial, project.material_id)
            # 会话内快照为普通对象：阶段函数不得触碰游离 ORM 实例（DetachedInstanceError）
            mat = MaterialRef(
                material_id=material.id,
                file_path=material.file_path or "",
                fingerprint=material.fingerprint,
                duration_ms=material.duration_ms or 0,
                danmaku_path=material.danmaku_path,
            )
            proj = SimpleNamespace(
                id=project.id,
                live_type=project.live_type,
                intent=project.intent,
                background=project.background,
                prompt_body=resolve_prompt_body(
                    self._sf, prompt_id=project.prompt_id, live_type=project.live_type
                ),
            )
            task = StudioTask(
                project_id=project_id, kind="analyze", stage="pending", status="running"
            )
            s.add(task)
            project.status = "transcribing"
            s.commit()
            task_id = task.id

        degraded: list[dict] = []
        discarded: list[dict] = []
        candidate_ids: list[str] = []
        review_log: list[dict] = []
        pref_applied = False
        tracks: dict = {}  # 取消/失败早退时回执仍可构造（R-1 修复）
        try:
            tr = self._stage_transcribe(mat, task_id, cancel, progress, degraded)
            self._check_cancel(cancel)
            tracks = self._stage_signals(mat, task_id, cancel, progress, degraded)
            candidate_ids, discarded, review_log, pref_applied = self._stage_analyze(
                proj, tr, tracks, task_id, cancel, progress, degraded
            )
            status = "candidates_ready"
            error = None
        except PipelineCancelled:
            status, error = "cancelled", None
        except Exception as e:  # 失败落库，缓存保留，可续跑
            status, error = "failed", f"{type(e).__name__}: {e}"

        with self._sf() as s:
            task = s.get(StudioTask, task_id)
            task.status = "done" if status == "candidates_ready" else status
            task.error = error
            task.receipt = {
                "degraded": degraded,
                "discarded": discarded,
                "candidate_ids": candidate_ids,
                "review": review_log,
                "preference_applied": pref_applied,
                "signals": tracks,  # 审核台时间轴曲线数据源（R-1）
                "word_level": tr.word_level if status == "candidates_ready" else None,
            }
            p = s.get(StudioProject, project_id)
            p.status = status
            s.commit()

        return PipelineResult(
            project_id=project_id,
            task_id=task_id,
            status=status,
            candidate_ids=candidate_ids,
            degraded=degraded,
            discarded=discarded,
            error=error,
        )

    # ---------- 渲染入口（P2-1d：只读人工采纳的 clips1） ----------

    def _plan_edits(
        self,
        clip: ClipPlan,
        words: tuple[Word, ...],
        sentences: tuple,
        source_path: str,
        *,
        debreath: bool,
        filler_cut: bool,
        keep_breath_ms: int,
        cold_open: bool,
        flash_forward: bool,
    ) -> tuple[list, tuple[int, int] | None, tuple[int, int] | None, dict]:
        """剪辑决策（P2-2/P2-3）：返回 (cuts, cold_open区间, flash区间, 回执)。

        定位失败自动跳过并记录，绝不阻断渲染。能量 VAD 为 v1（RMS 阈值），
        神经 VAD 需模型批准后升级——回执如实标注。
        """
        from studio.media import range_rms, scan_silence
        from studio.pipeline.editlist import detect_breaths, detect_fillers

        cuts: list = []
        receipt: dict = {
            "debreath": [], "filler": [], "cold_open": None,
            "flash_forward": None, "skipped": [], "vad": "energy_rms_v1",
        }
        if debreath and words:
            silences = scan_silence(source_path, clip.start_ms / 1000, clip.end_ms / 1000)
            bc = detect_breaths(
                words, clip.start_ms, clip.end_ms, silences,
                lambda a, b: range_rms(source_path, a, b),
                keep_breath_ms=keep_breath_ms,
            )
            cuts.extend(bc)
            receipt["debreath"] = [
                {"start_ms": c.start_ms, "end_ms": c.end_ms, "reason": c.reason} for c in bc
            ]
        if filler_cut and words:
            fc = detect_fillers(words, clip.start_ms, clip.end_ms)
            cuts.extend(fc)
            receipt["filler"] = [
                {"start_ms": c.start_ms, "end_ms": c.end_ms, "reason": c.reason} for c in fc
            ]
        co_span: tuple[int, int] | None = None
        if cold_open:
            # quote 确定性定位：切片内找包含 quote 的句（归一化去空格），句界即词界
            q = "".join((clip.quote or "").split())
            if q:
                for s in sentences:
                    st = "".join(str(s.get("text", "")).split())
                    if q in st and clip.start_ms <= s["start_ms"] and s["end_ms"] <= clip.end_ms:
                        co_span = (int(s["start_ms"]), int(s["end_ms"]))
                        break
            if co_span is None:
                receipt["skipped"].append("cold_open 定位失败（quote 未在切片内句中找到），自动跳过")
        receipt["cold_open"] = co_span
        ff_span: tuple[int, int] | None = None
        if flash_forward:
            try:
                from studio.engines.cover import rms_peak_times

                peaks = rms_peak_times(
                    source_path, clip.start_ms / 1000, clip.end_ms / 1000,
                    window_s=0.5, top=1,
                )
                if peaks:
                    t = peaks[0]
                    s = max(clip.start_ms, int((t - 0.25) * 1000))
                    e = min(clip.end_ms, int((t + 0.25) * 1000))
                    if e - s >= 300:
                        ff_span = (s, e)
            except Exception as ex:
                receipt["skipped"].append(f"flash_forward 定位异常: {type(ex).__name__}: {ex}")
            if ff_span is None and not any("flash_forward" in x for x in receipt["skipped"]):
                receipt["skipped"].append("flash_forward 定位失败（无响度峰），自动跳过")
        receipt["flash_forward"] = ff_span
        return cuts, co_span, ff_span, receipt

    def render_project(
        self,
        project_id: str,
        *,
        platform: str = "douyin",
        portrait: bool = True,
        subtitles: bool = True,
        debreath: bool = False,
        filler_cut: bool = False,
        keep_breath_ms: int = 250,
        cold_open: bool = True,
        cold_open_jumpcut: bool = False,
        flash_forward: bool = False,
        l1_overlay: bool = True,
        l2_tts: bool = False,
        l2_rank_threshold: float = 80.0,
        use_gen_hooks: bool = False,
        gen_asset_ids: list[str] | None = None,
        cancel: threading.Event | None = None,
        progress: ProgressFn | None = None,
    ) -> PipelineResult:
        with self._sf() as s:
            project = s.get(StudioProject, project_id)
            if project is None:
                raise ValueError(f"项目不存在: {project_id}")
            material = s.get(StudioMaterial, project.material_id)
            mat = MaterialRef(
                material_id=material.id,
                file_path=material.file_path or "",
                fingerprint=material.fingerprint,
                duration_ms=material.duration_ms or 0,
                danmaku_path=material.danmaku_path,
            )
            adopted = s.execute(
                select(StudioCandidate)
                .where(
                    StudioCandidate.project_id == project_id,
                    StudioCandidate.layer == "clips1",
                    StudioCandidate.adopted.is_(True),
                )
                .order_by(StudioCandidate.rank_score.desc())
            ).scalars().all()
            if not adopted:
                raise ValueError("无可渲染候选：请先在审核台采纳 clips1（渲染只读人工采纳结果）")
            clips = [
                ClipPlan(
                    candidate_id=c.id, start_ms=c.start_ms, end_ms=c.end_ms,
                    title=c.title or "", quote=c.quote or "", hook_plan=c.hook_plan,
                    rank_score=c.rank_score,
                )
                for c in adopted
            ]
            tr_row = s.execute(
                select(StudioTranscript)
                .where(StudioTranscript.material_id == mat.material_id)
                .order_by(StudioTranscript.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            words = (
                tuple(Word(**w) for w in tr_row.words)
                if tr_row is not None and tr_row.word_level
                else ()
            )
            sentences = tuple(tr_row.sentences) if tr_row is not None else ()
            # P3 生成素材快照：cid → [(kind, file_path, mock)]（mock 禁入真实渲染）
            gen_q = select(StudioArtifact).where(
                StudioArtifact.project_id == project_id,
                StudioArtifact.kind.in_(("hook_image", "hook_video")),
            )
            if gen_asset_ids is not None:
                # 显式选择（前端勾选）；项目过滤保证跨项目 ID 不被纳入
                gen_q = gen_q.where(StudioArtifact.id.in_(gen_asset_ids))
            gen_rows = s.execute(gen_q.order_by(StudioArtifact.created_at.desc())).scalars().all()
            gen_assets: dict[str, list[tuple[str, str, bool]]] = {}
            for g in gen_rows:
                gen_assets.setdefault(g.candidate_id, []).append(
                    (g.kind, g.file_path, bool((g.meta or {}).get("mock")))
                )
            task = StudioTask(
                project_id=project_id, kind="render", stage="pending", status="running"
            )
            s.add(task)
            project.status = "rendering"
            s.commit()
            task_id = task.id

        degraded: list[dict] = []
        artifact_ids: list[str] = []
        render_log: list[dict] = []
        out_dir = self._settings.data_dir / "projects" / project_id / "render"
        try:
            res = self._registry.resolve("render")
            degraded.extend({"engine": d.engine, "reason": d.reason} for d in res.degraded)
            for i, clip in enumerate(clips):
                self._check_cancel(cancel)
                self._emit(
                    task_id,
                    ProgressEvent("rendering", i, len(clips), "条", f"渲染 {clip.title or clip.candidate_id}"),
                    progress, degraded,
                )
                out_path = out_dir / f"{clip.candidate_id}.mp4"
                cuts, co_span, ff_span, edit_receipt = self._plan_edits(
                    clip, words, sentences, mat.file_path,
                    debreath=debreath, filler_cut=filler_cut,
                    keep_breath_ms=keep_breath_ms,
                    cold_open=cold_open, flash_forward=flash_forward,
                )
                for skip in edit_receipt["skipped"]:
                    degraded.append({"engine": "hook_locate", "reason": skip})
                # 制作钩子（P2-4/P2-5）：决策树代码做，LLM 只填内容
                from dataclasses import replace

                from studio.pipeline.hook_decision import decide_hooks

                dec = decide_hooks(
                    native_used=co_span is not None,
                    l1_enabled=l1_overlay, l2_enabled=l2_tts,
                    rank_score=clip.rank_score, l2_rank_threshold=l2_rank_threshold,
                )
                hook_receipt: dict = {
                    "decision": {"l1": dec.l1, "l2": dec.l2, "reasons": list(dec.reasons)},
                    "l1": None, "l2": None, "skipped": [],
                }
                overlay: dict | None = None
                hook_audio: str | None = None
                if dec.l1:
                    llm_text = None
                    try:
                        lres = self._registry.resolve("llm")
                        out = lres.engine.chat_json(
                            "你是钩子文案写手。为切片写一句不超过20字的悬念句，只输出 JSON。",
                            f"标题: {clip.title}\n金句: {clip.quote}\n输出 {{\"text\": \"悬念句\"}}",
                            {},
                        )
                        llm_text = out.get("text")
                    except Exception:
                        llm_text = None  # LLM 不可用 → 引擎回退标题/金句
                    hp = dict(clip.hook_plan or {})
                    if llm_text:
                        hp["l1_text"] = {"text": llm_text}
                    try:
                        hres = self._registry.resolve_name("text_overlay")
                        asset = hres.engine.generate(replace(clip, hook_plan=hp), self._ctx, lambda ev: None)
                    except EngineUnavailable as e:
                        asset = None
                        hook_receipt["skipped"].append(str(e))
                    if asset is None:
                        msg = "L1 写不出悬念句，自动不加"
                        hook_receipt["skipped"].append(msg)
                        degraded.append({"engine": "text_overlay", "reason": msg})
                    else:
                        overlay = {"text": asset.text, "duration_s": asset.meta["duration_s"]}
                        hook_receipt["l1"] = overlay
                if dec.l2:
                    try:
                        hres = self._registry.resolve_name("edge_tts")
                        asset = hres.engine.generate(clip, self._ctx, lambda ev: None)
                    except EngineUnavailable as e:
                        asset = None
                        hook_receipt["skipped"].append(str(e))
                    if asset is None or not asset.file_path:
                        msg = "L2 TTS 生成失败，自动跳过"
                        hook_receipt["skipped"].append(msg)
                        degraded.append({"engine": "edge_tts", "reason": msg})
                    else:
                        hook_audio = asset.file_path
                        hook_receipt["l2"] = {
                            "file_path": asset.file_path,
                            "engine": asset.meta.get("engine"),
                            "text": asset.text,
                        }
                # P3 生成素材集成：mock 禁入真实渲染；L4 生视频替代原生冷开场
                broll: str | None = None
                gen_video: str | None = None
                if use_gen_hooks:
                    for kind, gpath, gmock in gen_assets.get(clip.candidate_id, []):
                        if gmock:
                            msg = "mock 生成素材禁入真实渲染，已跳过"
                            hook_receipt["skipped"].append(msg)
                            degraded.append({"engine": "gen_hook", "reason": msg})
                            continue
                        if kind == "hook_video" and gen_video is None:
                            gen_video = gpath
                        elif kind == "hook_image" and broll is None:
                            broll = gpath
                    if gen_video and co_span is not None:
                        co_span = None  # 生视频冷开场优先，避免双钩子
                        hook_receipt["skipped"].append("L4 生视频冷开场替代原生 cold_open")
                # AIGC 显式角标：任何 AI 内容（TTS/B-roll/生视频）强制标识
                aigc_badge = bool(hook_audio or broll or gen_video)
                hook_receipt["gen"] = {
                    "broll_image": broll, "hook_video": gen_video, "aigc_badge": aigc_badge,
                }
                plan = RenderPlan(
                    clip=clip, platform=platform, portrait=portrait, subtitles=subtitles,
                    out_path=str(out_path), source_path=mat.file_path, words=words,
                    cuts=tuple((c.start_ms, c.end_ms) for c in cuts),
                    cold_open=co_span, cold_open_jumpcut=cold_open_jumpcut,
                    flash_forward=ff_span,
                    hook_overlay=overlay, hook_audio=hook_audio,
                    broll_image=broll, hook_video=gen_video, aigc_badge=aigc_badge,
                )

                def step(ev: ProgressEvent) -> None:
                    self._check_cancel(cancel)

                art = res.engine.render(plan, step)
                with self._sf() as s:
                    row = StudioArtifact(
                        project_id=project_id, candidate_id=clip.candidate_id,
                        kind="mp4", platform=platform, file_path=art.file_path,
                        size_bytes=art.size_bytes, meta=art.meta,
                        # 稳定内容 ID（P3 回流对账键）：项目+候选决定，重渲染不变
                        content_id=f"{project_id[:8]}-{clip.candidate_id[:8]}",
                    )
                    s.add(row)
                    s.flush()
                    artifact_ids.append(row.id)
                    s.commit()
                entry = {
                    "candidate_id": clip.candidate_id,
                    "artifact_id": artifact_ids[-1],
                    "meta": art.meta,
                    "edits": edit_receipt,
                    "hooks": hook_receipt,
                }
                # 封面：失败显式降级记录，不阻断成片交付（宁缺毋滥）
                try:
                    from studio.engines.cover import select_cover

                    cover_meta = select_cover(
                        art.file_path, 0, int((art.meta.get("end_ms", clip.end_ms) - art.meta.get("start_ms", clip.start_ms))),
                        out_dir / f"{clip.candidate_id}_cover.jpg",
                    )
                    with self._sf() as s:
                        crow = StudioArtifact(
                            project_id=project_id, candidate_id=clip.candidate_id,
                            kind="cover", platform=platform, file_path=cover_meta["file_path"],
                            size_bytes=cover_meta["size_bytes"], meta=cover_meta,
                        )
                        s.add(crow)
                        s.flush()
                        artifact_ids.append(crow.id)
                        s.commit()
                    entry["cover"] = cover_meta
                except Exception as e:
                    degraded.append({"engine": "cover", "reason": f"{type(e).__name__}: {e}"})
                    entry["cover"] = None
                render_log.append(entry)
                self._emit(
                    task_id,
                    ProgressEvent("rendering", i + 1, len(clips), "条", f"完成 {i + 1}/{len(clips)}"),
                    progress, degraded,
                )
            status, error = "rendered", None
        except PipelineCancelled:
            status, error = "cancelled", None
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"

        with self._sf() as s:
            task = s.get(StudioTask, task_id)
            task.status = "done" if status == "rendered" else status
            task.error = error
            task.receipt = {"degraded": degraded, "renders": render_log, "artifact_ids": artifact_ids}
            p = s.get(StudioProject, project_id)
            p.status = status
            s.commit()

        return PipelineResult(
            project_id=project_id, task_id=task_id, status=status,
            candidate_ids=[c.candidate_id for c in clips],
            degraded=degraded, error=error,
        )