"""选段契约测试（P1-1 验收：schema/吸附/时长/quote 防幻觉/NMS/归一/文本归一化）。"""
import pytest

from studio.contracts.selection import (
    ContractError,
    nms,
    process_candidates,
    quote_ok,
    rank_normalize,
    snap_boundaries,
    validate_selection_output,
)
from studio.contracts.text import cjk_join, normalize_text
from studio.engines.base import Sentence, TranscriptResult, Word


def make_tr(word_level=True):
    """3 句转写（500ms/字）：0-5000 / 5400-9900 / 10300-18300。"""
    texts = ["今天价格三十九块九。", "拍一发三送运费险。", "只有今天这个机制下播恢复原价啊。"]
    words, sentences = [], []
    t = 0
    for text in texts:
        start = t
        for ch in text:
            words.append(Word(ch, t, t + 500))
            t += 500
        sentences.append(Sentence(text, start, t))
        t += 400
    return TranscriptResult(
        words=tuple(words) if word_level else (),
        sentences=tuple(sentences),
        engine="t",
        model="t",
        params_hash="t",
        word_level=word_level,
    )


def cand(start=0, end=16000, quote="今天价格三十九块九", **kw):
    c = {
        "start_ms": start,
        "end_ms": end,
        "quote": quote,
        "title": "测试标题",
        "scores": {"hook": 9, "flow": 8, "value": 9, "trend": 7},
    }
    c.update(kw)
    return c


# ---------- schema ----------

def test_schema_rejects_missing_fields():
    with pytest.raises(ContractError):
        validate_selection_output({"candidates": [{"start_ms": 0, "end_ms": 1}]})


def test_schema_rejects_out_of_range_score():
    c = cand()
    c["scores"]["hook"] = 11
    with pytest.raises(ContractError):
        validate_selection_output({"candidates": [c]})


def test_schema_accepts_empty_candidates():
    validate_selection_output({"candidates": []})


# ---------- 吸附 ----------

def test_snap_to_word_boundary():
    tr = make_tr()
    c = cand(start=120, end=15900)  # 最近词边界：start 0 / end 15800
    assert snap_boundaries(c, tr) is None
    assert c["start_ms"] == 0 and c["end_ms"] == 15800
    assert c["snap"]["start_offset_ms"] == 120
    assert c["snap"]["end_offset_ms"] == 100


def test_snap_offset_over_2s_discarded():
    # 句级转写：end=16000 距最近句界 18300 差 2300ms > 2000 → 丢弃
    tr = make_tr(word_level=False)
    kept, disc = process_candidates(
        {"candidates": [cand(start=0, end=16000)]}, tr, live_type="ecommerce"
    )
    assert kept == [] and "吸附偏移超限" in disc[0]["reason"]


def test_snap_sentence_level_when_no_words():
    tr = make_tr(word_level=False)
    c = cand(start=100, end=20100)  # 最近句界 0 / 18300（偏移 1800 ≤ 2000）
    assert snap_boundaries(c, tr) is None
    assert c["start_ms"] == 0 and c["end_ms"] == 18300


# ---------- quote 防幻觉 ----------

def test_quote_exact_and_whitespace_normalized():
    assert quote_ok("三十九块九", normalize_text("今 天 三 十 九 块 九 啊"))


def test_quote_fuzzy_two_edits():
    # 1 字差异（九→八）应通过容错
    assert quote_ok("三十九块八", normalize_text("今天三十九块九啊"))
    # 2 字差异应通过
    assert quote_ok("三十八块八", normalize_text("今天三十九块九啊"))


def test_quote_fabricated_rejected():
    assert not quote_ok("这句话转写里根本没有", normalize_text("今天价格三十九块九拍一发三"))


def test_quote_three_edits_rejected():
    assert not quote_ok("甲乙丙丁戊", normalize_text("今天价格三十九块九"))


# ---------- 时长约束 ----------

def test_duration_too_short_discarded():
    tr = make_tr()
    kept, disc = process_candidates(
        {"candidates": [cand(start=0, end=5000)]}, tr, live_type="ecommerce"
    )
    assert kept == [] and "时长不符" in disc[0]["reason"]


def test_duration_override():
    tr = make_tr()
    kept, _ = process_candidates(
        {"candidates": [cand(start=0, end=5000)]},
        tr,
        live_type="ecommerce",
        duration_range=(1000, 60000),
    )
    assert len(kept) == 1


def test_timestamp_out_of_range():
    tr = make_tr()
    kept, disc = process_candidates(
        {"candidates": [cand(start=0, end=999_000)]}, tr, live_type="ecommerce"
    )
    assert kept == [] and disc[0]["reason"] == "时间戳越界"


# ---------- NMS ----------

def test_nms_keeps_higher_score():
    a = cand(start=0, end=16000)
    b = cand(start=1000, end=16000, title="低分重叠")
    b["scores"] = {"hook": 3, "flow": 3, "value": 3, "trend": 3}
    kept, dropped = nms([b, a])
    assert len(kept) == 1 and kept[0]["title"] == "测试标题"
    assert dropped[0]["reason"] == "重叠去重(NMS)"


def test_nms_keeps_disjoint():
    a = cand(start=0, end=5000)
    b = cand(start=9400, end=20000, quote="只有今天这个机制下播恢复原价啊")
    kept, dropped = nms([a, b])
    assert len(kept) == 2 and dropped == []


# ---------- 批内归一 ----------

def test_rank_normalize_range_and_weak_flag():
    hi = cand()
    lo = cand(title="弱片")
    lo["scores"] = {"hook": 2, "flow": 2, "value": 2, "trend": 2}
    rank_normalize([hi, lo])
    assert hi["rank_score"] == 99.0 and lo["rank_score"] == 40.0
    assert lo["scores"]["not_recommended"] is True
    assert hi["scores"]["not_recommended"] is False


def test_rank_normalize_single():
    c = cand()
    rank_normalize([c])
    assert c["rank_score"] == 76.0


# ---------- 文本归一化 ----------

def test_cjk_join_hotword_spaces():
    assert cjk_join("三 十 九 块 九") == "三十九块九"
    assert cjk_join("hello world 你 好") == "hello world 你好"


def test_normalize_text_fullwidth_space():
    assert normalize_text("三\u3000十 九") == "三十九"


# ---------- 主入口集成 ----------

def test_process_candidates_full_pass():
    tr = make_tr()
    kept, disc = process_candidates({"candidates": [cand()]}, tr, live_type="ecommerce")
    assert len(kept) == 1 and disc == []
    assert kept[0]["rank_score"] is not None
    assert "snap" in kept[0]


def test_process_candidates_schema_error_raises():
    tr = make_tr()
    with pytest.raises(ContractError):
        process_candidates({"candidates": [{"bad": 1}]}, tr, live_type="ecommerce")