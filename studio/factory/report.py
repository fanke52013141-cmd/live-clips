"""数据战报（P4-d，04 号方案 §11）：回流指标聚合。

按平台/切片聚合 views/likes/comments/shares/orders，给运营概览。
只报实际回流数据，无数据明示空，不伪造。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.models import StudioArtifact, StudioFeedback, StudioProject, StudioPublication

_METRICS = ("views", "likes", "comments", "shares", "orders")


def battle_report(sf: sessionmaker[Session], project_id: str | None = None) -> dict:
    with sf() as s:
        if project_id and s.get(StudioProject, project_id) is None:
            raise ValueError(f"项目不存在: {project_id}")
        pub_q = select(StudioPublication)
        pubs = s.execute(pub_q).scalars().all()
        art_cache: dict[str, StudioArtifact] = {}
        rows = []
        for pub in pubs:
            art = art_cache.get(pub.artifact_id)
            if art is None:
                art = s.get(StudioArtifact, pub.artifact_id)
                art_cache[pub.artifact_id] = art
            if art is None:
                continue
            if project_id and art.project_id != project_id:
                continue
            fb = s.execute(
                select(StudioFeedback).where(StudioFeedback.publication_id == pub.id)
                .order_by(StudioFeedback.imported_at.desc()).limit(1)
            ).scalar_one_or_none()
            if fb is None:
                continue
            rows.append((pub, art, fb.metrics))

    totals = {k: 0.0 for k in _METRICS}
    by_platform: dict[str, dict] = {}
    clips = []
    for pub, art, metrics in rows:
        for k in _METRICS:
            totals[k] += float(metrics.get(k, 0))
        plat = pub.platform or "unknown"
        bp = by_platform.setdefault(plat, {k: 0.0 for k in _METRICS} | {"publications": 0})
        for k in _METRICS:
            bp[k] += float(metrics.get(k, 0))
        bp["publications"] += 1
        clips.append({
            "artifact_id": art.id, "content_id": art.content_id, "platform": plat,
            "views": float(metrics.get("views", 0)),
            "engagement": round(
                (float(metrics.get("likes", 0)) + float(metrics.get("comments", 0))
                 + float(metrics.get("shares", 0))) / max(1.0, float(metrics.get("views", 0))), 4),
        })
    clips.sort(key=lambda c: -c["views"])
    return {
        "project_id": project_id,
        "publications_with_data": len(rows),
        "totals": {k: round(v, 1) for k, v in totals.items()},
        "by_platform": by_platform,
        "top_clips": clips[:10],
        "note": "仅聚合实际回流数据；无回流则为空，不伪造" if rows else "暂无回流数据",
    }