"""mock 引擎全链路测试（P0-2 验收：无 Key/无 GPU 可跑通，产物显式标 mock）。"""
from pathlib import Path

from studio.engines.base import (
    ClipPlan,
    EngineContext,
    MaterialRef,
    RenderPlan,
    noop_progress,
)
from studio.engines.mock import (
    MockASR,
    MockDiarizer,
    MockHook,
    MockLLM,
    MockRenderer,
    MockSignal,
)


def make_ctx(tmp_path: Path) -> EngineContext:
    return EngineContext(
        profile="cpu-cloud",
        data_dir=tmp_path,
        models_dir=tmp_path / "models",
        gpu_available=False,
    )


def test_mock_asr_deterministic_and_monotonic():
    asr = MockASR()
    a = asr.transcribe("x.wav", hotwords=[], language="zh", progress=noop_progress)
    b = asr.transcribe("x.wav", hotwords=[], language="zh", progress=noop_progress)
    assert a == b  # 确定性
    assert a.word_level is True
    assert len(a.sentences) == 4
    ts = [w.start_ms for w in a.words]
    assert ts == sorted(ts)
    assert all(w.end_ms > w.start_ms for w in a.words)


def test_mock_asr_progress_reports_numbers():
    events = []
    MockASR().transcribe("x.wav", hotwords=[], language="zh", progress=events.append)
    assert len(events) == 4
    assert events[-1].done == 4 and events[-1].total == 4
    assert events[-1].unit == "句"


def test_mock_diarizer_labels_speakers():
    tr = MockASR().transcribe("x.wav", hotwords=[], language="zh", progress=noop_progress)
    tr2 = MockDiarizer().assign_speakers(tr, "x.wav", noop_progress)
    assert all(s.speaker == "spk0" for s in tr2.sentences)


def test_mock_signal_buckets():
    m = MaterialRef("m1", "x.mp4", "fp", duration_ms=60_000)
    track = MockSignal().extract(m, noop_progress)
    assert track.bucket_ms == 5000
    assert len(track.series["danmaku"]) == 12


def test_mock_llm_contract_shape():
    out = MockLLM().chat_json("sys", "user", {})
    c = out["candidates"][0]
    assert {"start_ms", "end_ms", "quote", "scores", "title"} <= set(c)
    assert {"hook", "flow", "value", "trend"} <= set(c["scores"])


def test_mock_hook_and_render(tmp_path):
    ctx = make_ctx(tmp_path)
    clip = ClipPlan("c1", 0, 4000, "t", "q")
    hook = MockHook().generate(clip, ctx, noop_progress)
    assert hook is not None and hook.level == 1 and hook.meta["mock"] is True

    plan = RenderPlan(clip, platform="douyin", portrait=True, subtitles=True,
                      out_path=str(tmp_path / "out" / "c1.mp4"))
    art = MockRenderer().render(plan, noop_progress)
    assert Path(art.file_path).exists()
    assert art.size_bytes > 0
    assert art.meta["mock"] is True