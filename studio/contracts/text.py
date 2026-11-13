"""文本归一化（P0-4b 发现：FunASR 热词模式输出空格分词文本）。

- normalize_text：去掉全部空白（含全角空格），用于 quote/热词匹配口径。
- cjk_join：仅去掉 CJK 字符之间的空白（展示/字幕用），保留英文词间空格。
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\s+(?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])")


def normalize_text(s: str) -> str:
    """匹配口径：去全部空白。"""
    return _WS.sub("", s)


def cjk_join(s: str) -> str:
    """展示口径：中文之间的空格去掉（\"三 十 九 块 九\" → \"三十九块九\"）。"""
    return _CJK_GAP.sub("", s).strip()