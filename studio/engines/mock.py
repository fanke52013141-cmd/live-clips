"""确定性 mock 引擎：无 Key/无 GPU/断网可跑全链路（测试与演示用）。

所有 mock 产物 meta 带 mock=True，绝不冒充真实结果。
"""
from __future__ import annotations

from pathlib import Path

from studio.engines.base import (
    ArtifactRef,
    ClipPlan,
    EngineContext,
    HookAsset,
    MaterialRef,
    ProgressEvent,
    ProgressFn,
    RenderPlan,
    Sentence,
    SignalTrack,
    TranscriptResult,
    Word,
)

_MOCK_TEXT = [
    "今天这个价格直接给你干到三十九块九。",
    "拍一发三，还送运费险。",
    "很多人问这个面料透不透，我现场给你看。",
    "只有今天这个机制，下播就恢复原价。",
]


class _AlwaysAvailable:
    def available(self, ctx: EngineContext) -> tuple[bool, str]:
        return True, ""


class MockASR(_AlwaysAvailable):
    name = "mock_asr"

    def transcribe(
        self,
        audio_path: str,
        *,
        hotwords: list[str],
        language: str,
        progress: ProgressFn,
    ) -> TranscriptResult:
        words: list[Word] = []
        sentences: list[Sentence] = []
        t = 0
        for i, text in enumerate(_MOCK_TEXT):
            start = t
            # 每句按字符数线性铺词（300ms/字，接近真实语速），保证时间戳单调；
            # 全片约 20.7s，使 mock 候选可满足电商 15-60s 时长硬约束（P1-1 契约）
            for ch in text:
                words.append(Word(text=ch, start_ms=t, end_ms=t + 300))
                t += 300
            sentences.append(Sentence(text=text, start_ms=start, end_ms=t))
            t += 400  # 句间停顿
            progress(
                ProgressEvent(
                    stage="transcribing",
                    done=i + 1,
                    total=len(_MOCK_TEXT),
                    unit="句",
                    message="mock 转写",
                )
            )
        return TranscriptResult(
            words=tuple(words),
            sentences=tuple(sentences),
            engine=self.name,
            model="mock",
            params_hash="mock",
            word_level=True,
        )


class MockDiarizer(_AlwaysAvailable):
    name = "mock_diarize"

    def assign_speakers(
        self, tr: TranscriptResult, audio_path: str, progress: ProgressFn
    ) -> TranscriptResult:
        return TranscriptResult(
            words=tuple(
                Word(w.text, w.start_ms, w.end_ms, speaker="spk0") for w in tr.words
            ),
            sentences=tuple(
                Sentence(s.text, s.start_ms, s.end_ms, speaker="spk0")
                for s in tr.sentences
            ),
            engine=tr.engine,
            model=tr.model,
            params_hash=tr.params_hash,
            word_level=tr.word_level,
            degraded_from=tr.degraded_from,
        )


class MockSignal(_AlwaysAvailable):
    name = "mock_signal"

    def extract(self, material: MaterialRef, progress: ProgressFn) -> SignalTrack:
        buckets = max(1, material.duration_ms // 5000)
        return SignalTrack(
            bucket_ms=5000,
            series={
                "danmaku": tuple(float(i % 5) for i in range(buckets)),
                "loudness": tuple(0.5 for _ in range(buckets)),
            },
        )


class MockLLM(_AlwaysAvailable):
    name = "mock_llm"

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        # 复审环应答（P1-2）：按 system 标记区分，确定性输出
        if "切片复审官" in system:
            return {"standalone": True, "score": 0.9, "reason": "mock 复审通过"}
        if "边界修复器" in system:
            return {"start_ms": 0, "end_ms": 5400, "reason": "mock 修复"}
        if "钩子文案写手" in system:
            return {"text": "mock 悬念句"}
        return {
            "candidates": [
                {
                    "start_ms": 0,
                    "end_ms": 16000,
                    "clip_type": "福利",
                    "quote": _MOCK_TEXT[0],
                    "reason": "mock 候选：价格爆点",
                    "scores": {
                        "hook": 9,
                        "flow": 8,
                        "value": 9,
                        "trend": 7,
                        "reasons": {
                            "hook": "开口即价格",
                            "flow": "有报价有收尾",
                            "value": "省钱利益",
                            "trend": "低价福利",
                        },
                    },
                    "title": "mock 标题：39.9 拍一送三",
                    "caption": "mock 文案 #直播切片",
                }
            ]
        }


class MockHook(_AlwaysAvailable):
    name = "mock_hook"
    level = 1

    def generate(
        self, clip: ClipPlan, ctx: EngineContext, progress: ProgressFn
    ) -> HookAsset | None:
        return HookAsset(
            level=1,
            kind="text_overlay",
            text="mock 悬念句",
            meta={"mock": True},
        )


class MockRenderer(_AlwaysAvailable):
    name = "mock_render"

    def render(self, plan: RenderPlan, progress: ProgressFn) -> ArtifactRef:
        out = Path(plan.out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = f"MOCK ARTIFACT {plan.clip.candidate_id}\n".encode("utf-8")
        out.write_bytes(payload)
        progress(
            ProgressEvent(
                stage="rendering", done=1, total=1, unit="条", message="mock 渲染"
            )
        )
        return ArtifactRef(
            kind="mp4",
            file_path=str(out),
            size_bytes=len(payload),
            meta={"mock": True, "platform": plan.platform},
        )