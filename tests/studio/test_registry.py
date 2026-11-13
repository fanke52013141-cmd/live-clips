"""EngineRegistry 测试（P0-2 验收：探测/降级显式记录/mock 兜底/未实现不报错）。"""
from pathlib import Path

import pytest

from studio.engines.base import EngineContext
from studio.engines.registry import EngineRegistry, EngineUnavailable
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


def make_ctx(gpu: bool = False) -> EngineContext:
    return EngineContext(
        profile="cpu-cloud",
        data_dir=Path("./.data-test"),
        models_dir=Path("./.data-test/models"),
        gpu_available=gpu,
        env={},
    )


def make_registry(profile: str = "cpu-cloud", gpu: bool = False) -> EngineRegistry:
    s = load_settings(PROFILES_DIR, profile)
    return EngineRegistry(s, make_ctx(gpu))


def test_p0_falls_back_to_mock_with_degradation_record(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    reg = make_registry()
    res = reg.resolve("asr")
    # P0 阶段：云缺 Key、本地缺包/缺模型 → 全部显式记降级，最终 mock 兜底
    assert res.name == "mock"
    assert res.engine.name == "mock_asr"
    names = res.degraded_names
    assert "bailian" in names and "whisper_cpu" in names and "paraformer_cpu" in names
    assert all(d.reason for d in res.degraded)  # 每条降级必须有原因
    assert any("DASHSCOPE_API_KEY" in d.reason for d in res.degraded)


def test_every_stage_resolves():
    reg = make_registry()
    for stage in ("asr", "diarize", "llm", "hooks", "render"):
        res = reg.resolve(stage)
        assert res.engine is not None


def test_signals_resolve_individually():
    reg = make_registry()
    res = reg.resolve_name("mock_signal")
    assert res.engine.name == "mock_signal"
    # P1-4 起 danmaku/audio 已实现（素材级探测在 extract）
    assert reg.resolve_name("danmaku").engine.name == "danmaku"
    with pytest.raises(EngineUnavailable):
        reg.resolve_name("visual")  # 未实现


def test_custom_factory_first_available_wins():
    class FakeASR:
        name = "bailian"

        def available(self, ctx):
            return True, ""

    reg = make_registry()
    reg._factories["bailian"] = FakeASR  # 测试注入
    res = reg.resolve("asr")
    assert res.name == "bailian"
    assert res.degraded == []


def test_unavailable_engine_degrades_with_reason():
    class NoKeyASR:
        name = "bailian"

        def available(self, ctx):
            return False, "缺少 DASHSCOPE_API_KEY"

    reg = make_registry()
    reg._factories["bailian"] = NoKeyASR
    res = reg.resolve("asr")
    assert res.name == "mock"
    assert res.degraded[0].engine == "bailian"
    assert "DASHSCOPE_API_KEY" in res.degraded[0].reason


def test_engine_instance_cached():
    reg = make_registry()
    r1 = reg.resolve("asr")
    r2 = reg.resolve("asr")
    assert r1.engine is r2.engine