"""选段输出契约（04 号方案 §7.1，代码级铁律）。

流程：JSON Schema 强校验 → 时间戳越界检查 → 句/词边界吸附（偏移>2s 丢弃）
→ 时长硬约束 → quote 防幻觉（归一化子串，容错≤2字）→ NMS 去重 → 批内归一评分。
所有丢弃带原因，供任务回执审计。
"""
from __future__ import annotations

import jsonschema

from studio.contracts.text import normalize_text
from studio.engines.base import TranscriptResult

# 时长硬约束（毫秒），live_type 预设；项目级覆盖经 duration_range 传入
DURATION_PRESETS: dict[str, tuple[int, int]] = {
    "ecommerce": (15_000, 60_000),
    "knowledge": (30_000, 90_000),
    "story": (60_000, 180_000),
    "entertainment": (15_000, 90_000),
    "game": (15_000, 90_000),
    "talent": (15_000, 90_000),
}
DEFAULT_DURATION = (15_000, 120_000)

MAX_SNAP_OFFSET_MS = 2_000
MAX_QUOTE_EDITS = 2
IOU_THRESHOLD = 0.5
WEAK_THRESHOLD = 60.0

_WEIGHTS = {"hook": 0.35, "flow": 0.25, "value": 0.25, "trend": 0.15}

_SCORE_DIM = {"type": "number", "minimum": 0, "maximum": 10}

CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["start_ms", "end_ms", "quote", "title", "scores"],
                "properties": {
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 0},
                    "quote": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "clip_type": {"type": "string"},
                    "reason": {"type": "string"},
                    "caption": {"type": "string"},
                    "scores": {
                        "type": "object",
                        "required": ["hook", "flow", "value", "trend"],
                        "properties": {
                            "hook": _SCORE_DIM,
                            "flow": _SCORE_DIM,
                            "value": _SCORE_DIM,
                            "trend": _SCORE_DIM,
                            "reasons": {"type": "object"},
                        },
                    },
                },
            },
        }
    },
}


class ContractError(ValueError):
    """LLM 输出不符合契约（重试后仍失败 → 任务 failed，不静默）。"""


