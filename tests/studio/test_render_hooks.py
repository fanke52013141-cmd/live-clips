"""管线级剪辑测试（P2-2/P2-3：去气口三重判定真实音频/冷开场定位/失败自动跳过）。"""
import wave
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import EngineContext
from studio.engines.registry import EngineRegistry
from studio.media import ffmpeg_available
from studio.models import (
    Base,
    StudioArtifact,
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTranscript,
)
from studio.pipeline.orchestrator import Pipeline
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
pytestmark_media = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")


def make_speech_fixture(out: Path, seconds: int = 12) -> Path:
    """类语音夹具：正弦 bursts + 真实静音间隙（2-3.2s 长间隙 / 6-6.3s 短间隙）。"""
    import subprocess

    sr = 44100
    t = np.arange(int(seconds * sr)) / sr
    audio = np.zeros_like(t)
    for a, b in ((0.0, 2.0), (3.2, 6.0), (6.3, float(seconds))):
        m = (t >= a) & (t < b)
        audio[m] = 0.5 * np.sin(2 * np.pi * 440 * t[m])
    pcm = (audio * 32767).astype(np.int16)
    wav = out.with_suffix(".wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={seconds}",
            "-i", str(wav),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(out),
        ],
        capture_output=True, text=True, check=True,
    )
    return out


WORDS = [
    {"text": "价", "start_ms": 0, "end_ms": 2000, "speaker": None},
    {"text": "格", "start_ms": 3200, "end_ms": 6000, "speaker": None},
    {"text": "好", "start_ms": 6300, "end_ms": 12000, "speaker": None},
]
SENTENCES = [
    {"text": "价格三十九块九", "start_ms": 0, "end_ms": 6000, "speaker": None},
    {"text": "好今天下单", "start_ms": 6300, "end_ms": 12000, "speaker": None},
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path / "ms-empty"))
    engine = create_engine(f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    src = make_speech_fixture(tmp_path / "speech.mp4")
    with sf() as s:
        m = StudioMaterial(
            source_type="local_file", source_uri=str(src), file_path=str(src),
            fingerprint="fp-speech", duration_ms=12_000,
        )
        s.add(m)
        s.flush()
        p = StudioProject(
            material_id=m.id, name="钩子测试", live_type="ecommerce", profile="cpu-cloud"
        )
        s.add(p)
        s.flush()
        s.add(StudioTranscript(
            material_id=m.id, cache_key="test-key", engine="test", model="test",
            words=WORDS, sentences=SENTENCES, word_level=True,
        ))
        c0 = StudioCandidate(
            project_id=p.id, layer="clips0", start_ms=0, end_ms=12000,
            quote="价格", title="t", created_by="ai",
        )
        s.add(c0)
        s.flush()
        c1 = StudioCandidate(
            project_id=p.id, layer="clips1", parent_id=c0.id,
            start_ms=0, end_ms=12000, quote="价格", title="t",
            adopted=True, created_by="user:tester",
        )
        s.add(c1)
        s.commit()
        pid = p.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(
        profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path / "models",
        gpu_available=False,
    )
    yield sf, settings, ctx, pid
    engine.dispose()


def _pipe(env):
    sf, settings, ctx, pid = env
    return Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx)), pid


def test_debreath_real_silence(env):
    """长间隙(1.2s)三重判定通过被剪；短间隙(0.3s)不剪。"""
    sf, _, _, pid = env
    pipe, _ = _pipe(env)
    rr = pipe.render_project(pid, portrait=False, debreath=True, cold_open=False)
    assert rr.status == "rendered", rr.error
    with sf() as s:
        task_receipt = None
        from studio.models import StudioTask
        task_receipt = s.execute(
            select(StudioTask).where(StudioTask.kind == "render")
        ).scalars().one().receipt
        edits = task_receipt["renders"][0]["edits"]
        assert len(edits["debreath"]) == 1  # 只剪长间隙
        cut = edits["debreath"][0]
        assert cut["start_ms"] == 2125 and cut["end_ms"] == 3075  # 保留 250ms 呼吸口
        assert edits["vad"] == "energy_rms_v1"
        mp4 = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        # 12s - 0.95s ≈ 11.05s
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 11.05) <= 0.3
        assert mp4.meta["editlist"]["cuts"] == [{"start_ms": 2125, "end_ms": 3075}]


