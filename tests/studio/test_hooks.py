"""钩子测试（P2-4/P2-5：决策树/L1 贴片/L2 TTS/渲染混音/管线门槛）。"""
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from studio.engines.base import ClipPlan, EngineContext, RenderPlan, Word
from studio.engines.hooks_text import TextOverlayHook
from studio.engines.hooks_tts import TtsHook
from studio.engines.render_ffmpeg import FfmpegRenderer, build_ass
from studio.media import ffmpeg_available, make_lavfi_fixture
from studio.pipeline.hook_decision import clamp_hook_text, decide_hooks

CTX = EngineContext(profile="cpu-cloud", data_dir=Path("."), models_dir=Path("."), gpu_available=False)
pytestmark_media = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")


def clip(**kw):
    defaults = dict(candidate_id="c1", start_ms=2000, end_ms=8000, title="标题", quote="金句")
    defaults.update(kw)
    return ClipPlan(**defaults)


# ---------- 决策树 ----------

def test_native_used_suppresses_l1():
    d = decide_hooks(native_used=True)
    assert d.l1 is False and "原生钩子" in d.reasons[0]


def test_l1_default_on_without_native():
    assert decide_hooks(native_used=False).l1 is True
    assert decide_hooks(native_used=False, l1_enabled=False).l1 is False


def test_l2_rank_threshold():
    assert decide_hooks(native_used=True, l2_enabled=True, rank_score=85).l2 is True
    d = decide_hooks(native_used=True, l2_enabled=True, rank_score=70)
    assert d.l2 is False and any("门槛" in r for r in d.reasons)
    assert decide_hooks(native_used=True, l2_enabled=True, rank_score=None).l2 is False


def test_clamp_hook_text():
    assert clamp_hook_text("  ") is None
    assert clamp_hook_text("") is None
    assert clamp_hook_text("一二三四五六七八九十一二三四五六七八九十一二三") == "一二三四五六七八九十一二三四五六七八九十"
    assert clamp_hook_text("你好 世界") == "你好世界"


# ---------- L1 引擎 ----------

def test_text_overlay_from_hook_plan():
    c = clip(hook_plan={"l1_text": {"text": "悬念句"}})
    a = TextOverlayHook().generate(c, CTX, lambda ev: None)
    assert a.text == "悬念句" and a.level == 1
    assert a.meta["duration_s"] == 2.0


def test_text_overlay_fallback_title_and_none():
    a = TextOverlayHook().generate(clip(title=" fallback "), CTX, lambda ev: None)
    assert a.text == "fallback"
    assert TextOverlayHook().generate(clip(title="", quote=""), CTX, lambda ev: None) is None


# ---------- L2 TTS ----------

def test_tts_injected_synth(tmp_path):
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path, gpu_available=False)

    def fake_synth(text, out):
        out.write_bytes(b"RIFF" + b"\x00" * 200)

    a = TtsHook(synth=fake_synth).generate(clip(), ctx, lambda ev: None)
    assert a is not None and a.kind == "tts_audio"
    assert a.meta["engine"] == "injected" and Path(a.file_path).is_file()


def test_tts_empty_output_returns_none(tmp_path):
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path, gpu_available=False)
    a = TtsHook(synth=lambda t, o: o.write_bytes(b"")).generate(clip(), ctx, lambda ev: None)
    assert a is None  # 空产物不冒充成功


def test_tts_no_text_returns_none(tmp_path):
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path, gpu_available=False)
    a = TtsHook(synth=lambda t, o: None).generate(clip(title="", quote=""), ctx, lambda ev: None)
    assert a is None


