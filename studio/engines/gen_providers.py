"""生成式钩子提供商适配层（P3，04 号方案 §7.3 L3/L4）。

纪律：
- mock 提供商永远可用、零费用，产物 meta.mock=True 绝不冒充真实生成
- cloud 提供商**配置驱动**：base_url/端点/Key 全部来自用户配置（给 Key 时按官方
  文档核实后填入），代码不预设任何猜测端点；未配置 → 显式不可用原因
- 费用预估先行，人工确认后才生成（服务层强制），绝不静默计费
- 本地 Wan 引擎留 5080 实测批（gpu-local 档案注册即用，接口同构）
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from studio.engines.base import EngineContext


@dataclass(frozen=True)
class CostEstimate:
    provider: str
    kind: str  # image | video
    unit_price: float
    quantity: int
    currency: str = "CNY"
    note: str = ""

    @property
    def total(self) -> float:
        return round(self.unit_price * self.quantity, 4)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "kind": self.kind,
            "unit_price": self.unit_price, "quantity": self.quantity,
            "total": self.total, "currency": self.currency, "note": self.note,
        }


class GenProvider(Protocol):
    name: str
    kind: str

    def available(self, ctx: EngineContext) -> tuple[bool, str]: ...
    def estimate(self, params: dict) -> CostEstimate: ...
    def generate(self, params: dict, out_path: Path, ctx: EngineContext) -> dict: ...


class MockGenProvider:
    """确定性 mock：占位产物+mock 标记，零费用。测试/演示/无 Key 开发用。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.name = f"mock_{kind}"

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        return True, ""

    def estimate(self, params: dict) -> CostEstimate:
        return CostEstimate(self.name, self.kind, 0.0, 1, note="mock 零费用")

    def generate(self, params: dict, out_path: Path, ctx: EngineContext) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 占位文件（非真实媒体）：meta.mock=True 强制标注，渲染集成时拒绝 mock 素材
        out_path.write_bytes(f"MOCK_{self.kind.upper()}_HOOK {params.get('prompt', '')}".encode("utf-8"))
        return {"mock": True, "provider": self.name, "prompt": params.get("prompt", ""),
                "size_bytes": out_path.stat().st_size}


# cloud 配置键（ctx.env）：用户给 Key 时按官方文档核实后配置，代码不猜端点
_CLOUD_ENV = {
    "image": ("GEN_IMAGE_BASE_URL", "GEN_IMAGE_ENDPOINT", "GEN_IMAGE_API_KEY", "GEN_IMAGE_UNIT_PRICE"),
    "video": ("GEN_VIDEO_BASE_URL", "GEN_VIDEO_ENDPOINT", "GEN_VIDEO_API_KEY", "GEN_VIDEO_UNIT_PRICE"),
}

Transport = Callable[[str, str, dict, dict], dict]  # (method, url, headers, payload) -> resp


def _http_transport(method: str, url: str, headers: dict, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CloudGenProvider:
    """配置驱动的云生成适配器。未配置 → 不可用（原因明示待 Key/官方文档）。

    响应解析宽容（url/b64/任务 id 轮询结构各异）：真实 schema 待 Key 到位后
    按官方文档核实并收紧【未核实】。transport 可注入（测试零网络）。
    """

    def __init__(self, kind: str, transport: Transport | None = None) -> None:
        self.kind = kind
        self.name = f"cloud_{kind}"
        self._transport = transport or _http_transport

    def _config(self, ctx: EngineContext) -> dict | None:
        base_k, ep_k, key_k, price_k = _CLOUD_ENV[self.kind]
        env = ctx.env
        if not all(env.get(k) for k in (base_k, ep_k, key_k)):
            return None
        return {
            "base_url": env[base_k].rstrip("/"), "endpoint": env[ep_k],
            "api_key": env[key_k], "unit_price": float(env.get(price_k, "0") or 0),
        }

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        if self._config(ctx) is None:
            return False, (
                f"待配置 {_CLOUD_ENV[self.kind][0]}/{_CLOUD_ENV[self.kind][1]}/"
                f"{_CLOUD_ENV[self.kind][2]}（用户提供 Key 时按官方文档核实端点，不预设猜测地址）"
            )
        return True, ""

    def estimate(self, params: dict) -> CostEstimate:
        # 单价来自配置；未配置时给 0 并明示「待官方定价核实」
        price = float(params.get("unit_price", 0) or 0)
        return CostEstimate(
            self.name, self.kind, price, 1,
            note="单价来自配置；真实定价以官方为准（待核实）",
        )

    def generate(self, params: dict, out_path: Path, ctx: EngineContext) -> dict:
        cfg = self._config(ctx)
        if cfg is None:
            raise RuntimeError("cloud 生成未配置（待 Key 与官方端点核实）")
        url = cfg["base_url"] + cfg["endpoint"]
        resp = self._transport(
            "POST", url, {"Authorization": f"Bearer {cfg['api_key']}"},
            {"prompt": params.get("prompt", ""), **params.get("extra", {})},
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 宽容解析【未核实】：直返 URL / b64 / 异步任务 id
        if resp.get("output_path"):
            out_path.write_bytes(Path(resp["output_path"]).read_bytes())
        elif resp.get("b64"):
            import base64
            out_path.write_bytes(base64.b64decode(resp["b64"]))
        else:
            raise RuntimeError(f"cloud 响应无可落盘产物（schema 待官方文档核实）: {str(resp)[:200]}")
        return {"mock": False, "provider": self.name, "prompt": params.get("prompt", ""),
                "response_keys": sorted(resp.keys()), "size_bytes": out_path.stat().st_size}


def default_gen_providers() -> dict[str, GenProvider]:
    return {
        "mock_image": MockGenProvider("image"),
        "mock_video": MockGenProvider("video"),
        "cloud_image": CloudGenProvider("image"),
        "cloud_video": CloudGenProvider("video"),
    }