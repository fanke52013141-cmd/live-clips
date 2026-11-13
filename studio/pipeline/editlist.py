"""剪辑决策纯逻辑（04 号方案 §10.5/10.6，P2-2/P2-3）。

无 IO、可单测：
- detect_breaths：去气口三重判定（词间隙>0.6s ∧ silencedetect 确认 ∧ 能量 VAD 无人声）
- detect_fillers：口头禅保守剪除（单字语气词+两侧停顿，默认关）
- subtract_intervals / build_segments：段列表（输出顺序=段顺序）
- remap_ms：源时间轴 → 输出时间轴（字幕重映射用）

铁律：
- 三条件缺一不剪；笑声/尾音有能量 → 能量 VAD 拒绝（不误删）
- 每个剪除决定带理由，供回执审计
- 能量 VAD 为 v1（RMS 阈值），神经 VAD 需模型批准后升级，回执如实标注
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from studio.engines.base import Word

# 保守口头禅词表：仅单字语气词，不含「那个/就是」等可能有语义的词
FILLER_VOCAB = frozenset({"嗯", "呃", "唔"})


@dataclass(frozen=True)
class Cut:
    start_ms: int
    end_ms: int
    kind: str  # breath | filler
    reason: str


def detect_breaths(
    words: list[Word] | tuple[Word, ...],
    clip_start_ms: int,
    clip_end_ms: int,
    silences: list[tuple[int, int]],
    rms_fn: Callable[[float, float], float],
    *,
    min_gap_ms: int = 600,
    keep_breath_ms: int = 250,
    energy_threshold: float = 0.01,
) -> list[Cut]:
    """三重判定去气口。silences 为 silencedetect 实测区间（源绝对 ms）。

    rms_fn(start_s, end_s) 返回间隙 RMS（能量 VAD v1）。
    保留呼吸口：间隙两端各留 keep_breath_ms/2。
    """
    cuts: list[Cut] = []
    inside = [w for w in words if w.end_ms > clip_start_ms and w.start_ms < clip_end_ms]
    inside = sorted(inside, key=lambda w: w.start_ms)
    for prev, nxt in zip(inside, inside[1:]):
        gap_start, gap_end = prev.end_ms, nxt.start_ms
        gap = gap_end - gap_start
        if gap <= min_gap_ms:
            continue  # 条件一：间隙时长
        covered = sum(
            max(0, min(gap_end, se) - max(gap_start, ss)) for ss, se in silences
        )
        if covered < 0.8 * gap:
            continue  # 条件二：silencedetect 确认≥80%覆盖
        rms = rms_fn(gap_start / 1000, gap_end / 1000)
        if rms >= energy_threshold:
            continue  # 条件三：能量 VAD——有声音（笑声/尾音/环境人声）不剪
        half = keep_breath_ms // 2
        cs, ce = gap_start + half, gap_end - half
        if ce <= cs:
            continue
        cuts.append(Cut(cs, ce, "breath",
                        f"间隙{gap}ms 静音覆盖{covered}ms RMS={rms:.4f} 三重判定通过"))
    return cuts


def detect_fillers(
    words: list[Word] | tuple[Word, ...],
    clip_start_ms: int,
    clip_end_ms: int,
    *,
    min_side_gap_ms: int = 250,
    max_filler_ms: int = 600,
) -> list[Cut]:
    """保守口头禅剪除：仅独立单字语气词（两侧均有停顿），语流中的不剪。"""
    cuts: list[Cut] = []
    inside = sorted(
        (w for w in words if w.end_ms > clip_start_ms and w.start_ms < clip_end_ms),
        key=lambda w: w.start_ms,
    )
    for i, w in enumerate(inside):
        if w.text not in FILLER_VOCAB or len(w.text) != 1:
            continue
        if w.end_ms - w.start_ms > max_filler_ms:
            continue
        left_gap = w.start_ms - (inside[i - 1].end_ms if i > 0 else clip_start_ms)
        right_gap = (inside[i + 1].start_ms if i + 1 < len(inside) else clip_end_ms) - w.end_ms
        if left_gap < min_side_gap_ms or right_gap < min_side_gap_ms:
            continue  # 语流中的语气词不剪（保守）
        cuts.append(Cut(w.start_ms, w.end_ms, "filler",
                        f"独立语气词「{w.text}」两侧停顿{left_gap}/{right_gap}ms"))
    return cuts


def subtract_intervals(
    base: tuple[int, int], cuts: list[Cut] | list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从 base 区间减去剪除区间，返回保留段（源时间轴，升序不重叠）。"""
    spans = [(c.start_ms, c.end_ms) if isinstance(c, Cut) else c for c in cuts]
    kept = [base]
    for cs, ce in sorted(spans):
        nxt: list[tuple[int, int]] = []
        for ks, ke in kept:
            if ce <= ks or cs >= ke:
                nxt.append((ks, ke))
                continue
            if ks < cs:
                nxt.append((ks, cs))
            if ce < ke:
                nxt.append((ce, ke))
        kept = nxt
    return kept


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    kind: str  # flash | cold_open | body

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def build_segments(
    clip_start_ms: int,
    clip_end_ms: int,
    cuts: list[Cut],
    *,
    cold_open: tuple[int, int] | None = None,
    cold_open_jumpcut: bool = False,
    flash_forward: tuple[int, int] | None = None,
) -> list[Segment]:
    """输出顺序 = 段顺序：[flash] + [cold_open] + 正文保留段。

    cold_open/flash 区间必须在切片内且非空，否则 ValueError（上层捕获后自动跳过）。
    jumpcut=True 时正文中挖掉钩子原位（避免重复）；默认原位保留。
    """
    for name, span in (("cold_open", cold_open), ("flash_forward", flash_forward)):
        if span is not None:
            s, e = span
            if not (clip_start_ms <= s < e <= clip_end_ms):
                raise ValueError(f"{name} 区间非法或越界: {span}不在[{clip_start_ms},{clip_end_ms}]")
    body_cuts = list(cuts)
    if cold_open is not None and cold_open_jumpcut:
        body_cuts.append(Cut(cold_open[0], cold_open[1], "cold_open", "跳剪原位"))
    kept = subtract_intervals((clip_start_ms, clip_end_ms), body_cuts)
    segs: list[Segment] = []
    if flash_forward is not None:
        segs.append(Segment(flash_forward[0], flash_forward[1], "flash"))
    if cold_open is not None:
        segs.append(Segment(cold_open[0], cold_open[1], "cold_open"))
    segs.extend(Segment(s, e, "body") for s, e in kept)
    return segs


def remap_ms(t_ms: int, segments: list[Segment]) -> int | None:
    """源时间轴时刻 → 输出时间轴；落在被剪区间返回 None。

    同一源时刻可能出现在多个段（cold_open 复制）：取第一个匹配段。
    """
    acc = 0
    for seg in segments:
        if seg.start_ms <= t_ms < seg.end_ms:
            return acc + (t_ms - seg.start_ms)
        acc += seg.duration_ms
    return None