@pytest.mark.skipif(sys.platform != "win32", reason="SAPI 仅 Windows")
def test_tts_edge_fail_sapi_fallback(tmp_path, monkeypatch):
    """edge-tts 失败（本机已知 NoAudioReceived）→ SAPI 离线兜底真实合成。"""
    import studio.engines.hooks_tts as mod

    def broken_edge(text, out, voice):
        raise RuntimeError("NoAudioReceived (模拟本机已知故障)")

    monkeypatch.setattr(mod, "_edge_tts", broken_edge)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path, gpu_available=False)
    a = TtsHook().generate(clip(hook_plan={"l2_tts": {"text": "三十九块九拍一发三"}}), ctx, lambda ev: None)
    assert a is not None, "SAPI 兜底应成功"
    assert a.meta["engine"] == "sapi_offline"
    assert Path(a.file_path).is_file() and Path(a.file_path).stat().st_size > 1000


# ---------- 渲染：贴片与混音（真实媒体） ----------

def words_2_to_8():
    # 词表必须铺满 2-8s：词边界吸附会把出点拉到最近词尾，
    # 词表过短会导致出点合法前移（曾误判为混音截断缺陷）
    text = "价格三十九块九拍一发三"
    out = []
    t = 2000
    i = 0
    while t < 8000:
        out.append(Word(text[i % len(text)], t, t + 300))
        t += 300
        i += 1
    return tuple(out)


def make_wav(path: Path, seconds: float = 1.0) -> Path:
    sr = 44100
    t = np.arange(int(seconds * sr)) / sr
    pcm = (0.4 * np.sin(2 * np.pi * 880 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def test_ass_hook_overlay_style():
    ass = build_ass(
        [Word("你", 2000, 2300)], 2000, 2300, portrait=False,
        hook_overlay={"text": "悬念句", "duration_s": 2.0},
    )
    assert "Style: Hook," in ass
    assert "{\\fad(200,300)}悬念句" in ass
    assert "Dialogue: 1,0:00:00.00,0:00:02.00,Hook," in ass


@pytestmark_media
def test_render_with_overlay(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    p = RenderPlan(
        clip=clip(), platform="douyin", portrait=False, subtitles=True,
        out_path=str(tmp_path / "out.mp4"), source_path=str(src), words=words_2_to_8(),
        hook_overlay={"text": "悬念句", "duration_s": 2.0},
    )
    art = FfmpegRenderer().render(p, lambda ev: None)
    assert art.meta["hooks"]["l1_overlay"]["text"] == "悬念句"
    ass = Path(art.file_path).with_name("subs_c1.ass").read_text(encoding="utf-8")
    assert "Style: Hook," in ass


@pytestmark_media
def test_render_with_tts_plain_path(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    tts = make_wav(tmp_path / "tts.wav", 1.0)
    p = RenderPlan(
        clip=clip(), platform="douyin", portrait=False, subtitles=True,
        out_path=str(tmp_path / "out.mp4"), source_path=str(src), words=words_2_to_8(),
        hook_audio=str(tts),
    )
    art = FfmpegRenderer().render(p, lambda ev: None)
    # TTS 叠加不改变时长；混音后仍双流的
    assert abs(art.meta["selfcheck"]["duration_s"] - 6.0) <= 0.25
    assert art.meta["selfcheck"]["streams"] == ["audio", "video"]
    assert art.meta["hooks"]["l2_audio"] == str(tts)
    assert art.meta["hooks"]["l2_duration_s"] == 1.0


@pytestmark_media
def test_render_with_tts_and_cuts_segment_path(tmp_path):
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=12)
    tts = make_wav(tmp_path / "tts.wav", 1.0)
    p = RenderPlan(
        clip=clip(), platform="douyin", portrait=False, subtitles=True,
        out_path=str(tmp_path / "out.mp4"), source_path=str(src), words=words_2_to_8(),
        cuts=((4000, 5000),), hook_audio=str(tts),
    )
    art = FfmpegRenderer().render(p, lambda ev: None)
    assert abs(art.meta["selfcheck"]["duration_s"] - 5.0) <= 0.25
    assert art.meta["hooks"]["l2_audio"] == str(tts)