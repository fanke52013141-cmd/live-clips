"""OP-4 成本漏斗：弹幕解析 → 密度召回 → clip_finder 只精读候选区间 + 预算护栏。

对应《docs/11-优化包实施方案》OP-4。召回算法移植自 auto-slice-video（MIT）；
降级保护：信号稀疏回退全量路径（访谈/知识类素材不受影响）。
"""

import pytest

from backend.pipeline import clip_finder
from backend.pipeline.recall import density_windows, recalled_spans, to_row_spans
from backend.tests.test_clip_finder import _entries
from backend.utils.danmaku_parser import DanmakuEvent, merge_events, parse_ass, parse_xml


# ---------- 弹幕解析 ----------

def test_parse_ass_dialogue_lines():
    ass = """[Script Info]
Title: t

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:41:20.50,0:41:22.00,Pop,,0,0,0,,草 哈哈哈
Dialogue: 0,0:41:21.00,0:41:23.00,Pop,,0,0,0,,6666
非 Dialogue 行忽略
"""
    events = parse_ass(ass)
    assert [round(e.time, 1) for e in events] == [2480.5, 2481.0]


def test_parse_xml_caps_one_uid():
    rows = ''.join(f'<d p="{i}.0,1,25,16777215,0,0,{uid},0,0">x</d>'
                   for i, uid in enumerate(['spam'] * 30 + ['normal'] * 5, start=100))
    events = parse_xml(f'<i>{rows}</i>')
    assert len(events) == 25  # spam 封顶 20 + normal 5
    assert all(e.uid != 'spam' or e.time < 120 for e in events)


def test_merge_events_sorts_and_drops_out_of_range():
    merged = merge_events([DanmakuEvent(30)], [DanmakuEvent(10), DanmakuEvent(999)], max_time=100)
    assert [e.time for e in merged] == [10.0, 30.0]


# ---------- 密度召回 ----------

def test_density_windows_overlap_greedy():
    # 两波高能（0-90s 与 400-490s），窗长 90s：top-2 应各占一波而不是同一波切两刀
    per_second = {sec: 10 for sec in range(0, 90)}
    per_second.update({sec: 8 for sec in range(400, 490)})
    picked = density_windows(per_second, duration_sec=600.0, window_sec=90.0, step_sec=10.0,
                             max_overlap_sec=45.0, top_k=2)
    assert len(picked) == 2
    starts = sorted(p[0] for p in picked)
    assert starts[0] <= 90 and starts[1] >= 360  # 一波一个候选


def test_recalled_spans_none_when_signal_sparse():
    rows = [{'start': i * 5.0, 'end': (i + 1) * 5.0} for i in range(100)]
    assert recalled_spans(rows, duration_sec=500.0, danmaku_per_second={10: 3, 20: 2}) is None


def test_recalled_spans_maps_dense_regions_to_row_ranges():
    rows = [{'start': i * 5.0, 'end': (i + 1) * 5.0} for i in range(int(40 * 60 / 5))]  # 40 min
    burst = {sec: 5 for sec in range(int(30 * 60), int(33 * 60))}  # 30-33 分钟集中爆发
    spans = recalled_spans(rows, duration_sec=40 * 60.0, danmaku_per_second=burst)
    assert spans, "密集信号必须召回出候选区间"
    for first, last in spans:
        assert 0 <= first <= last < len(rows)
        start_sec, end_sec = rows[first]['start'], rows[last]['end']
        assert start_sec < 33 * 60 + 90 and end_sec > 30 * 60 - 90  # 覆盖爆发段并带 pad


def test_to_row_spans_merges_overlapping_windows():
    rows = [{'start': i * 5.0, 'end': (i + 1) * 5.0} for i in range(100)]
    spans = to_row_spans([(10.0, 40.0), (35.0, 60.0)], rows, pad_sec=0.0)
    assert len(spans) == 1 and spans[0] == (2, 11)  # 10-60s 的重叠窗合并为一个行区间


# ---------- clip_finder 集成 ----------

def test_clip_finder_with_danmaku_only_reads_recall_windows(tmp_path):
    sent = []

    def call(prompt, data):
        lines = data['rows'].splitlines()
        first_row = int(lines[0].split('|')[0])
        last_row = int(lines[-1].split('|')[0])
        sent.append(data['rows'])
        return {'clips': [{'start': first_row, 'end': min(first_row + 20, last_row),
                           'title': '召回段内的高光', 'reason': '现场炸了', 'score': 0.95}]}

    # 60 分钟字幕；弹幕全部集中在 20-22 分钟（120 条，远超稀疏阈值 30）
    entries = _entries(60)
    events = [DanmakuEvent(time=float(sec)) for sec in range(20 * 60, 22 * 60)]
    clip_finder.find_clips(entries, call, threshold=0.5, metadata_dir=tmp_path, danmaku_events=events)

    assert sent, "召回生效时也要发出 LLM 调用"
    covered_rows = set()
    for rows_payload in sent:
        for line in rows_payload.splitlines():
            covered_rows.add(int(line.split('|')[0]))
    assert covered_rows, "提交行非空"
    assert min(covered_rows) >= (20 * 60 / 5) - 60  # 不再从片头全量读起
    report = (tmp_path / 'quality_report.json')
    assert report.exists()
    assert report.read_text(encoding='utf-8').find('"recalled": true') >= 0


def test_clip_finder_sparse_danmaku_falls_back_to_full(tmp_path):
    sent = []

    def call(prompt, data):
        sent.append(data['rows'])
        return {'clips': [{'start': 0, 'end': 30, 'title': '全量路径', 'reason': '正常', 'score': 0.9}]}

    entries = _entries(12)
    events = [DanmakuEvent(time=10.0), DanmakuEvent(time=20.0)]  # 只有 2 条：稀疏
    clip_finder.find_clips(entries, call, threshold=0.5, danmaku_events=events)
    assert sent and sent[0].startswith('0|')  # 回退全量：第一行就从 0 开始


def test_budget_guard_blocks_before_llm_call(monkeypatch, tmp_path):
    sent = []

    def call(prompt, data):
        sent.append(data)
        return {'clips': []}

    monkeypatch.setenv('AUTOCLIP_MAX_LLM_INPUT_TOKENS', '10')  # 预算小到必超
    with pytest.raises(clip_finder.ClipFinderError, match='预算'):
        clip_finder.find_clips(_entries(12), call, threshold=0.5, metadata_dir=tmp_path)
    assert sent == []  # 预算拦截发生在任何 LLM 调用之前
