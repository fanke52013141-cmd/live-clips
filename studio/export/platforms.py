"""平台发布预设（P2-8，04 号方案 §10.3/§9）。

五平台 v1：画幅/封面比例/标题文案上限/安全区遮挡比例。
【如实标注】安全区比例为经验值（平台 UI 遮挡：顶部状态栏/底部文案区/右侧互动栏），
须真机校准后更新；文案上限以平台当前规则为准，此处为保守值。
"""
from __future__ import annotations

PLATFORMS: dict[str, dict] = {
    "douyin": {
        "name": "抖音", "aspect": "9:16", "width": 1080, "height": 1920,
        "cover_ratio": "3:4", "title_max": 30, "caption_max": 1000,
        "safe_area": {"top": 0.12, "bottom": 0.18, "left": 0.04, "right": 0.14},
    },
    "kuaishou": {
        "name": "快手", "aspect": "9:16", "width": 1080, "height": 1920,
        "cover_ratio": "3:4", "title_max": 30, "caption_max": 1000,
        "safe_area": {"top": 0.12, "bottom": 0.20, "left": 0.04, "right": 0.12},
    },
    "bilibili": {
        "name": "B站", "aspect": "16:9", "width": 1920, "height": 1080,
        "cover_ratio": "16:10", "title_max": 80, "caption_max": 2000,
        "safe_area": {"top": 0.08, "bottom": 0.12, "left": 0.03, "right": 0.03},
    },
    "xhs": {
        "name": "小红书", "aspect": "3:4", "width": 1080, "height": 1440,
        "cover_ratio": "3:4", "title_max": 20, "caption_max": 1000,
        "safe_area": {"top": 0.10, "bottom": 0.16, "left": 0.04, "right": 0.10},
    },
    "shipinhao": {
        "name": "视频号", "aspect": "9:16", "width": 1080, "height": 1920,
        "cover_ratio": "3:4", "title_max": 30, "caption_max": 1000,
        "safe_area": {"top": 0.12, "bottom": 0.20, "left": 0.04, "right": 0.12},
    },
}


def get_platform(name: str) -> dict:
    if name not in PLATFORMS:
        raise ValueError(f"未知平台: {name}（支持: {sorted(PLATFORMS)}）")
    return PLATFORMS[name]


def fit_text(text: str, limit: int) -> tuple[str, bool]:
    """按平台上限截断；返回 (文本, 是否溢出)。溢出显式标注，不静默丢内容。"""
    t = text or ""
    if len(t) <= limit:
        return t, False
    return t[: limit - 1] + "…", True