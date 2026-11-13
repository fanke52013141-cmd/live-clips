"""信号上下文与共振加成（04 号方案 P1-5，吸收 hotclip/OpusClip 多信号融合思路）。

- summarize_tracks：给 LLM 的信号摘要（每路 top 桶 + 时间范围），不是原始数组轰炸。
- resonance：多路信号「同桶共振」才可信——候选区间内 ≥2 路信号同时处于高位
  （≥该路 p80 且 >0）才给加成；单路峰值不加（防单信号噪声）。
- 加成上限 +8 分，rank_score 封顶 99；证据写入 scores.signal_evidence（审计）。
"""
from __future__ import annotations


def _p80(values: list[float]) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    return sv[int(0.8 * (len(sv) - 1))]


def summarize_tracks(tracks: dict, top_n: int = 5) -> str:
    """LLM 上下文用摘要：每路信号的高能桶（时间范围+归一值）。"""
    if not tracks:
        return "无可用信号（已降维）"
    lines: list[str] = []
    for name, track in tracks.items():
        bucket_ms = track.get("bucket_ms", 5000)
        for series_name, values in track.get("series", {}).items():
            vals = list(values)
            if not vals or max(vals) <= 0:
                continue
            idx = sorted(range(len(vals)), key=lambda i: vals[i], reverse=True)[:top_n]
            idx.sort()
            peaks = ", ".join(
                f"{i * bucket_ms // 1000}-{(i + 1) * bucket_ms // 1000}s:{vals[i]:.2f}"
                for i in idx
            )
            lines.append(f"{name}/{series_name} 高能段: {peaks}")
    return "\n".join(lines) if lines else "无可用信号（已降维）"


def _high_buckets(track: dict) -> dict[str, set[int]]:
    """每路序列的高位桶集合（≥p80 且 >0）。"""
    out: dict[str, set[int]] = {}
    for series_name, values in track.get("series", {}).items():
        vals = list(values)
        th = _p80(vals)
        out[series_name] = {i for i, v in enumerate(vals) if v > 0 and v >= th}
    return out


def apply_resonance(candidates: list[dict], tracks: dict) -> None:
    """就地给候选加成并写证据。无信号时零改动（A/B 对照的「无信号」臂）。"""
    if not tracks or not candidates:
        return
    bucket_ms = next(iter(tracks.values())).get("bucket_ms", 5000)
    high: dict[str, set[int]] = {}
    for track in tracks.values():
        for series_name, buckets in _high_buckets(track).items():
            high.setdefault(series_name, set()).update(buckets)
    for c in candidates:
        b0 = c["start_ms"] // bucket_ms
        b1 = max(b0, (c["end_ms"] - 1) // bucket_ms)
        evidence: list[dict] = []
        for b in range(b0, b1 + 1):
            hits = sorted(name for name, buckets in high.items() if b in buckets)
            if len(hits) >= 2:  # 同桶 ≥2 路共振才可信
                evidence.append({"bucket_start_ms": b * bucket_ms, "signals": hits})
        bonus = min(8.0, 2.0 * len(evidence))
        scores = c.setdefault("scores", {})
        scores["resonance_bonus"] = bonus
        scores["signal_evidence"] = evidence
        if bonus and c.get("rank_score") is not None:
            c["rank_score"] = min(99.0, round(c["rank_score"] + bonus, 1))