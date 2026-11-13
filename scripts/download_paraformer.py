"""下载 SeACo-Paraformer 模型（用户已批准，约 1GB）。

来源：ModelScope 官方模型库（funasr SDK 自动拉取），无任何代理替换。
缓存位置：MODELSCOPE_CACHE 环境变量或默认 ~/.cache/modelscope，
与 studio/engines/asr_paraformer.py 的解析顺序一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MODEL_ID = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"


def main() -> int:
    cache = Path(os.environ.get("MODELSCOPE_CACHE") or Path.home() / ".cache" / "modelscope")
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["MODELSCOPE_CACHE"] = str(cache)
    print(f"cache dir: {cache}", flush=True)
    print(f"model: {MODEL_ID}", flush=True)
    from funasr import AutoModel

    AutoModel(model=MODEL_ID, device="cpu", disable_update=True)
    print("DOWNLOAD_OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())