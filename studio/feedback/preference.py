"""偏好回流（P3-6，04 号方案）：采纳/否决记录 → 选段 prompt 偏置。

越用越准的飞轮：人工审核决定（clips1 adopted/rejected）是最高质量偏好信号。
- 按 clip_type 统计采纳率（样本数明示，小样本不夸大）
- 否决理由高频词提取（保守：仅统计，不自动改契约铁律）
- 生成 prompt 偏置块注入 analyze（铁律后缀不受影响）
"""
from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.models import StudioCandidate

_STOP = {"的", "了", "是", "不", "太", "很", "有点", "这个", "那个", "感觉", "觉得"}


def preference_summary(
    sf: sessionmaker[Session], *, live_type: str | None = None, limit: int = 500,
) -> dict:
    with sf() as s:
        q = select(StudioCandidate).where(StudioCandidate.layer == "clips1")
        rows = s.execute(q.order_by(StudioCandidate.created_at.desc()).limit(limit)).scalars().all()
        if live_type:
            from studio.models import StudioProject
            pids = {
                p.id for p in s.execute(
                    select(StudioProject).where(StudioProject.live_type == live_type)
                ).scalars().all()
            }
            rows = [r for r in rows if r.project_id in pids]
        by_type: dict[str, dict] = {}
        reasons: Counter = Counter()
        for r in rows:
            t = r.clip_type or "未分类"
            stat = by_type.setdefault(t, {"adopted": 0, "rejected": 0})
            if r.adopted:
                stat["adopted"] += 1
            else:
                stat["rejected"] += 1
                for w in re.findall(r"[\u4e00-\u9fff]{2,6}", r.rejected_reason or ""):
                    if w not in _STOP:
                        reasons[w] += 1
    types = []
    for t, st in sorted(by_type.items(), key=lambda kv: -(kv[1]["adopted"] + kv[1]["rejected"])):
        n = st["adopted"] + st["rejected"]
        types.append({
            "clip_type": t, "adopted": st["adopted"], "rejected": st["rejected"],
            "adopt_rate": round(st["adopted"] / n, 3) if n else 0.0, "n": n,
        })
    return {
        "total_decisions": len(rows),
        "by_type": types,
        "top_reject_keywords": [{"word": w, "count": c} for w, c in reasons.most_common(5)],
        "note": "小样本偏好仅方向性参考；不改变契约铁律",
    }


def preference_prompt_block(summary: dict) -> str:
    """注入 analyze 的偏置块；无决策记录返回空串（不注入噪声）。"""
    if not summary.get("total_decisions"):
        return ""
    parts = ["历史人工审核偏好（方向性参考，样本量见 n）："]
    reliable = [t for t in summary["by_type"] if t["n"] >= 3]
    if reliable:
        good = [t["clip_type"] for t in reliable if t["adopt_rate"] >= 0.6]
        bad = [t["clip_type"] for t in reliable if t["adopt_rate"] <= 0.3]
        if good:
            parts.append(f"优先类型：{'、'.join(good)}")
        if bad:
            parts.append(f"谨慎类型：{'、'.join(bad)}")
    kws = summary.get("top_reject_keywords") or []
    if kws:
        parts.append("常见否决原因关键词：" + "、".join(k["word"] for k in kws[:3]))
    return "\n".join(parts) if len(parts) > 1 else ""