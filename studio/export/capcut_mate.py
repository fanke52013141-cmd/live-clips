"""capcut-mate REST 适配层（P2-6，L3 生态对接）。

许可证：Apache-2.0（官方 LICENSE 核实，见 docs/LICENSE_AUDIT.md）。
集成方式：独立服务 HTTP 对接，不复制其源码——本文件仅为客户端。

【未核实】响应 schema 与草稿格式版本：需部署服务后实测。
客户端对响应做宽容解析（顶层或 data 嵌套），关键字段缺失显式报错不伪造。
video_url 须为服务可访问的链接（README 要求公开可访问；本地部署的文件路径
支持程度未核实，调用方传入时回执标注）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

# transport(method, url, payload) -> dict；注入点（测试用 fake，零网络）
Transport = Callable[[str, str, dict], dict]


class DraftError(RuntimeError):
    pass


def _http_transport(method: str, url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise DraftError(f"capcut-mate 服务不可达或响应非法: {url}: {e}") from e


def _extract(resp: dict, *keys: str) -> dict:
    """宽容解析：顶层或 data 嵌套；一个关键字段都没有 → 显式报错。"""
    for container in (resp, resp.get("data") if isinstance(resp.get("data"), dict) else {}):
        if any(k in container for k in keys):
            return container
    raise DraftError(f"响应缺少关键字段 {keys}: {json.dumps(resp, ensure_ascii=False)[:300]}")


class CapCutMateClient:
    def __init__(self, base_url: str, transport: Transport | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._transport = transport or _http_transport

    def _post(self, path: str, payload: dict) -> dict:
        return self._transport("POST", f"{self._base}/openapi/capcut-mate/v1/{path}", payload)

    def create_draft(self, width: int, height: int) -> dict:
        resp = self._post("create_draft", {"width": width, "height": height})
        return _extract(resp, "draft_id", "draft_url")

    def add_videos(self, draft_url: str, video_infos: list[dict]) -> dict:
        # README：video_infos 为 JSON 字符串（不能嵌套数组）；时间单位微秒
        return self._post("add_videos", {
            "draft_url": draft_url,
            "video_infos": json.dumps(video_infos, ensure_ascii=False),
        })

    def add_captions(self, draft_url: str, caption_infos: list[dict]) -> dict:
        return self._post("add_captions", {
            "draft_url": draft_url,
            "caption_infos": json.dumps(caption_infos, ensure_ascii=False),
        })

    def save_draft(self, draft_url: str) -> dict:
        return self._post("save_draft", {"draft_url": draft_url})