"""LLM 结构化输出的统一容错入口（OP-2）。

多层防线（来源：《10-通用可借鉴与不可借鉴清单》3.1，四方开源项目共识）：
1. LLMClient.parse_json_response 已有 code fence 剥离 + 正则修复 + 重试；
2. 本模块补上 json_repair 模糊兜底与「逐条字段校验」——一条坏数据丢掉该条并记日志，
   其余照常出片，不再让单个畸形元素毁掉整单；
3. 输出形态守卫：期望数组拿到对象、期望列表拿到散文时直接报"形态不合格"。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def repair_parse(text: str) -> Any:
    """json.loads → json_repair 模糊修复 → 抛 ValueError。

    与 LLMClient.parse_json_response 的差异：不预处理 code fence 之外的叙述文字，
    适合模型被明确要求只输出 JSON 的调用点。
    """
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import json_repair
    except ImportError as error:  # pragma: no cover - 依赖缺失只在全新环境出现
        raise ValueError(f"JSON 解析失败且 json_repair 不可用: {error}") from error
    try:
        repaired = json_repair.loads(text)
    except Exception as error:  # noqa: BLE001
        raise ValueError(f"JSON 解析失败: {error}") from error
    if not isinstance(repaired, (dict, list)):
        raise ValueError("JSON 解析失败：修复结果不是结构化数据")
    return repaired


def extract_list(parsed: Any, *, list_key: str | None = None) -> list:
    """输出形态守卫：从响应里取出数组；形态不合格抛 ValueError。

    list_key 兼容 {"clips": [...]} / {"items": [...]} 这类包装结构。
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        if list_key and isinstance(parsed.get(list_key), list):
            return parsed[list_key]
        for value in parsed.values():
            if isinstance(value, list):
                return value
    raise ValueError(f"LLM 输出形态不合格：期望数组，实际 {type(parsed).__name__}")


def filter_valid_items(
    items: Any,
    required_fields: tuple[str, ...],
    *,
    item_validator: Callable[[dict], bool] | None = None,
    label: str = "item",
) -> tuple[list[dict], int]:
    """逐条字段校验：缺字段/类型不对/自定义校验不过的条目丢弃并计数，其余返回。

    返回 (有效条目, 丢弃数)。全部丢弃时返回空列表而不是抛错——调用方据此
    决定整单失败还是降级，坏数据本身永远是可跳过的。
    """
    if not isinstance(items, list):
        raise ValueError(f"LLM 输出形态不合格：期望数组，实际 {type(items).__name__}")
    valid: list[dict] = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            dropped += 1
            logger.warning("LLM 输出中丢弃非对象%s（共 %d 条被丢弃）", label, dropped)
            continue
        missing = [field for field in required_fields if field not in item or item[field] in (None, "")]
        if missing:
            dropped += 1
            logger.warning("LLM 输出中丢弃缺少字段 %s 的%s（共 %d 条被丢弃）", missing, label, dropped)
            continue
        if item_validator is not None and not item_validator(item):
            dropped += 1
            logger.warning("LLM 输出中丢弃未通过校验的%s（共 %d 条被丢弃）", label, dropped)
            continue
        valid.append(item)
    return valid, dropped