def validate_selection_output(out: dict) -> None:
    try:
        jsonschema.validate(out, CANDIDATE_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ContractError(f"选段输出不符合契约: {e.message} @ {list(e.absolute_path)}") from e


def weighted(scores: dict) -> float:
    return sum(float(scores.get(k, 0)) * w for k, w in _WEIGHTS.items())


def rank_normalize(candidates: list[dict]) -> None:
    """批内归一 40-99：分数是排序器不是预言机；<60 标 not_recommended。"""
    if not candidates:
        return
    vals = [weighted(c.get("scores", {})) for c in candidates]
    lo, hi = min(vals), max(vals)
    for c, v in zip(candidates, vals):
        c["rank_score"] = round(40 + 59 * (v - lo) / (hi - lo), 1) if hi > lo else 76.0
        c.setdefault("scores", {})["not_recommended"] = c["rank_score"] < WEAK_THRESHOLD


def _nearest(value: int, options: list[int]) -> tuple[int, int]:
    best = min(options, key=lambda o: abs(o - value))
    return best, abs(best - value)


def snap_boundaries(c: dict, tr: TranscriptResult) -> str | None:
    """句→词边界吸附（镜头级在 P2）。返回丢弃原因或 None；偏移写入 c['snap']。"""
    starts = [s.start_ms for s in tr.sentences]
    ends = [s.end_ms for s in tr.sentences]
    if tr.word_level and tr.words:
        starts = [w.start_ms for w in tr.words]
        ends = [w.end_ms for w in tr.words]
    if not starts or not ends:
        return "转写无边界可吸附"
    new_start, off_s = _nearest(int(c["start_ms"]), starts)
    new_end, off_e = _nearest(int(c["end_ms"]), ends)
    if off_s > MAX_SNAP_OFFSET_MS or off_e > MAX_SNAP_OFFSET_MS:
        return f"吸附偏移超限(>{MAX_SNAP_OFFSET_MS}ms): start={off_s} end={off_e}"
    c["snap"] = {"start_offset_ms": off_s, "end_offset_ms": off_e}
    c["start_ms"], c["end_ms"] = new_start, new_end
    if new_end <= new_start:
        return "吸附后区间为空"
    return None


def quote_ok(quote: str, full_text_norm: str, max_edits: int = MAX_QUOTE_EDITS) -> bool:
    """归一化子串匹配；容错≤max_edits 字（滑窗失配计数，防 LLM 编造）。"""
    q = normalize_text(quote)
    if not q:
        return False
    if q in full_text_norm:
        return True
    n, m = len(full_text_norm), len(q)
    if m > n + max_edits:
        return False
    for width in range(max(1, m - max_edits), min(n, m + max_edits) + 1):
        for i in range(n - width + 1):
            misses = 0
            for a, b in zip(q, full_text_norm[i : i + width]):
                if a != b:
                    misses += 1
                    if misses > max_edits:
                        break
            else:
                if misses + abs(width - m) <= max_edits:
                    return True
    return False


def _iou(a: dict, b: dict) -> float:
    inter = max(0, min(a["end_ms"], b["end_ms"]) - max(a["start_ms"], b["start_ms"]))
    union = max(a["end_ms"], b["end_ms"]) - min(a["start_ms"], b["start_ms"])
    return inter / union if union > 0 else 0.0


def nms(candidates: list[dict], threshold: float = IOU_THRESHOLD) -> tuple[list[dict], list[dict]]:
    """IoU>threshold 保留高分。返回 (保留, 丢弃带原因)。"""
    ordered = sorted(candidates, key=lambda c: weighted(c.get("scores", {})), reverse=True)
    kept: list[dict] = []
    dropped: list[dict] = []
    for c in ordered:
        if any(_iou(c, k) > threshold for k in kept):
            dropped.append({"title": c.get("title"), "reason": "重叠去重(NMS)"})
        else:
            kept.append(c)
    return kept, dropped


def process_candidates(
    out: dict,
    tr: TranscriptResult,
    *,
    live_type: str,
    duration_ms: int | None = None,
    duration_range: tuple[int, int] | None = None,
) -> tuple[list[dict], list[dict]]:
    """契约主入口。返回 (kept 含 rank_score, discarded 含原因)。

    Schema 不合法抛 ContractError（任务 failed）；单条违规只丢弃该条并留证。
    """
    validate_selection_output(out)
    lo, hi = duration_range or DURATION_PRESETS.get(live_type, DEFAULT_DURATION)
    limit = duration_ms if duration_ms is not None else (tr.sentences[-1].end_ms if tr.sentences else 0)
    full_text = normalize_text("".join(s.text for s in tr.sentences))

    kept: list[dict] = []
    discarded: list[dict] = []
    for c in out.get("candidates", []):
        c = dict(c)  # 不改 LLM 原始输出，留证用原始值
        why: str | None = None
        if not (0 <= c["start_ms"] < c["end_ms"] <= limit):
            why = "时间戳越界"
        if why is None:
            why = snap_boundaries(c, tr)
        if why is None and not (lo <= c["end_ms"] - c["start_ms"] <= hi):
            why = f"时长不符({(c['end_ms'] - c['start_ms']) / 1000:.1f}s, 要求{lo / 1000:.0f}-{hi / 1000:.0f}s)"
        if why is None and not quote_ok(c["quote"], full_text):
            why = "quote 非转写原文（防幻觉）"
        if why:
            discarded.append({"title": c.get("title"), "reason": why})
        else:
            kept.append(c)

    kept, nms_dropped = nms(kept)
    discarded.extend(nms_dropped)
    rank_normalize(kept)
    return kept, discarded