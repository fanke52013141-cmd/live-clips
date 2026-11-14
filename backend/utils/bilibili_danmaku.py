"""B 站弹幕自动抓取（OP-4）：为召回层提供真实数据源。

链路：项目 source_url → BV 号 → view API 取 cid → 弹幕 API（deflate XML）→
统一弹幕事件流（`danmaku_parser.parse_xml`）。全部走 B 站公开接口，无需登录；
任何一步失败都返回 None——召回层据此自动回退全量路径（零影响）。

灰度开关：`AUTOCLIP_DANMAKU_RECALL`（默认开，设 0/off 关闭）。关闭或
非 B 站链接时 `_find_clips_in_one_pass` 完全按原路径调用 find_clips。
"""
from __future__ import annotations

import logging
import os
import re
import zlib
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .danmaku_parser import DanmakuEvent, parse_xml

logger = logging.getLogger(__name__)

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
_VIEW_API = 'https://api.bilibili.com/x/web-interface/view'
_DANMAKU_API = 'https://api.bilibili.com/x/v1/dm/list.so'
_BV_RE = re.compile(r'(BV[0-9A-Za-z]{10})')

_TRUTHY = frozenset({'1', 'on', 'true', 'yes'})
_FALSY = frozenset({'0', 'off', 'false', 'no'})


def recall_enabled() -> bool:
    """弹幕召回开关：默认开；AUTOCLIP_DANMAKU_RECALL=0/off 显式关闭。"""
    value = os.getenv('AUTOCLIP_DANMAKU_RECALL', '').strip().lower()
    if value in _FALSY:
        return False
    return True


def extract_bvid(url: str) -> Optional[str]:
    """从 B 站链接提取 BV 号；非 B 站链接返回 None。b23.tv 短链靠 302 跳转解析。"""
    if not url:
        return None
    host = (urlparse(url).hostname or '').lower()
    if host not in ('bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'b23.tv'):
        return None
    match = _BV_RE.search(url or '')
    if match:
        return match.group(1)
    if host == 'b23.tv':
        try:
            with httpx.Client(follow_redirects=False, timeout=10, headers={'User-Agent': _UA}) as client:
                location = client.get(url).headers.get('location', '')
            match = _BV_RE.search(location)
            return match.group(1) if match else None
        except httpx.HTTPError:
            return None
    return None


def fetch_view_info(bvid: str, *, timeout: float = 10.0) -> Optional[dict[str, Any]]:
    """view API：返回 {'cid', 'duration', 'title'}；失败返回 None。"""
    try:
        response = httpx.get(_VIEW_API, params={'bvid': bvid},
                             headers={'User-Agent': _UA, 'Referer': 'https://www.bilibili.com'},
                             timeout=timeout)
        payload = response.json()
        data = (payload or {}).get('data') or {}
        cid = data.get('cid')
        if payload.get('code') != 0 or not cid:
            return None
        return {'cid': int(cid), 'duration': data.get('duration'),
                'title': str(data.get('title') or '')[:300]}
    except (httpx.HTTPError, ValueError):
        return None


def fetch_danmaku_xml(cid: int, *, timeout: float = 15.0) -> Optional[str]:
    """弹幕 API 返回 deflate 压缩的 XML；解压失败按明文处理。失败返回 None。"""
    try:
        response = httpx.get(_DANMAKU_API, params={'oid': cid},
                             headers={'User-Agent': _UA, 'Referer': 'https://www.bilibili.com'},
                             timeout=timeout)
        content = response.content
        try:
            content = zlib.decompress(content)
        except zlib.error:
            try:
                content = zlib.decompress(content, -zlib.MAX_WBITS)  # raw deflate
            except zlib.error:
                pass  # 未压缩的明文 XML
        return content.decode('utf-8', errors='ignore')
    except (httpx.HTTPError, OSError):
        return None


def fetch_events_for_url(url: str, *, max_time: Optional[float] = None,
                         timeout: float = 15.0) -> Optional[list[DanmakuEvent]]:
    """项目链接 → 弹幕事件流；非 B 站链接、关闭开关或任一步失败都返回 None。"""
    if not recall_enabled():
        return None
    bvid = extract_bvid(url)
    if not bvid:
        return None
    info = fetch_view_info(bvid, timeout=timeout)
    if not info:
        logger.info('弹幕召回跳过：view API 未取到 cid（bvid=%s）', bvid)
        return None
    xml = fetch_danmaku_xml(info['cid'], timeout=timeout)
    if not xml:
        return None
    events = parse_xml(xml)
    if max_time is not None:
        events = [event for event in events if 0 <= event.time <= max_time]
    logger.info('弹幕召回：bvid=%s 取到 %d 条弹幕事件', bvid, len(events))
    return events or None
