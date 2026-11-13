"""百炼云 ASR 适配（cpu-cloud 档案首选，04 号 P0-4）。

诚实约束：
- 云端仅句级时间戳（05 号笔记 §2 实测上游行为），word_level=False。
- 若 API 未返回时间戳，明确报错，绝不伪造/均摊时间戳。
- 真实 API 行为【未核实】：需要 DASHSCOPE_API_KEY 实测（P0-4 验收待用户提供 Key）。
- call_fn 可注入，测试不依赖网络。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from studio.engines.base import (
    EngineContext,
    ProgressEvent,
    ProgressFn,
    Sentence,
    TranscriptResult,
)
from studio.engines.cache import params_hash


class BailianASR:
    name = "bailian"

    def __init__(
        self,
        model: str = "qwen3-asr-flash",
        call_fn: Callable | None = None,  # 测试注入: (audio_path, model, api_key) -> dict
    ) -> None:
        self.model = model
        self._call_fn = call_fn

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        key = ctx.env.get("DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if not key:
            return False, "缺少 DASHSCOPE_API_KEY"
        return True, ""

    def transcribe(
        self,
        audio_path: str,
        *,
        hotwords: list[str],
        language: str,
        progress: ProgressFn,
    ) -> TranscriptResult:
        key = os.environ.get("DASHSCOPE_API_KEY", "")
        progress(ProgressEvent("transcribing", 0, 1, "段", message="云端 ASR 请求中"))
        if self._call_fn is not None:
            resp = self._call_fn(audio_path, self.model, key)
        else:
            import dashscope
            from dashscope import MultiModalConversation

            dashscope.api_key = key
            resp_raw = MultiModalConversation.call(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [{"audio": Path(audio_path).as_uri()}],
                    }
                ],
            )
            resp = resp_raw  # 真实解析待 Key 实测后补全【未核实】
            raise NotImplementedError(
                "百炼真实 API 解析待实测（需 DASHSCOPE_API_KEY）；当前仅支持注入 call_fn"
            )
        sentences_raw = resp.get("sentences") if isinstance(resp, dict) else None
        if not sentences_raw:
            raise ValueError("云端引擎未返回句级时间戳，请改用本地引擎（不伪造时间戳）")
        sentences = tuple(
            Sentence(
                text=s["text"],
                start_ms=int(s["start_ms"]),
                end_ms=int(s["end_ms"]),
            )
            for s in sentences_raw
        )
        progress(ProgressEvent("transcribing", 1, 1, "段", message="云端 ASR 完成"))
        ph = params_hash({"model": self.model, "language": language, "hotwords": hotwords})
        return TranscriptResult(
            words=(),
            sentences=sentences,
            engine=self.name,
            model=self.model,
            params_hash=ph,
            word_level=False,
        )