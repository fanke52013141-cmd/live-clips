"""OP-5 选段质量：三闸门查重（时间重叠/文本相似/转写行复用，拒绝带理由）+ 提示词防膨胀。

对应《docs/11-优化包实施方案》OP-5。机制来自 Clips Kitty（实测 8/11 切点、
三闸门阈值 40%/70%/40%）；阈值全部可配，拒绝理由落 quality_report.json 可审计。
"""

from backend.pipeline import clip_finder
from backend.pipeline.quality import three_gate_dedupe
from backend.tests.test_clip_finder import _entries
from backend.pipeline.quality import to_srt_time


def _items(*specs):
    """specs: (id, start_sec, end_sec, score, text)"""
    return [{'id': str(i), 'start_time': to_srt_time(s), 'end_time': to_srt_time(e),
             'final_score': score, 'recommend_reason': 'r', 'generated_title': f'片段{i}',
             'outline': f'片段{i}'}
            for i, (s, e, score, text) in enumerate(specs, 1)]


def _entries_with_text(specs_text):
    """按 (start_sec, end_sec, text) 生成字幕行，时间与 items 对齐。"""
    return [{'index': i + 1, 'start_time': to_srt_time(s), 'end_time': to_srt_time(e), 'text': text}
            for i, (s, e, text) in enumerate(specs_text)]


def test_overlap_gate_rejects_highly_overlapping_clip():
    entries = _entries_with_text([(0, 60, '第一段内容'), (60, 120, '第二段内容'), (120, 180, '第三段内容')])
    items = _items((10, 80, 0.9, ''), (30, 100, 0.6, ''))  # 30-100 与 10-80 重叠 50s/70s
    kept, rejections = three_gate_dedupe(items, entries)
    assert [c['id'] for c in kept] == ['1']
    assert len(rejections) == 1
    assert rejections[0]['gate'] == '时间重叠' and rejections[0]['against'] == '1'


def test_text_similarity_gate_catches_rewritten_duplicate():
    base = '这是一段关于投资理财的长内容，讲述了如何分散风险并长期持有指数基金'
    same = '这是一段关于投资理财的长内容，讲述了如何分散风险并长期持有指数基金的补充说明'
    entries = _entries_with_text([(0, 60, base), (60, 120, same), (120, 180, '完全无关的另一段内容')])
    items = _items((10, 50, 0.9, ''), (70, 110, 0.8, ''))  # 文本几乎一样
    kept, rejections = three_gate_dedupe(items, entries)
    assert [c['id'] for c in kept] == ['1']
    assert rejections[0]['gate'] == '文本相似'


def test_segment_reuse_gate():
    entries = _entries_with_text([(0, 10, '内容一'), (10, 20, '内容二'), (20, 30, '内容三'), (30, 40, '内容四')])
    # 时间重叠仅 2s/10s=20%（过第一道闸门），但复用了 1/2 的转写行 → 复用闸门命中
    items = _items((0, 20, 0.9, ''), (18, 28, 0.8, ''))
    kept, rejections = three_gate_dedupe(items, entries)
    assert [c['id'] for c in kept] == ['1']
    assert rejections[0]['gate'] == '转写行复用'


def test_distinct_clips_survive_all_gates():
    entries = _entries_with_text([(0, 60, '第一段'), (60, 120, '第二段'), (120, 180, '第三段')])
    items = _items((10, 50, 0.9, ''), (70, 110, 0.8, ''), (130, 170, 0.7, ''))
    kept, rejections = three_gate_dedupe(items, entries)
    assert [c['id'] for c in kept] == ['1', '2', '3'] and rejections == []


def test_prompt_has_anti_inflation_calibration():
    assert '评分要苛刻' in clip_finder.PROMPT and '低于 0.7' in clip_finder.PROMPT


def test_clip_finder_reports_dedupe_rejections(tmp_path):
    """两窗各选到同一高光 → 三闸门拒绝其一，理由落报告。"""

    def call(prompt, data):
        lines = data['rows'].splitlines()
        first_row = int(lines[0].split('|')[0])
        last_row = int(lines[-1].split('|')[0])
        mid = max(first_row, (first_row + last_row) // 2)
        return {'clips': [
            {'start': first_row, 'end': min(first_row + 30, last_row), 'title': '重叠片段甲', 'reason': 'r', 'score': 0.9},
            {'start': mid, 'end': min(mid + 30, last_row), 'title': '重叠片段乙', 'reason': 'r', 'score': 0.8},
        ]}

    clip_finder.find_clips(_entries(12), call, threshold=0.5, metadata_dir=tmp_path)
    report_text = (tmp_path / 'quality_report.json').read_text(encoding='utf-8')
    assert 'dedupe_rejections' in report_text
