"""Settings / Profile 测试（P0-2 验收：frozen、整体替换、双档案可加载）。"""
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from studio.settings import STAGES, load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


def test_load_cpu_cloud():
    s = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=Path("./.data-test"))
    assert s.profile == "cpu-cloud"
    assert s.chains.asr[0] == "bailian"
    assert s.chains.asr[-1] == "mock"
    assert s.allow_paid_generation is False
    assert s.data_dir == Path("./.data-test")


def test_load_gpu_local():
    s = load_settings(PROFILES_DIR, "gpu-local")
    assert s.chains.asr[0] == "whisper_gpu"
    assert "visual" in s.chains.signals
    assert s.chains.render[0] == "nvenc"
    assert s.max_concurrency == 2


def test_all_stages_present():
    for name in ("cpu-cloud", "gpu-local"):
        s = load_settings(PROFILES_DIR, name)
        for stage in STAGES:
            assert len(getattr(s.chains, stage)) >= 1


def test_frozen_no_field_mutation():
    s = load_settings(PROFILES_DIR, "cpu-cloud")
    with pytest.raises(FrozenInstanceError):
        s.profile = "gpu-local"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        s.chains.asr = ("mock",)  # type: ignore[misc]


def test_profile_switch_is_whole_replacement():
    a = load_settings(PROFILES_DIR, "cpu-cloud")
    b = load_settings(PROFILES_DIR, "gpu-local")
    assert a is not b
    assert a.chains != b.chains


def test_missing_profile_raises():
    with pytest.raises(FileNotFoundError):
        load_settings(PROFILES_DIR, "no-such-profile")