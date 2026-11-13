"""FunASR SeACo-Paraformer 适配（中文 ASR + 热词 + 词级时间戳，04 号 P0-4）。

- 懒加载 funasr；模型未下载显式报原因，不静默下载。
- 热词走 SeACo-Paraformer（hotword 参数），这是上游 AutoClip 没有的能力（05 号笔记 §2）。
- paraformer 的 timestamp 为逐字毫秒对，映射为 Word。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from studio.contracts.text import cjk_join
from studio.engines.base import (
    EngineContext,
    ProgressEvent,
    ProgressFn,
    Sentence,
    TranscriptResult,
    Word,
)
from studio.engines.cache import params_hash

_MODEL_ID = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"


class ParaformerASR:
    def __init__(
        self,
        name: str = "paraformer_cpu",
        device: str = "cpu",
        model_factory: Callable | None = None,  # 测试注入
        cache_dir: str | Path | None = None,
    ) -> None:
        self.name = name
        self.device = device
        self._model_factory = model_factory
        self._cache_dir = cache_dir

    def _cache_root(self) -> Path:
        """ModelScope 官方缓存位置：显式指定 > MODELSCOPE_CACHE 环境变量 > 默认 ~/.cache/modelscope。"""
        if self._cache_dir is not None:
            return Path(self._cache_dir)
        env = os.environ.get("MODELSCOPE_CACHE")
        if env:
            return Path(env)
        return Path.home() / ".cache" / "modelscope"

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        if self._model_factory is not None:
            return True, ""  # 注入 factory = 调用方自带模型，跳过依赖探测
        try:
            import funasr  # noqa: F401
        except ImportError:
            return False, "funasr 未安装"
        root = self._cache_root()
        # 必须查到具体模型权重，缓存根目录存在不代表本模型已下载
        model_dir = root / "models" / _MODEL_ID.replace("/", "--") / "snapshots" / "master"
        if not (model_dir / "model.pt").exists():
            return False, f"模型未下载: {model_dir}（需用户批准后下载）"
        return True, ""

    def transcribe(
        self,
        audio_path: str,
        *,
        hotwords: list[str],
        language: str,
        progress: ProgressFn,
    ) -> TranscriptResult:
        if self._model_factory is not None:
            model = self._model_factory()
        else:
            os.environ["MODELSCOPE_CACHE"] = str(self._cache_root())
            from funasr import AutoModel

            model = AutoModel(model=_MODEL_ID, device=self.device, disable_update=True)
        progress(ProgressEvent("transcribing", 0, 1, "段", message="paraformer 转写中"))
        res = model.generate(
            input=audio_path,
            hotword=" ".join(hotwords) if hotwords else None,
            sentence_timestamp=True,
        )
        item = res[0]
        text: str = item.get("text", "")
        timestamps: list[list[int]] = item.get("timestamp", []) or []
        chars = [c for c in text if not c.isspace()]
        words: list[Word] = []
        for ch, ts in zip(chars, timestamps):
            words.append(Word(text=ch, start_ms=int(ts[0]), end_ms=int(ts[1])))
        start_ms = words[0].start_ms if words else 0
        end_ms = words[-1].end_ms if words else 0
        # 热词模式输出空格分词文本（P0-4b 发现）：展示口径去 CJK 间空格
        sentences = [Sentence(text=cjk_join(text), start_ms=start_ms, end_ms=end_ms)]
        # 句级细分：优先用 sentence_info（若模型返回）
        for si in item.get("sentence_info", []) or []:
            sentences.append(
                Sentence(
                    text=cjk_join(si.get("text", "")),
                    start_ms=int(si.get("start", 0)),
                    end_ms=int(si.get("end", 0)),
                )
            )
        if len(sentences) > 1:
            sentences = sentences[1:]
        progress(ProgressEvent("transcribing", 1, 1, "段", message="paraformer 完成"))
        ph = params_hash(
            {"model": _MODEL_ID, "device": self.device, "hotwords": hotwords, "language": language}
        )
        return TranscriptResult(
            words=tuple(words),
            sentences=tuple(sentences),
            engine=self.name,
            model=_MODEL_ID,
            params_hash=ph,
            word_level=True,
        )