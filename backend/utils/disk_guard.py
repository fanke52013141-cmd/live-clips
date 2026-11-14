"""磁盘熔断（OP-6.5）：写大文件前检查剩余空间。

来源：《10-清单》3.2-13（StreamCap 磁盘阈值熔断）。批量出片/合集拼接/
大视频下载都是"先写几 GB 再发现满了"的高危场景——低于阈值直接失败并给出
可操作提示，而不是写到一半损坏文件。探测失败按"通过"处理（fail-open），
磁盘信息拿不到时绝不阻塞正常任务。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MIN_FREE_MB = 2048


def free_mb(path) -> int | None:
    """目标路径所在盘的剩余空间（MB）；路径非法或探测失败返回 None。"""
    try:
        root = Path(path).anchor or str(Path(path))
        return int(shutil.disk_usage(root).free / (1024 * 1024))
    except (OSError, ValueError, TypeError):
        return None


def ensure_disk_space(path: Path, min_free_mb: int = DEFAULT_MIN_FREE_MB) -> None:
    """剩余空间低于 min_free_mb 时抛 RuntimeError，带剩余量与处置提示。"""
    available = free_mb(path)
    if available is None:
        return  # fail-open：探测不到磁盘信息不阻塞任务
    if available < min_free_mb:
        raise RuntimeError(
            f"磁盘剩余空间不足：{available}MB < 要求 {min_free_mb}MB"
            f"（路径 {path}）。请清理磁盘空间或在设置中调整。")
    logger.debug("磁盘空间检查通过: %dMB 可用（阈值 %dMB）", available, min_free_mb)
