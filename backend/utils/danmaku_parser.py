"""弹幕文件解析为统一事件流（OP-4）。

弹幕密度是"观众逐秒投票"的免费高光信号（《10-清单》4-1/4-2）。本模块把两种
常见格式解析为统一的 (时间秒, 权重, uid) 事件，供 pipeline/recall.py 做密度召回：

- B 站 ASS 弹幕（`Dialogue:` 行，自动导出/AAConverter 均兼容）
- B 站 / 录播姬 XML 弹幕（`<d p="时间,模式,字号,颜色,时间戳,池,用户ID,弹幕ID">文本`）
  其中 XML 自带用户 ID，可做单人刷屏封顶（付费互动的"比弹幕硬"的计权思想
  留给接入礼物/SC 事件源时扩展，本模块先支持文本弹幕）。

权重约定：普通弹幕 weight=1；付费事件源（SC/礼物）应由调用方给更大权重后合并。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable

_ASS_TIME = re.compile(r'^\s*Dialogue:\s*[^,]*,\s*(\d+:\d{2}:\d{2}\.\d+)')


@dataclass(frozen=True)
class DanmakuEvent:
    """一条弹幕的归一化事件。weight 供礼物/SC 等付费源放大。"""

    time: float          # 相对视频起点的秒数
    weight: float = 1.0
    uid: str = ''

    @property
    def second(self) -> int:
        return int(self.time)


def parse_ass(text: str) -> list[DanmakuEvent]:
    """ASS 弹幕文件 → 事件流。每个 Dialogue 行记一条（起始时刻即出现时刻）。"""
    events: list[DanmakuEvent] = []
    for line in text.splitlines():
        match = _ASS_TIME.match(line)
        if not match:
            continue
        h, m, s = match.group(1).split(':')
        events.append(DanmakuEvent(time=int(h) * 3600 + int(m) * 60 + float(s)))
    return events


def parse_xml(text: str, *, max_per_uid: int = 20) -> list[DanmakuEvent]:
    """B 站弹幕 XML → 事件流。

    max_per_uid 单人刷屏封顶：同一 UID 贡献的弹幕最多计 max_per_uid 条，
    刷屏机器人不再能凭数量伪造"高能"（《10-清单》4-2）。
    """
    events: list[DanmakuEvent] = []
    uid_counts: dict[str, int] = {}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return events
    for elem in root.iter('d'):
        p = (elem.get('p') or '').split(',')
        if not p or not p[0]:
            continue
        try:
            time_sec = float(p[0])
        except ValueError:
            continue
        uid = p[6] if len(p) > 6 else ''
        if uid and uid_counts.get(uid, 0) >= max_per_uid:
            continue
        if uid:
            uid_counts[uid] = uid_counts.get(uid, 0) + 1
        events.append(DanmakuEvent(time=time_sec, uid=uid))
    return events


def merge_events(*sources: Iterable[DanmakuEvent], max_time: float | None = None) -> list[DanmakuEvent]:
    """合并多个事件源并按时间排序；超出素材时长的事件丢弃（弹幕与视频时间轴偶有偏移）。"""
    merged = [event for source in sources for event in source]
    if max_time is not None:
        merged = [event for event in merged if 0 <= event.time <= max_time]
    return sorted(merged, key=lambda event: event.time)


def events_per_second(events: list[DanmakuEvent]) -> dict[int, int]:
    """每秒弹幕计数（加权求和取整），召回层密度计算的直接输入。"""
    counts: dict[int, int] = {}
    for event in events:
        counts[event.second] = counts.get(event.second, 0) + max(1, round(event.weight))
    return counts
