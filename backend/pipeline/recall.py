"""信号召回层（OP-4）：把"哪里热闹"的免费信号变成 LLM 的候选区间。

三层漏斗的第一层（《10-清单》3.4-1）：先用零 LLM 成本的信号把长视频
粗筛到 10-20%，LLM 只精读候选区间的字幕——云端模型省 token，本地小模型
顺带解决上下文装不下的问题。

算法移植自 auto-slice-video（MIT，已核实可引入）：每秒计数 → 滑窗密度 →
降序 → 重叠约束贪心（与已选区间重叠超阈值的丢弃，保证 top-K 不是同一波高能）。

降级保护（4-5）：信号源稀疏（弹幕太少/没有）时返回 None，调用方回退全量
路径——弹幕信号只做可选召回通道，不是唯一判定；访谈/知识类素材不受影响。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 召回开关的默认参数
DEFAULT_WINDOW_SEC = 90.0        # 候选区间长度：按一条短视频 60-180s 的目标量级
DEFAULT_STEP_SEC = 10.0          # 滑窗步长
DEFAULT_MAX_OVERLAP_SEC = 45.0   # 候选间最大重叠：超过则视为同一波高能
DEFAULT_PAD_SEC = 45.0           # 候选区间向两侧扩展，给 LLM 留上下文
DEFAULT_MIN_EVENTS = 30          # 事件总数低于此值视为信号稀疏，回退全量路径


def density_windows(
    events_per_second: dict[int, int],
    *,
    duration_sec: float,
    window_sec: float = DEFAULT_WINDOW_SEC,
    step_sec: float = DEFAULT_STEP_SEC,
    max_overlap_sec: float = DEFAULT_MAX_OVERLAP_SEC,
    top_k: int | None = None,
) -> list[tuple[float, float]]:
    """滑窗密度 + 重叠约束贪心，返回按密度降序的高能区间 [(start, end)]。

    top_k 缺省按"每 5 分钟一个名额"伸缩（Clips Kitty 的预算公式 max(12, 时长//300)
    的保守版），避免长视频后段的峰值拿不到名额。
    """
    if not events_per_second or duration_sec <= 0:
        return []
    if top_k is None:
        top_k = max(6, min(30, int(duration_sec // 300)))
    if window_sec > duration_sec:
        window_sec = duration_sec

    times = sorted(events_per_second)
    candidates: list[tuple[float, int]] = []
    for start in range(times[0], max(0, times[-1] - int(window_sec)) + 1, max(1, int(step_sec))):
        end = min(start + window_sec, duration_sec)
        density = sum(count for sec, count in events_per_second.items() if start <= sec < end)
        if density > 0:
            candidates.append((float(start), density))
    candidates.sort(key=lambda pair: pair[1], reverse=True)

    picked: list[tuple[float, float]] = []
    for start, _density in candidates:
        end = min(start + window_sec, duration_sec)
        if any(min(end, other_end) - max(start, other_start) > max_overlap_sec
               for other_start, other_end in picked):
            continue  # 与已选候选高度重叠：同一波高能，不重复占名额
        picked.append((start, end))
        if len(picked) >= top_k:
            break
    return picked


def subtitle_events_per_second(srt_entries: list[dict[str, Any]]) -> dict[int, int]:
    """零依赖信号源：字幕行密度（每行按字数加权）。

    弹幕缺失时的兜底信号——说话密度高的区段大概率也是内容密集区段。
    """
    from .quality import to_seconds
    counts: dict[int, int] = {}
    for entry in srt_entries:
        text = str(entry.get('text') or '').strip()
        if not text:
            continue
        second = int(to_seconds(entry.get('start_time')))
        counts[second] = counts.get(second, 0) + max(1, len(text) // 10)
    return counts


def to_row_spans(
    picked: list[tuple[float, float]],
    rows: list[dict[str, Any]],
    *,
    pad_sec: float = DEFAULT_PAD_SEC,
    total_sec: float | None = None,
) -> list[tuple[int, int]]:
    """高能时间区间 → 字幕行号区间 [first, last]，向两侧扩展 pad_sec 并合并重叠。"""
    if not picked:
        return []
    total = total_sec if total_sec is not None else (rows[-1]['end'] if rows else 0.0)
    spans: list[tuple[float, float]] = []
    for start, end in picked:
        spans.append((max(0.0, start - pad_sec), min(total, end + pad_sec)))
    spans.sort()

    out: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        first = next((i for i, row in enumerate(rows) if row['end'] > span_start), None)
        last = next((i for i in range(len(rows) - 1, -1, -1) if rows[i]['start'] < span_end), None)
        if first is None or last is None or first > last:
            continue
        if out and first <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], last))  # 相邻/重叠区间合并
        else:
            out.append((first, last))
    return out


def recalled_spans(
    rows: list[dict[str, Any]],
    *,
    duration_sec: float,
    danmaku_per_second: dict[int, int] | None = None,
    srt_entries: list[dict[str, Any]] | None = None,
    window_sec: float = DEFAULT_WINDOW_SEC,
    step_sec: float = DEFAULT_STEP_SEC,
    max_overlap_sec: float = DEFAULT_MAX_OVERLAP_SEC,
    min_events: int = DEFAULT_MIN_EVENTS,
    top_ratio: float = 0.25,
) -> Optional[list[tuple[int, int]]]:
    """召回入口：返回候选区间的行号列表；信号不足返回 None（调用方回退全量）。

    信号优先级：弹幕（真实观众投票）> 字幕行密度（零依赖兜底）。
    top_ratio 控制召回强度：候选区间总时长约占素材的 top_ratio（默认 25%）。
    """
    signal = danmaku_per_second if danmaku_per_second else subtitle_events_per_second(srt_entries or [])
    total_events = sum(signal.values())
    if total_events < min_events or duration_sec <= 0:
        logger.info('召回层跳过：信号稀疏（events=%d < %d），回退全量路径', total_events, min_events)
        return None

    picked = density_windows(signal, duration_sec=duration_sec, window_sec=window_sec,
                             step_sec=step_sec, max_overlap_sec=max_overlap_sec)
    if not picked:
        return None
    spans = to_row_spans(picked, rows, total_sec=duration_sec)
    if not spans:
        return None

    # 召回强度收敛：候选区间行数占比超过 top_ratio 时，只保留密度最高的前若干区间
    span_seconds = [(rows[last]['end'] - rows[first]['start'], first, last) for first, last in spans]
    total = max(rows[-1]['end'] - rows[0]['start'], 1.0)
    budget = total * top_ratio
    kept: list[tuple[int, int]] = []
    used = 0.0
    for seconds, first, last in sorted(span_seconds, reverse=True):
        if used + seconds > budget and kept:
            break
        kept.append((first, last))
        used += seconds
    kept.sort()
    kept_seconds = sum(rows[last]['end'] - rows[first]['start'] for first, last in kept)
    logger.info('召回层：%d 个候选区间（%.0f%% 素材时长），信号事件 %d 条',
                len(kept), 100.0 * kept_seconds / max(total, 1.0), total_events)
    return kept
