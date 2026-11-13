"""钩子决策树（04 号方案 §7.3，P2-4）。

铁律：决策由代码做，LLM 只填内容。
- 原生钩子已用（cold_open 定位成功）→ 不叠 L1（避免双钩子互相稀释）
- 否则 L1 必做；写不出悬念句 → 自动不加（宁缺毋滥，引擎返回 None）
- L2 按开关 + rank 门槛（默认 80）；L3/L4 在 P3，且 L4 需人工确认费用
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HookDecision:
    l1: bool = False
    l2: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)


def decide_hooks(
    *,
    native_used: bool,
    l1_enabled: bool = True,
    l2_enabled: bool = False,
    rank_score: float | None = None,
    l2_rank_threshold: float = 80.0,
) -> HookDecision:
    reasons: list[str] = []
    l1 = False
    if native_used:
        reasons.append("原生钩子已用，L1 不叠加")
    elif l1_enabled:
        l1 = True
    else:
        reasons.append("L1 开关关闭")
    l2 = False
    if l2_enabled:
        if (rank_score or 0.0) >= l2_rank_threshold:
            l2 = True
        else:
            reasons.append(f"L2 rank {rank_score} 未达门槛 {l2_rank_threshold}")
    return HookDecision(l1=l1, l2=l2, reasons=tuple(reasons))


def clamp_hook_text(text: str, max_chars: int = 20) -> str | None:
    """悬念句≤20字；空白/超长截断后仍空 → None（写不出就不加）。"""
    t = "".join((text or "").split())
    if not t:
        return None
    return t[:max_chars]