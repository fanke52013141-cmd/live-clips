"""复审环 judge → repair → rejudge（04 号方案 §7.2，移植 openclip 思路）。

- judge：独立成段？0-1 分 + 理由。
- 不合格 → repair：LLM 只允许扩/缩边界到邻近句界；代码强制校验
  （必须精确落在句界、距原边界至多一个相邻句、时长约束、quote 仍有效）。
- rejudge：仍不合格 → 丢弃。每轮输入输出全落回执（审计可回放）。
- 开关默认开（Settings.review_loop），成本敏感可关。
"""
from __future__ import annotations

from studio.contracts.selection import (
    DURATION_PRESETS,
    DEFAULT_DURATION,
    quote_ok,
)
from studio.contracts.text import normalize_text
from studio.engines.base import LLMClient, TranscriptResult

JUDGE_SYSTEM = (
    "你是切片复审官（judge）。判断候选片段脱离直播上下文后是否独立成段："
    "有开头钩子、有展开、有收尾，观众不看前后文也能看懂。只输出 JSON。"
)
REPAIR_SYSTEM = (
    "你是边界修复器（repair）。候选片段不独立成段，仅允许把入点/出点扩或缩到"
    "邻近句子边界来补全结构，不得改动内容。只输出 JSON。"
)

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["standalone", "score", "reason"],
    "properties": {
        "standalone": {"type": "boolean"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
}
REPAIR_SCHEMA = {
    "type": "object",
    "required": ["start_ms", "end_ms", "reason"],
    "properties": {
        "start_ms": {"type": "integer", "minimum": 0},
        "end_ms": {"type": "integer", "minimum": 0},
        "reason": {"type": "string"},
    },
}

PASS_SCORE = 0.6


def _judge_input(c: dict, tr: TranscriptResult) -> str:
    lines = [
        f"{s.start_ms}|{s.end_ms}|{s.text}"
        for s in tr.sentences
        if s.end_ms >= c["start_ms"] - 5000 and s.start_ms <= c["end_ms"] + 5000
    ]
    return (
        f"候选: start={c['start_ms']} end={c['end_ms']} quote={c.get('quote', '')}\n"
        f"上下文时间轴:\n" + "\n".join(lines)
    )


def _passes(verdict: dict) -> bool:
    return bool(verdict.get("standalone")) and float(verdict.get("score", 0)) >= PASS_SCORE


def _validate_repair(
    old: dict, new_start: int, new_end: int, tr: TranscriptResult,
    lo: int, hi: int, full_text: str,
) -> str | None:
    """代码级校验修复边界。返回拒绝原因或 None。"""
    starts = [s.start_ms for s in tr.sentences]
    ends = [s.end_ms for s in tr.sentences]
    if new_start not in starts or new_end not in ends:
        return "修复边界未落在句界"
    if new_end <= new_start:
        return "修复后区间为空"
    # 距原边界至多一个相邻句
    i_old = min(range(len(starts)), key=lambda i: abs(starts[i] - old["start_ms"]))
    j_old = min(range(len(ends)), key=lambda j: abs(ends[j] - old["end_ms"]))
    i_new, j_new = starts.index(new_start), ends.index(new_end)
    if abs(i_new - i_old) > 1 or abs(j_new - j_old) > 1:
        return "修复超出相邻句范围"
    if not (lo <= new_end - new_start <= hi):
        return f"修复后时长不符({(new_end - new_start) / 1000:.1f}s)"
    if not quote_ok(old.get("quote", ""), full_text):
        return "修复后 quote 失效"
    return None


def review_candidates(
    kept: list[dict],
    tr: TranscriptResult,
    llm: LLMClient,
    *,
    live_type: str,
    duration_range: tuple[int, int] | None = None,
    enabled: bool = True,
) -> tuple[list[dict], list[dict], list[dict]]:
    """返回 (保留, 丢弃带原因, 复审日志)。enabled=False 时原样返回、零 LLM 调用。"""
    if not enabled or not kept:
        return list(kept), [], []
    lo, hi = duration_range or DURATION_PRESETS.get(live_type, DEFAULT_DURATION)
    full_text = normalize_text("".join(s.text for s in tr.sentences))

    out_kept: list[dict] = []
    discarded: list[dict] = []
    log: list[dict] = []
    for c in kept:
        rounds: list[dict] = []
        title = c.get("title")
        inp = _judge_input(c, tr)
        verdict = llm.chat_json(JUDGE_SYSTEM, inp, JUDGE_SCHEMA)
        rounds.append({"action": "judge", "output": verdict})
        if _passes(verdict):
            out_kept.append(c)
            log.append({"title": title, "verdict": "pass", "rounds": rounds})
            continue

        repair_inp = inp + f"\n复审理由: {verdict.get('reason', '')}\n请给出修复后的 start_ms/end_ms（必须为句界）。"
        fix = llm.chat_json(REPAIR_SYSTEM, repair_inp, REPAIR_SCHEMA)
        rounds.append({"action": "repair", "output": fix})
        why = _validate_repair(c, int(fix["start_ms"]), int(fix["end_ms"]), tr, lo, hi, full_text)
        if why:
            discarded.append({"title": title, "reason": f"复审修复被拒: {why}"})
            log.append({"title": title, "verdict": "repair_rejected", "rounds": rounds})
            continue

        c2 = dict(c)
        c2["repair"] = {"old_start_ms": c["start_ms"], "old_end_ms": c["end_ms"]}
        c2["start_ms"], c2["end_ms"] = int(fix["start_ms"]), int(fix["end_ms"])
        verdict2 = llm.chat_json(JUDGE_SYSTEM, _judge_input(c2, tr), JUDGE_SCHEMA)
        rounds.append({"action": "rejudge", "output": verdict2})
        if _passes(verdict2):
            out_kept.append(c2)
            log.append({"title": title, "verdict": "repaired", "rounds": rounds})
        else:
            discarded.append({"title": title, "reason": "复审仍不合格（半截话）"})
            log.append({"title": title, "verdict": "fail", "rounds": rounds})
    return out_kept, discarded, log