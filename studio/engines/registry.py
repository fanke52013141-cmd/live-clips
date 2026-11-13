"""引擎注册表：探测 → 有序降级 → 显式记录（04 号方案 §5）。

P0 规则：
- 引擎链中「未实现」的名字记为降级原因继续向后找，不抛错（真实引擎 P0-4 起逐个注册）。
- mock 永远兜底；整条链都不可用才抛 EngineUnavailable。
- resolve 结果带 degraded 列表，调用方必须写进任务进度与回执。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from studio.engines.base import Engine, EngineContext
from studio.settings import Settings


class EngineUnavailable(RuntimeError):
    pass


_STAGE_MOCK = {
    "asr": "mock_asr",
    "diarize": "mock_diarize",
    "signals": "mock_signal",
    "llm": "mock_llm",
    "hooks": "mock_hook",
    "render": "mock_render",
}


@dataclass(frozen=True)
class Degradation:
    engine: str
    reason: str


@dataclass
class Resolution:
    engine: Engine
    name: str
    degraded: list[Degradation] = field(default_factory=list)

    @property
    def degraded_names(self) -> tuple[str, ...]:
        return tuple(d.engine for d in self.degraded)


def default_factories() -> dict[str, Callable[[], Engine]]:
    from studio.engines import mock
    from studio.engines.asr_bailian import BailianASR
    from studio.engines.asr_paraformer import ParaformerASR
    from studio.engines.asr_whisper import WhisperASR
    from studio.engines.diarize_campp import CamppDiarizer
    from studio.engines.hooks_text import TextOverlayHook
    from studio.engines.hooks_tts import TtsHook
    from studio.engines.render_ffmpeg import FfmpegRenderer
    from studio.engines.signals_audio import LoudnessSignal
    from studio.engines.signals_danmaku import DanmakuSignal

    return {
        "mock_asr": mock.MockASR,
        "mock_diarize": mock.MockDiarizer,
        "mock_signal": mock.MockSignal,
        "mock_llm": mock.MockLLM,
        "mock_hook": mock.MockHook,
        "mock_render": mock.MockRenderer,
        # 真实 ASR 引擎（P0-4）；懒加载重依赖，available() 显式探测
        "bailian": BailianASR,
        "whisper_cpu": WhisperASR,
        "paraformer_cpu": ParaformerASR,
        # 信号抽取器（P1-4）：纯 CPU，双档案无差别
        "danmaku": DanmakuSignal,
        "audio": LoudnessSignal,
        # 说话人分离（P1-7）：可选开关，默认关
        "campp_cpu": CamppDiarizer,
        # 制作钩子（P2-4/P2-5）：L1 文字贴片 / L2 TTS（edge-tts 主+SAPI 兜底）
        "text_overlay": TextOverlayHook,
        "edge_tts": TtsHook,
        # 渲染（P2-1）：词边界切割/逐词字幕/两遍 loudnorm/自检
        "x264": FfmpegRenderer,
    }


class EngineRegistry:
    def __init__(
        self,
        settings: Settings,
        ctx: EngineContext,
        factories: dict[str, Callable[[], Engine]] | None = None,
    ) -> None:
        self._settings = settings
        self._ctx = ctx
        self._factories = factories if factories is not None else default_factories()
        self._cache: dict[str, Engine] = {}

    def _get(self, name: str) -> Engine | None:
        if name in self._cache:
            return self._cache[name]
        factory = self._factories.get(name)
        if factory is None:
            return None
        eng = factory()
        self._cache[name] = eng
        return eng

    def resolve_name(self, name: str) -> Resolution:
        """按名字解析单个引擎（signals 并行抽取器用）。"""
        degraded: list[Degradation] = []
        eng = self._get(name)
        if eng is None:
            raise EngineUnavailable(f"引擎未实现: {name}")
        ok, reason = eng.available(self._ctx)
        if not ok:
            raise EngineUnavailable(f"引擎不可用: {name}: {reason}")
        return Resolution(engine=eng, name=name, degraded=degraded)

    def resolve(self, stage: str) -> Resolution:
        chain: tuple[str, ...] = getattr(self._settings.chains, stage)
        degraded: list[Degradation] = []
        for name in chain:
            # 链尾统一兜底到对应阶段的 mock（显式映射，避免单复数歧义）
            effective = name if name != "mock" else _STAGE_MOCK[stage]
            eng = self._get(effective)
            if eng is None:
                degraded.append(Degradation(name, "引擎未实现"))
                continue
            ok, reason = eng.available(self._ctx)
            if ok:
                return Resolution(engine=eng, name=name, degraded=degraded)
            degraded.append(Degradation(name, reason))
        raise EngineUnavailable(f"阶段 {stage} 无可用引擎: {degraded}")