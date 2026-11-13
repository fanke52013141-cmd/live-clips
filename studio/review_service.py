"""审核工作流服务（04 号方案 P1-6，clips0/clips1 双区间数据模型的操作层）。

铁律：
- clips0 只由 AI 写，人工只读；任何人工决定（采纳/否决/精修）落 clips1，parent_id 溯源。
- 渲染只读 adopted=True 的 clips1（P2 渲染引擎强制）。
- 人工边界修改校验 0 ≤ start < end ≤ 素材时长；操作者记 created_by=user:<id>。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.models import (
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTranscript,
)


class ReviewError(ValueError):
    pass


def _candidate_dict(c: StudioCandidate) -> dict:
    return {
        "id": c.id, "project_id": c.project_id, "layer": c.layer,
        "parent_id": c.parent_id, "start_ms": c.start_ms, "end_ms": c.end_ms,
        "clip_type": c.clip_type, "scores": c.scores, "rank_score": c.rank_score,
        "quote": c.quote, "reason": c.reason, "title": c.title, "caption": c.caption,
        "hook_plan": c.hook_plan, "adopted": c.adopted,
        "rejected_reason": c.rejected_reason, "created_by": c.created_by,
    }


def list_candidates(
    sf: sessionmaker[Session], project_id: str, *, layer: str | None = None
) -> list[dict]:
    if layer is not None and layer not in ("clips0", "clips1"):
        raise ReviewError(f"非法 layer: {layer}")
    with sf() as s:
        q = select(StudioCandidate).where(StudioCandidate.project_id == project_id)
        if layer:
            q = q.where(StudioCandidate.layer == layer)
        rows = s.execute(q.order_by(StudioCandidate.rank_score.desc())).scalars().all()
        return [_candidate_dict(c) for c in rows]


def _get_checked(s: Session, candidate_id: str, expect_layer: str) -> StudioCandidate:
    c = s.get(StudioCandidate, candidate_id)
    if c is None:
        raise ReviewError(f"候选不存在: {candidate_id}")
    if c.layer != expect_layer:
        raise ReviewError(f"只能操作 {expect_layer}，实际 {c.layer}")
    return c


def _material_duration(s: Session, project_id: str) -> int:
    p = s.get(StudioProject, project_id)
    if p is None:
        raise ReviewError(f"项目不存在: {project_id}")
    m = s.get(StudioMaterial, p.material_id)
    return (m.duration_ms if m else None) or 0


def _check_bounds(start_ms: int, end_ms: int, duration_ms: int) -> None:
    if not (0 <= start_ms < end_ms):
        raise ReviewError(f"非法区间: {start_ms}-{end_ms}")
    if duration_ms and end_ms > duration_ms:
        raise ReviewError(f"出点超素材时长: {end_ms} > {duration_ms}")


def adopt_candidate(
    sf: sessionmaker[Session],
    candidate_id: str,
    *,
    user: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    title: str | None = None,
    caption: str | None = None,
) -> str:
    """采纳 clips0 → 生成 clips1（可带入出点/标题/文案修改）。返回 clips1 id。"""
    with sf() as s:
        src = _get_checked(s, candidate_id, "clips0")
        dur = _material_duration(s, src.project_id)
        new_start = start_ms if start_ms is not None else src.start_ms
        new_end = end_ms if end_ms is not None else src.end_ms
        _check_bounds(new_start, new_end, dur)
        c1 = StudioCandidate(
            project_id=src.project_id, layer="clips1", parent_id=src.id,
            start_ms=new_start, end_ms=new_end, clip_type=src.clip_type,
            scores=src.scores, rank_score=src.rank_score, quote=src.quote,
            reason=src.reason,
            title=title if title is not None else src.title,
            caption=caption if caption is not None else src.caption,
            hook_plan=src.hook_plan, adopted=True,
            created_by=f"user:{user}",
        )
        s.add(c1)
        s.commit()
        return c1.id


def reject_candidate(
    sf: sessionmaker[Session], candidate_id: str, *, user: str, reason: str
) -> str:
    """否决也落 clips1（adopted=False + 理由），AI 原始候选保留可审计。"""
    if not reason.strip():
        raise ReviewError("否决必须给理由")
    with sf() as s:
        src = _get_checked(s, candidate_id, "clips0")
        c1 = StudioCandidate(
            project_id=src.project_id, layer="clips1", parent_id=src.id,
            start_ms=src.start_ms, end_ms=src.end_ms, clip_type=src.clip_type,
            scores=src.scores, rank_score=src.rank_score, quote=src.quote,
            reason=src.reason, title=src.title, caption=src.caption,
            adopted=False, rejected_reason=reason,
            created_by=f"user:{user}",
        )
        s.add(c1)
        s.commit()
        return c1.id


def update_clips1(
    sf: sessionmaker[Session],
    clips1_id: str,
    *,
    user: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    title: str | None = None,
    caption: str | None = None,
    hook_plan: dict | None = None,
    adopted: bool | None = None,
) -> dict:
    """人工精修 clips1。clips0 不可改（_get_checked 强制）。"""
    with sf() as s:
        c = _get_checked(s, clips1_id, "clips1")
        dur = _material_duration(s, c.project_id)
        new_start = start_ms if start_ms is not None else c.start_ms
        new_end = end_ms if end_ms is not None else c.end_ms
        _check_bounds(new_start, new_end, dur)
        c.start_ms, c.end_ms = new_start, new_end
        if title is not None:
            c.title = title
        if caption is not None:
            c.caption = caption
        if hook_plan is not None:
            c.hook_plan = hook_plan
        if adopted is not None:
            c.adopted = adopted
        c.created_by = f"user:{user}"
        s.commit()
        return _candidate_dict(c)


def get_transcript(sf: sessionmaker[Session], project_id: str) -> dict | None:
    """逐字稿页数据源：项目素材的最新转写（句级+词级）。"""
    with sf() as s:
        p = s.get(StudioProject, project_id)
        if p is None:
            raise ReviewError(f"项目不存在: {project_id}")
        tr = s.execute(
            select(StudioTranscript)
            .where(StudioTranscript.material_id == p.material_id)
            .order_by(StudioTranscript.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if tr is None:
            return None
        return {
            "engine": tr.engine, "model": tr.model, "word_level": tr.word_level,
            "words": tr.words, "sentences": tr.sentences, "hotwords": tr.hotwords,
        }