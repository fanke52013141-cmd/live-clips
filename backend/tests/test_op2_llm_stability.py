"""OP-2 LLM 稳定性：json_repair 兜底 + 逐条字段校验 + 输出形态守卫。

对应《docs/11-优化包实施方案》OP-2。畸形输出（Python 风格 None/True、
中文引号嵌套、尾逗号混合单引号等正则修不动的形态）由 json_repair 兜底恢复；
垃圾纯文本仍抛 ModelResponseInvalid，保持既有语义。
"""

import pytest

from backend.core.failure_attribution import ModelResponseInvalid
from backend.utils.llm_client import LLMClient
from backend.utils.llm_json import extract_list, filter_valid_items, repair_parse


def _client() -> LLMClient:
    try:
        return LLMClient()
    except Exception:  # pragma: no cover - 无配置环境仍可测解析层
        return LLMClient.__new__(LLMClient)


def test_json_repair_rescues_python_style_output():
    """Python 风格 None + 中文冒号后单引号：正则修复路径修不动，json_repair 兜底。"""
    raw = "[{'outline': '测试话题', 'start_time': '00:00:01', 'end_time': '00:00:10', 'score': None}]"
    parsed = _client().parse_json_response(raw)
    assert isinstance(parsed, list) and parsed[0]['outline'] == '测试话题'


def test_json_repair_rescues_trailing_comma_and_bad_quotes():
    raw = '{"clips": [{"title": "「引号」嵌套", "start": 1, "end": 2, "score": 0.8,},]}'
    parsed = _client().parse_json_response(raw)
    assert parsed['clips'][0]['title'].startswith('「')


def test_plain_garbage_still_raises_model_response_invalid():
    with pytest.raises(ModelResponseInvalid):
        _client().parse_json_response("这是一段完全没有任何结构化数据的叙述文本")


def test_repair_parse_rejects_non_structured():
    with pytest.raises(ValueError):
        repair_parse("just prose")


def test_extract_list_guard():
    assert extract_list([1, 2]) == [1, 2]
    assert extract_list({'clips': [1]}, list_key='clips') == [1]
    assert extract_list({'anything': [3]}) == [3]
    with pytest.raises(ValueError):
        extract_list("prose")
    with pytest.raises(ValueError):
        extract_list({'a': 1})


def test_filter_valid_items_drops_bad_entries_keeps_good():
    items = [
        {'id': '1', 'score': 0.9},
        'not-a-dict',
        {'id': None, 'score': 0.5},        # 必填字段为空
        {'score': 0.4},                    # 缺 id
        {'id': '2', 'score': 0.7, 'extra': True},
    ]
    valid, dropped = filter_valid_items(items, ('id',), label='clip')
    assert [v['id'] for v in valid] == ['1', '2']
    assert dropped == 3


def test_filter_valid_items_with_custom_validator():
    items = [{'id': 'a', 'score': 1.5}, {'id': 'b', 'score': 0.2}]
    valid, dropped = filter_valid_items(
        items, ('id', 'score'),
        item_validator=lambda item: 0.0 <= float(item['score']) <= 1.0,
    )
    assert [v['id'] for v in valid] == ['b']
    assert dropped == 1