def test_cold_open_quote_located(env):
    sf, _, _, pid = env
    pipe, _ = _pipe(env)
    rr = pipe.render_project(pid, portrait=False, cold_open=True)
    assert rr.status == "rendered", rr.error
    with sf() as s:
        from studio.models import StudioTask
        receipt = s.execute(
            select(StudioTask).where(StudioTask.kind == "render")
        ).scalars().one().receipt
        edits = receipt["renders"][0]["edits"]
        assert edits["cold_open"] == [0, 6000] or edits["cold_open"] == (0, 6000)
        mp4 = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        # 钩子句 6s 复制到片头，原位保留 → 18s
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 18.0) <= 0.3
        assert mp4.meta["editlist"]["segments"][0]["kind"] == "cold_open"


def test_cold_open_locate_fail_auto_skip(env):
    """quote 定位失败 → 自动跳过 + 显式记录，渲染不阻断。"""
    sf, _, _, pid = env
    with sf() as s:
        c1 = s.execute(
            select(StudioCandidate).where(StudioCandidate.layer == "clips1")
        ).scalars().one()
        c1.quote = "这句话不存在于转写中"
        s.commit()
    pipe, _ = _pipe(env)
    rr = pipe.render_project(pid, portrait=False, cold_open=True)
    assert rr.status == "rendered", rr.error
    assert any(d["engine"] == "hook_locate" and "cold_open" in d["reason"] for d in rr.degraded)
    with sf() as s:
        mp4 = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 12.0) <= 0.3
        assert mp4.meta["editlist"]["cold_open"] is None


def test_l2_tts_pipeline_rank_gate(env):
    """L2 管线全链路：rank 达门槛+注入 TTS → 混音成片；未达门槛 → 跳过。"""
    from studio.engines.hooks_tts import TtsHook
    from studio.engines.registry import default_factories
    from studio.models import StudioTask

    sf, settings, ctx, pid = env
    with sf() as s:
        c1 = s.execute(
            select(StudioCandidate).where(StudioCandidate.layer == "clips1")
        ).scalars().one()
        c1.rank_score = 90.0
        s.commit()

    def fake_synth(text, out):
        import numpy as np

        sr = 44100
        t = np.arange(sr) / sr
        pcm = (0.4 * np.sin(2 * np.pi * 880 * t) * 32767).astype(np.int16)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())

    factories = default_factories()
    factories["edge_tts"] = lambda: TtsHook(synth=fake_synth)
    pipe = Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx, factories=factories))
    rr = pipe.render_project(pid, portrait=False, cold_open=False, l2_tts=True)
    assert rr.status == "rendered", rr.error
    with sf() as s:
        receipt = s.execute(select(StudioTask).where(StudioTask.kind == "render")).scalars().one().receipt
        hooks = receipt["renders"][0]["hooks"]
        assert hooks["decision"]["l2"] is True
        assert hooks["l2"]["engine"] == "injected"
        mp4 = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        assert mp4.meta["hooks"]["l2_audio"] is not None
        # TTS 叠加不改变时长
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 12.0) <= 0.3

    # rank 未达门槛 → L2 不做
    with sf() as s:
        c1 = s.execute(
            select(StudioCandidate).where(StudioCandidate.layer == "clips1")
        ).scalars().one()
        c1.rank_score = 50.0
        s.commit()
    rr2 = pipe.render_project(pid, portrait=False, cold_open=False, l2_tts=True)
    assert rr2.status == "rendered", rr2.error
    with sf() as s:
        tasks = s.execute(
            select(StudioTask).where(StudioTask.kind == "render").order_by(StudioTask.created_at.desc())
        ).scalars().all()
        hooks = tasks[0].receipt["renders"][0]["hooks"]
        assert hooks["decision"]["l2"] is False
        assert hooks["l2"] is None