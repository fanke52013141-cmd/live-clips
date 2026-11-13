"""引擎抽象层（04 号方案 §5 接口契约）。

铁律：
- 每个引擎声明 available(ctx)，不可用时给出原因（供降级记录与设置页展示）。
- 降级必须显式记录（Degradation），绝不静默。
- mock 引擎永远可用，保证无 Key/无 GPU/断网可跑全链路。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None


@dataclass(frozen=True)
class Sentence:
    text: str
    start_ms: int
    end_ms: int
    speaker: str | None = None


@dataclass(frozen=True)
class TranscriptResult:
    words: tuple[Word, ...]
    sentences: tuple[Sentence, ...]
    engine: str
    model: str
    params_hash: str
    word_level: bool  # 云 ASR 仅句级时为 False（05 号笔记 §2）
    degraded_from: str | None = None


@dataclass(frozen=True)
class EngineContext:
    profile: str
    data_dir: Path
    models_dir: Path
    gpu_available: bool
    env: dict[str, str] = field(default_factory=dict)  # API Key 等，来自环境/ settings.json
    ffmpeg_path: str | None = None


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    done: int
    total: int
    unit: str
    message: str = ""
    degraded: tuple[str, ...] = ()


ProgressFn = Callable[[ProgressEvent], None]


def noop_progress(event: ProgressEvent) -> None:
    pass


@dataclass(frozen=True)
class MaterialRef:
    material_id: str
    file_path: str
    fingerprint: str
    duration_ms: int
    danmaku_path: str | None = None


@dataclass(frozen=True)
class SignalTrack:
    bucket_ms: int
    series: dict[str, tuple[float, ...]]


@dataclass(frozen=True)
class ClipPlan:
    candidate_id: str
    start_ms: int
    end_ms: int
    title: str
    quote: str
    hook_plan: dict | None = None
    rank_score: float | None = None  # P2-4：L2 钩子 rank 门槛用


@dataclass(frozen=True)
class HookAsset:
    level: int  # 1 文字 2 TTS 3 B-roll 4 生成视频
    kind: str
    file_path: str | None = None
    text: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RenderPlan:
    clip: ClipPlan
    platform: str
    portrait: bool
    subtitles: bool
    out_path: str
    # P2-1 扩展（带默认值，mock 渲染器不受影响）
    source_path: str = ""
    words: tuple[Word, ...] = ()  # 全片词级时间戳；切割吸附与逐词字幕用
    target_lufs: float = -14.0
    # P2-2/P2-3 剪辑决策（源绝对 ms；空=无剪辑走快路径）
    cuts: tuple[tuple[int, int], ...] = ()  # 去气口/口头禅剪除区间
    cold_open: tuple[int, int] | None = None  # 钩子句区间（复制到片头）
    cold_open_jumpcut: bool = False  # True=正文挖掉钩子原位
    flash_forward: tuple[int, int] | None = None  # 高能闪现 0.3-1s
    # P2-4/P2-5 制作钩子
    hook_overlay: dict | None = None  # {text, duration_s} L1 文字贴片
    hook_audio: str | None = None  # L2 TTS 音频文件（叠片头，原声闪避）
    hook_duck: float = 0.3  # TTS 期间原声音量系数
    # P3 生成素材集成（mock 素材禁入：编排器强制校验 meta.mock）
    broll_image: str | None = None  # L3 生图 → 片头 2s 静帧 B-roll
    hook_video: str | None = None  # L4 生视频 → 片头冷开场（替代原生 cold_open）
    aigc_badge: bool = False  # AI 内容显式角标（全程「AI生成」）


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    file_path: str
    size_bytes: int
    meta: dict = field(default_factory=dict)


@runtime_checkable
class Engine(Protocol):
    name: str

    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。可用时原因为空串。"""
        ...


@runtime_checkable
class ASREngine(Engine, Protocol):
    def transcribe(
        self,
        audio_path: str,
        *,
        hotwords: list[str],
        language: str,
        progress: ProgressFn,
    ) -> TranscriptResult: ...


@runtime_checkable
class Diarizer(Engine, Protocol):
    def assign_speakers(
        self, tr: TranscriptResult, audio_path: str, progress: ProgressFn
    ) -> TranscriptResult: ...


@runtime_checkable
class SignalExtractor(Engine, Protocol):
    def extract(self, material: MaterialRef, progress: ProgressFn) -> SignalTrack: ...


@runtime_checkable
class LLMClient(Engine, Protocol):
    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        """输出必须过 JSON Schema 校验；失败重试后仍失败须抛错，不静默。"""
        ...


@runtime_checkable
class HookGenerator(Engine, Protocol):
    level: int

    def generate(
        self, clip: ClipPlan, ctx: EngineContext, progress: ProgressFn
    ) -> HookAsset | None:
        """None = 生成失败/写不出 → 上层跳过该级钩子（宁缺毋滥）。"""
        ...


@runtime_checkable
class Renderer(Engine, Protocol):
    def render(self, plan: RenderPlan, progress: ProgressFn) -> ArtifactRef: ...