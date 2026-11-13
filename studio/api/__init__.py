"""studio 独立 API（04 号方案 §8 的 P1 子集）。

独立 app 工厂，不改上游 backend 文件；与上游服务的挂载缝合在部署批做。
本地开发：uvicorn studio.api.app:dev_app --port 26761
"""
from studio.api.app import create_app

__all__ = ["create_app"]