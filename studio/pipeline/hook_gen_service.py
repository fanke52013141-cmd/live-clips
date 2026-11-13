"""生成式钩子服务（P3，04 号方案 §7.3）。

铁律：
- 费用预估先行；generate 必须 confirm=True（人工确认），绝不静默计费
- L4 仅 rank top3 候选（文档门槛）；L3 top5；门槛在预估回执明示
- cloud 提供商需 settings.allow_paid_generation=True；mock 永远零费用可跑
- 预算上限 max_cost：预估超限直接拒绝
- 产物 meta.mock 强制标注；mock 素材不得进真实渲染（渲染集成批强制）
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.engines.base import EngineContext
from studio.engines.gen_providers import GenProvider, default_gen_providers
from studio.models import StudioArtifact, StudioCandidate, StudioProject
from studio.settings import Settings

L3_TOP_N = 5
L4_TOP_N = 3
_KIND_BY_LEVEL = {3: "image", 4: "video"}
_ARTIFACT_KIND = {3: "hook_image", 4: "hook_video"}


class HookGenError(RuntimeError):
    pass


def _select_candidates(
    sf: sessionmaker[Session], project_id: str, level: int,
    candidate_ids: list[str] | None,
) -> list[StudioCandidate]:
    if level not in _KIND_BY_LEVEL:
        raise HookGenError(f"非法钩子级别: {level}（仅 3/4）")
    with sf() as s:
        if s.get(StudioProject, project_id) is None:
            raise HookGenError(f"项目不存在: {project_id}")
        q = select(StudioCandidate).where(
            StudioCandidate.project_id == project_id,
            StudioCandidate.layer == "clips1",
            StudioCandidate.adopted.is_(True),
        ).order_by(StudioCandidate.rank_score.desc())
        rows = s.execute(q).scalars().all()
        top_n = L4_TOP_N if level == 4 else L3_TOP_N
        gated = rows[:top_n]
        if candidate_ids is not None:
            allowed = {c.id for c in gated}
            bad = [cid for cid in candidate_ids if cid not in allowed]
            if bad:
                raise HookGenError(
                    f"候选超出 L{level} top{top_n} 门槛或非采纳: {bad}"
                )
            gated = [c for c in gated if c.id in set(candidate_ids)]
        if not gated:
            raise HookGenError("无可生成候选：请先采纳 clips1")
        # 会话内快照
        return [
            StudioCandidate(
                id=c.id, project_id=c.project_id, layer=c.layer,
                start_ms=c.start_ms, end_ms=c.end_ms, title=c.title,
                quote=c.quote, hook_plan=c.hook_plan, rank_score=c.rank_score,
                adopted=c.adopted, created_by=c.created_by,
            )
            for c in gated
        ]


def _prompt_for(c: StudioCandidate, level: int) -> str:
    hp = c.hook_plan or {}
    key = "l3_broll" if level == 3 else "l4_gen"
    custom = (hp.get(key) or {}).get("prompt")
    if custom:
        return custom
    base = f"直播切片钩子素材：{c.title or ''}（金句：{c.quote or ''}）"
    return base + ("，2秒竖屏空镜" if level == 3 else "，3秒竖屏动态开场")


def _resolve_provider(
    providers: dict[str, GenProvider], provider_name: str | None, level: int, ctx: EngineContext,
) -> GenProvider:
    kind = _KIND_BY_LEVEL[level]
    name = provider_name or f"mock_{kind}"
    p = providers.get(name)
    if p is None:
        raise HookGenError(f"未知生成提供商: {name}（可选: {sorted(providers)}）")
    if p.kind != kind:
        raise HookGenError(f"提供商 {name} 类型为 {p.kind}，与 L{level}({kind}) 不符")
    ok, reason = p.available(ctx)
    if not ok:
        raise HookGenError(f"提供商不可用: {name}: {reason}")
    return p


def estimate_hooks(
    sf: sessionmaker[Session], project_id: str, *, level: int,
    provider_name: str | None = None, candidate_ids: list[str] | None = None,
    ctx: EngineContext | None = None, providers: dict[str, GenProvider] | None = None,
) -> dict:
    ctx = ctx or EngineContext(profile="cpu-cloud", data_dir=Path("."), models_dir=Path("."), gpu_available=False)
    providers = providers if providers is not None else default_gen_providers()
    cands = _select_candidates(sf, project_id, level, candidate_ids)
    p = _resolve_provider(providers, provider_name, level, ctx)
    items = []
    for c in cands:
        est = p.estimate({"prompt": _prompt_for(c, level)})
        items.append({
            "candidate_id": c.id, "title": c.title, "rank_score": c.rank_score,
            "prompt": _prompt_for(c, level), "estimate": est.to_dict(),
        })
    total = round(sum(i["estimate"]["total"] for i in items), 4)
    return {
        "level": level, "provider": p.name, "items": items, "total": total,
        "currency": "CNY",
        "gate": f"L{level} 仅 rank top{L4_TOP_N if level == 4 else L3_TOP_N}",
        "confirm_required": True,
        "note": "预估仅供参考；cloud 真实定价以官方为准（待核实）；mock 零费用",
    }


def generate_hooks(
    sf: sessionmaker[Session], settings: Settings, ctx: EngineContext, project_id: str,
    *, level: int, provider_name: str | None = None,
    candidate_ids: list[str] | None = None, confirm: bool = False,
    max_cost: float | None = None, providers: dict[str, GenProvider] | None = None,
) -> dict:
    if not confirm:
        raise HookGenError("生成式钩子必须人工确认（confirm=true）后才执行，绝不静默计费")
    providers = providers if providers is not None else default_gen_providers()
    cands = _select_candidates(sf, project_id, level, candidate_ids)
    p = _resolve_provider(providers, provider_name, level, ctx)
    is_mock = p.name.startswith("mock_")
    if not is_mock and not settings.allow_paid_generation:
        raise HookGenError("付费生成开关未开启（options.allow_paid_generation），拒绝计费操作")
    est = estimate_hooks(
        sf, project_id, level=level, provider_name=p.name,
        candidate_ids=[c.id for c in cands], ctx=ctx, providers=providers,
    )
    if max_cost is not None and est["total"] > max_cost:
        raise HookGenError(f"预估费用 {est['total']} 超预算上限 {max_cost}，拒绝执行")

    out_dir = settings.data_dir / "projects" / project_id / "hooks"
    generated: list[dict] = []
    skipped: list[dict] = []
    for c in cands:
        prompt = _prompt_for(c, level)
        ext = ".png" if level == 3 else ".mp4"
        out_path = out_dir / f"{c.id}_l{level}{ext}"
        try:
            meta = p.generate({"prompt": prompt}, out_path, ctx)
        except Exception as e:
            skipped.append({"candidate_id": c.id, "reason": f"{type(e).__name__}: {e}"})
            continue  # 宁缺毋滥：单条失败不阻断其余
        meta.update({
            "level": level, "candidate_id": c.id, "prompt": prompt,
            "cost": p.estimate({"prompt": prompt}).to_dict(),
            "aigc": True,  # 生成内容强制 AIGC 属性（合规层核对标识）
        })
        with sf() as s:
            row = StudioArtifact(
                project_id=project_id, candidate_id=c.id,
                kind=_ARTIFACT_KIND[level], file_path=str(out_path),
                size_bytes=out_path.stat().st_size if out_path.is_file() else 0,
                meta=meta,
            )
            s.add(row)
            s.commit()
            generated.append({"candidate_id": c.id, "artifact_id": row.id,
                              "file_path": str(out_path), "mock": meta.get("mock", False)})
    return {
        "level": level, "provider": p.name, "confirmed": True,
        "estimated_total": est["total"], "generated": generated, "skipped": skipped,
        "aigc_note": "生成内容须显式 AIGC 标识（角标烧录在渲染集成批实现）",
    }