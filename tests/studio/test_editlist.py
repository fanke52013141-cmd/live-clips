"""剪辑决策纯逻辑测试（P2-2a：三重判定/保守口头禅/段列表/重映射）。"""
import pytest

from studio.engines.base import Word
from studio.pipeline.editlist import (
    Cut,
    Segment,
    build_segments,
    detect_breaths,
    detect_fillers,
    remap_ms,
    subtract_intervals,
)


def w(text, s, e):
    return Word(text, s, e)


def low_rms(a, b):
    return 0.001


def loud_rms(a, b):
    return 0.2  # 笑声/尾音：有能量


# ---------- 三重判定 ----------

def test_breath_three_conditions_pass():
    words = [w("你", 0, 500), w("好", 1500, 2000)]  # 间隙 1000ms
    silences = [(500, 1500)]
    cuts = detect_breaths(words, 0, 2000, silences, low_rms)
    assert len(cuts) == 1
    c = cuts[0]
    assert c.kind == "breath"
    # 保留呼吸口：两端各留 125ms
    assert (c.start_ms, c.end_ms) == (625, 1375)
    assert "三重判定" in c.reason


def test_breath_short_gap_not_cut():
    words = [w("你", 0, 500), w("好", 1000, 1500)]  # 间隙 500ms ≤ 600
    assert detect_breaths(words, 0, 1500, [(500, 1000)], low_rms) == []


def test_breath_no_silence_confirmation_not_cut():
    words = [w("你", 0, 500), w("好", 1500, 2000)]
    # silencedetect 只覆盖间隙的一小部分（<80%）→ 不剪
    assert detect_breaths(words, 0, 2000, [(500, 700)], low_rms) == []


def test_breath_laughter_energy_rejected():
    """间隙里是笑声（有能量）→ 能量 VAD 拒绝，不误删。"""
    words = [w("哈", 0, 500), w("哈", 1500, 2000)]
    assert detect_breaths(words, 0, 2000, [(500, 1500)], loud_rms) == []


def test_breath_keep_breath_too_large_no_cut():
    words = [w("你", 0, 500), w("好", 1200, 1700)]  # 间隙 700ms
    # 保留 800ms > 间隙 → 无可剪区间
    assert detect_breaths(words, 0, 1700, [(500, 1200)], low_rms, keep_breath_ms=800) == []


# ---------- 保守口头禅 ----------

def test_filler_isolated_cut():
    words = [w("价", 0, 400), w("嗯", 800, 1100), w("格", 1500, 1900)]
    cuts = detect_fillers(words, 0, 1900)
    assert len(cuts) == 1 and cuts[0].kind == "filler"
    assert (cuts[0].start_ms, cuts[0].end_ms) == (800, 1100)


def test_filler_in_flow_not_cut():
    """语流中的语气词（一侧无停顿）不剪——保守原则。"""
    words = [w("价", 0, 400), w("嗯", 450, 700), w("格", 750, 1100)]
    assert detect_fillers(words, 0, 1100) == []


def test_filler_semantic_word_not_cut():
    words = [w("那", 0, 400), w("个", 800, 1100), w("好", 1500, 1900)]
    assert detect_fillers(words, 0, 1900) == []  # 「个」不在保守词表


# ---------- 区间运算与段列表 ----------

def test_subtract_intervals():
    kept = subtract_intervals((0, 10000), [Cut(2000, 3000, "breath", ""), (5000, 6000)])
    assert kept == [(0, 2000), (3000, 5000), (6000, 10000)]


def test_subtract_overlapping_cuts():
    kept = subtract_intervals((0, 10000), [(2000, 4000), (3000, 5000)])
    assert kept == [(0, 2000), (5000, 10000)]


def test_build_segments_plain():
    segs = build_segments(0, 10000, [Cut(2000, 3000, "breath", "")])
    assert [(s.start_ms, s.end_ms, s.kind) for s in segs] == [
        (0, 2000, "body"), (3000, 10000, "body"),
    ]


def test_build_segments_cold_open_keep_original():
    segs = build_segments(0, 10000, [], cold_open=(4000, 6000))
    assert segs[0] == Segment(4000, 6000, "cold_open")
    assert sum(s.duration_ms for s in segs) == 12000  # 钩子复制，原位保留


def test_build_segments_cold_open_jumpcut():
    segs = build_segments(0, 10000, [], cold_open=(4000, 6000), cold_open_jumpcut=True)
    assert sum(s.duration_ms for s in segs) == 10000  # 原位挖掉，不重复
    assert segs[0].kind == "cold_open"
    assert all(not (s.start_ms < 6000 and s.end_ms > 4000) for s in segs[1:])


def test_build_segments_flash_first():
    segs = build_segments(0, 10000, [], cold_open=(4000, 6000), flash_forward=(8000, 8500))
    assert [s.kind for s in segs] == ["flash", "cold_open", "body"]


def test_build_segments_invalid_hook_raises():
    with pytest.raises(ValueError, match="越界"):
        build_segments(0, 10000, [], cold_open=(9000, 12000))
    with pytest.raises(ValueError):
        build_segments(0, 10000, [], flash_forward=(5000, 5000))


# ---------- 时间重映射 ----------

def test_remap_with_cuts():
    segs = build_segments(0, 10000, [Cut(2000, 3000, "breath", "")])
    assert remap_ms(1000, segs) == 1000
    assert remap_ms(3500, segs) == 2500  # 剪掉 1000ms 后前移
    assert remap_ms(2500, segs) is None  # 落在被剪区间


def test_remap_cold_open_copy():
    segs = build_segments(0, 10000, [], cold_open=(4000, 6000))
    assert remap_ms(4500, segs) == 500  # 钩子副本在片头
    assert remap_ms(0, segs) == 2000    # 正文整体后移钩子时长