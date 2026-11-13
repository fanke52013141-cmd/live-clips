"""弹幕信号抽取（04 号方案 P1-4，吸收 ChopperBot/ClipStream 思路，重实现不抄码）。

支持格式：
- 录播姬 XML（B站）：<d p="秒,模式,字号,颜色,时间戳,池,uid,rowid">文本</d>，p[0] 为相对开播秒数。
- 抖音 JSONL：每行 {"time_ms": int, "content": str, "user": str?}
  （兼容 timestamp/text/uid 别名；时间单位毫秒）。

计权与反刷屏（代码强制）：
- 5s 分桶计数；同桶同文本最多计 3 条（刷屏封顶）；同桶同用户最多计 5 条。
- 无弹幕文件/解析失败 → 空 series，编排器记「自动降维」，不报错不伪造。
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from studio.engines.base import (
    EngineContext,
    MaterialRef,
    ProgressFn,
    SignalTrack,
)

BUCKET_MS = 5_000
SAME_TEXT_CAP = 3
SAME_USER_CAP = 5


def parse_danmaku(path: str | Path) -> list[tuple[int, str, str]]:
    """返回 [(time_ms, text, uid)]。格式按扩展名+内容嗅探；失败返回空列表。"""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[tuple[int, str, str]] = []
    if p.suffix.lower() == ".xml" or raw.lstrip().startswith("<"):
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        for d in root.iter("d"):
            p_attr = d.get("p", "")
            parts = p_attr.split(",")
            if not parts:
                continue
            try:
                time_ms = int(float(parts[0]) * 1000)
            except ValueError:
                continue
            uid = parts[6] if len(parts) > 6 else ""
            events.append((time_ms, (d.text or "").strip(), uid))
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("time_ms", obj.get("timestamp"))
            text = obj.get("content", obj.get("text", ""))
            if t is None or not text:
                continue
            try:
                events.append((int(t), str(text).strip(), str(obj.get("user", obj.get("uid", "")))))
            except (TypeError, ValueError):
                continue
    return events


def bucket_counts(
    events: list[tuple[int, str, str]], duration_ms: int, bucket_ms: int = BUCKET_MS
) -> list[float]:
    """分桶计数 + 反刷屏封顶。"""
    n = max(1, (duration_ms + bucket_ms - 1) // bucket_ms)
    counts = [0.0] * n
    text_seen: dict[int, dict[str, int]] = {}
    user_seen: dict[int, dict[str, int]] = {}
    for t, text, uid in events:
        if t < 0 or t >= duration_ms or not text:
            continue
        b = t // bucket_ms
        ts = text_seen.setdefault(b, {})
        if ts.get(text, 0) >= SAME_TEXT_CAP:
            continue
        if uid:
            us = user_seen.setdefault(b, {})
            if us.get(uid, 0) >= SAME_USER_CAP:
                continue
            us[uid] = us.get(uid, 0) + 1
        ts[text] = ts.get(text, 0) + 1
        counts[b] += 1
    return counts


class DanmakuSignal:
    name = "danmaku"

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        return True, ""  # 纯 Python 解析器；素材级探测在 extract（无弹幕→降维）

    def extract(self, material: MaterialRef, progress: ProgressFn) -> SignalTrack:
        if not material.danmaku_path:
            return SignalTrack(bucket_ms=BUCKET_MS, series={})
        events = parse_danmaku(material.danmaku_path)
        if not events:
            return SignalTrack(bucket_ms=BUCKET_MS, series={})
        counts = bucket_counts(events, material.duration_ms)
        return SignalTrack(bucket_ms=BUCKET_MS, series={"danmaku": tuple(counts)})