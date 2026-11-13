"""发布泵（P4-c，04 号方案 §11）。

匀速 + 抖动调度（防风控），**默认 dry-run**：只排期落 planned  publication，不真实上传。
真实上传 gated：social-auto-upload 许可证未核实 + 需平台账号授权 → 显式拒绝。
抖动确定性（seed），测试可复现。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.models import StudioArtifact, StudioCandidate, StudioProject, StudioPublication


class PublishBlocked(RuntimeError):
    pass


def schedule_times(
    n: int, *, interval_s: int = 300, jitter_s: int = 60,
    start: datetime | None = None, seed: int = 0,
) -> list[datetime]:
    """匀速间隔 + 确定性抖动（±jitter_s）。n 个发布时间点。"""
    base = start or datetime.now()
    rng = random.Random(seed)
    out: list[datetime] = []
    t = base
    for i in range(n):
        if i > 0:
            t = t + timedelta(seconds=interval_s)
        jitter = rng.randint(-jitter_s, jitter_s) if jitter_s > 0 else 0
        out.append(t + timedelta(seconds=jitter))
    return out


def assert_real_publish_allowed() -> None:
    """真实发布前置门禁：许可证 + 授权缺一不可（当前均未满足）。"""
    raise PublishBlocked(
        "真实上传已禁用：social-auto-upload 许可证未核实（04 §15）+ 需平台账号授权。"
        "核实许可证并配置授权后，经显式授权方可启用；当前仅 dry-run 排期。"
    )


def plan_pump(
    sf: sessionmaker[Session], project_id: str, *,
    interval_s: int = 300, jitter_s: int = 60, seed: int = 0,
    start: datetime | None = None, dry_run: bool = True,
) -> dict:
    if not dry_run:
        assert_real_publish_allowed()  # 真实发布一律 gated
    with sf() as s:
        if s.get(StudioProject, project_id) is None:
            raise PublishBlocked(f"项目不存在: {project_id}")
        adopted = s.execute(
            select(StudioCandidate).where(
                StudioCandidate.project_id == project_id,
                StudioCandidate.layer == "clips1",
                StudioCandidate.adopted.is_(True),
            )
        ).scalars().all()
        aids = [c.id for c in adopted]
        mp4s = s.execute(
            select(StudioArtifact).where(
                StudioArtifact.project_id == project_id,
                StudioArtifact.kind == "mp4",
                StudioArtifact.candidate_id.in_(aids) if aids else False,
            )
        ).scalars().all() if aids else []
        items = [(a.id, a.candidate_id, a.platform or "douyin", a.content_id) for a in mp4s]
        times = schedule_times(len(items), interval_s=interval_s, jitter_s=jitter_s, start=start, seed=seed)
        scheduled = []
        for (aid, cid, platform, content_id), t in zip(items, times):
            pub = StudioPublication(
                artifact_id=aid, platform=platform, account=None,
                published_at=t, status="planned",
            )
            s.add(pub)
            s.flush()
            scheduled.append({
                "publication_id": pub.id, "artifact_id": aid, "candidate_id": cid,
                "platform": platform, "content_id": content_id,
                "scheduled_at": t.isoformat(timespec="seconds"), "status": "planned",
            })
        s.commit()
    return {
        "project_id": project_id, "dry_run": True, "count": len(scheduled),
        "interval_s": interval_s, "jitter_s": jitter_s, "scheduled": scheduled,
        "note": "dry-run 排期，未真实上传；真实发布需许可证核实+授权+显式启用",
    }