"""三档审核闸门（P4-b，04 号方案 §11）。

工厂模式下的自动分流，但默认留人工（AI 出候选、人终审理念）：
- hard_reject：合规阻断（授权过期/AIGC 未标识）或已否决 → 绝不发布
- auto_pass：人工已采纳 + rank≥门槛 + 非弱片 + 无阻断 → 可进发布泵
- human：其余一律留人工（默认档）
"""
from __future__ import annotations

AUTO_PASS_MIN_RANK = 85.0


def gate_decision(
    candidate: dict, blockers: list[str], *, auto_pass_min_rank: float = AUTO_PASS_MIN_RANK,
) -> dict:
    reasons: list[str] = []
    if blockers:
        return {"tier": "hard_reject", "reasons": [f"合规阻断: {b}" for b in blockers]}
    if candidate.get("adopted") is False:
        return {"tier": "hard_reject", "reasons": ["人工已否决"]}
    weak = bool((candidate.get("scores") or {}).get("not_recommended"))
    rank = candidate.get("rank_score") or 0.0
    if candidate.get("adopted") and rank >= auto_pass_min_rank and not weak:
        return {"tier": "auto_pass", "reasons": [f"已采纳且 rank {rank}≥{auto_pass_min_rank}，非弱片"]}
    if not candidate.get("adopted"):
        reasons.append("尚未人工采纳")
    if rank < auto_pass_min_rank:
        reasons.append(f"rank {rank}<{auto_pass_min_rank}")
    if weak:
        reasons.append("弱片（不建议发布）")
    return {"tier": "human", "reasons": reasons or ["默认留人工"]}


def gate_project(candidates: list[dict], blockers: list[str], **kw) -> dict:
    decisions = [{"candidate_id": c["id"], **gate_decision(c, blockers, **kw)} for c in candidates]
    return {
        "decisions": decisions,
        "auto_pass": [d["candidate_id"] for d in decisions if d["tier"] == "auto_pass"],
        "human": [d["candidate_id"] for d in decisions if d["tier"] == "human"],
        "hard_reject": [d["candidate_id"] for d in decisions if d["tier"] == "hard_reject"],
        "note": "默认留人工；auto_pass 仍需发布泵 dry-run/授权后才真实发布",
    }