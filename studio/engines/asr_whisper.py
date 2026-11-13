"""faster-whisper 适配（本地 CPU int8 / GPU 共用，04 号 P0-4）。

- 懒加载：未安装 faster-whisper 时 available() 报「未安装」，不拖垮注册表。
- 模型未下载时报「模型未下载」，绝不静默触发大下载（需用户批准）。
- word_timestamps=True 输出词级时间戳；热词 whisper 不支持（仅 paraformer 支持），
  热词参与 params_hash 以区分缓存。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from studio.engines.base import (
    EngineContext,
    ProgressEvent,
    ProgressFn,
    Sentence,
    TranscriptResult,
    Word,
)
from studio.engines.cache import params_hash


class WhisperASR:
    def __init__(
        self,
        name: str = "whisper_cpu",
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        model_factory: Callable | None = None,  # 测试注入
    ) -> None:
        self.name = name
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._model_factory = model_factory

    def _model_dir(self, ctx: EngineContext) -> Path:
        return ctx.models_dir / "whisper" / self.model_size

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        if self._model_factory is not None:
            return True, ""  # 注入 factory = 调用方自带模型，跳过依赖探测
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper 未安装"
        if not self._model_dir(ctx).exists():
            return False, f"模型未下载: {self._model_dir(ctx)}"
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
            from faster_whisper import WhisperModel

            model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
        segments, info = model.transcribe(
            audio_path,
            language=language or None,
            word_timestamps=True,
            vad_filter=True,
        )
        total_s = max(int(getattr(info, "duration", 0) or 0), 1)
        words: list[Word] = []
        sentences: list[Sentence] = []
        for seg in segments:
            sentences.append(
                Sentence(
                    text=seg.text.strip(),
                    start_ms=int(seg.start * 1000),
                    end_ms=int(seg.end * 1000),
                )
            )
            for w in seg.words or []:
                words.append(
                    Word(
                        text=w.word.strip(),
                        start_ms=int(w.start * 1000),
                        end_ms=int(w.end * 1000),
                    )
                )
            progress(
                ProgressEvent(
                    stage="transcribing",
                    done=min(int(seg.end), total_s),
                    total=total_s,
                    unit="秒",
                    message=f"{self.name} 转写中",
                )
            )
        ph = params_hash(
            {
                "model_size": self.model_size,
                "device": self.device,
                "compute_type": self.compute_type,
                "language": language,
                "hotwords": hotwords,
                "vad": True,
            }
        )
        return TranscriptResult(
            words=tuple(words),
            sentences=tuple(sentences),
            engine=self.name,
            model=self.model_size,
            params_hash=ph,
            word_level=True,
        )