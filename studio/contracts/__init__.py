"""LLM 输入输出契约与代码级铁律（04 号方案 §7）。

原则：校验在代码里强制，不靠 prompt 自觉。
"""
from studio.contracts.selection import (
    ContractError,
    process_candidates,
    validate_selection_output,
)
from studio.contracts.text import cjk_join, normalize_text

__all__ = [
    "ContractError",
    "process_candidates",
    "validate_selection_output",
    "cjk_join",
    "normalize_text",
]