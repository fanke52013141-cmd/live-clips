"""studio 测试封闭性保障。

本机可能已真实下载 ASR 模型（P0-4b）；测试必须与真实环境隔离：
MODELSCOPE_CACHE 指向空临时目录 → paraformer 探测为「模型未下载」→ mock 兜底。
真实模型验收走 scripts/accept_paraformer.py，不在 pytest 内触发。
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_model_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "ms-cache-empty"))