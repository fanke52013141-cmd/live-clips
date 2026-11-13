"""信号抽取测试（P1-4 验收：弹幕双格式/反刷屏/降维/响度真实媒体）。"""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import EngineContext, MaterialRef
from studio.engines.signals_audio import LoudnessSignal
from studio.engines.signals_danmaku import (
    DanmakuSignal,
    bucket_counts,
    parse_danmaku,
)
from studio.media import ffmpeg_available, make_lavfi_fixture
from studio.models import Base, StudioMaterial, StudioProject, StudioTask
from studio.pipeline.orchestrator import Pipeline
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
CTX = EngineContext(profile="cpu-cloud", data_dir=Path("."), models_dir=Path("."), gpu_available=False)


def mat(tmp_path, danmaku=None, duration=20000):
    return MaterialRef(
        material_id="m", file_path=str(tmp_path / "m.mp4"),
        fingerprint="fp", duration_ms=duration, danmaku_path=danmaku,
    )


# ---------- 弹幕解析 ----------

def test_parse_xml_luboji(tmp_path):
    xml = tmp_path / "d.xml"
    xml.write_text(
        '<i><d p="1.5,1,25,16777215,1700000000,0,uid1,0">666</d>'
        '<d p="6.0,1,25,16777215,1700000001,0,uid2,0">价格真香</d></i>',
        encoding="utf-8",
    )
    events = parse_danmaku(xml)
    assert events == [(1500, "666", "uid1"), (6000, "价格真香", "uid2")]


def test_parse_jsonl_douyin(tmp_path):
    f = tmp_path / "d.jsonl"
    f.write_text(
        "\n".join([
            json.dumps({"time_ms": 2000, "content": "想要", "user": "u1"}),
            json.dumps({"timestamp": 7000, "text": "多少钱", "uid": "u2"}),
            "not-json",
        ]),
        encoding="utf-8",
    )
    events = parse_danmaku(f)
    assert events == [(2000, "想要", "u1"), (7000, "多少钱", "u2")]


def test_parse_corrupt_returns_empty(tmp_path):
    f = tmp_path / "bad.xml"
    f.write_text("<i><d p=", encoding="utf-8")
    assert parse_danmaku(f) == []
    assert parse_danmaku(tmp_path / "missing.xml") == []


# ---------- 计权与反刷屏 ----------

def test_bucket_counts_basic():
    events = [(1000, "a", "u1"), (6000, "b", "u2"), (6500, "c", "u3")]
    assert bucket_counts(events, 20000) == [1.0, 2.0, 0.0, 0.0]


def test_same_text_spam_capped():
    events = [(i * 100, "666", f"u{i}") for i in range(10)]  # 同桶同文本 10 条
    counts = bucket_counts(events, 20000)
    assert counts[0] == 3.0  # 封顶 3


def test_same_user_capped():
    events = [(i * 100, f"文本{i}", "spam-bot") for i in range(10)]  # 同用户不同文本
    counts = bucket_counts(events, 20000)
    assert counts[0] == 5.0  # 单用户封顶 5


def test_out_of_range_ignored():
    events = [(-100, "a", ""), (99999, "b", ""), (1000, "c", "")]
    assert bucket_counts(events, 20000) == [1.0, 0.0, 0.0, 0.0]


# ---------- 弹幕引擎降维 ----------

def test_danmaku_no_file_empty_series(tmp_path):
    track = DanmakuSignal().extract(mat(tmp_path), lambda ev: None)
    assert track.series == {}


def test_danmaku_with_file(tmp_path):
    xml = tmp_path / "d.xml"
    xml.write_text('<i><d p="1.0,1,25,0,0,0,u,0">666</d></i>', encoding="utf-8")
    track = DanmakuSignal().extract(mat(tmp_path, danmaku=str(xml)), lambda ev: None)
    assert track.series["danmaku"][0] == 1.0
    assert len(track.series["danmaku"]) == 4


# ---------- 响度（真实媒体） ----------

@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")
def test_loudness_sine_vs_silence(tmp_path):
    import subprocess

    sine = make_lavfi_fixture(tmp_path / "sine.mp4", seconds=12)
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
         "-t", "12", "-c:a", "aac", str(silent)],
        check=True, capture_output=True,
    )
    eng = LoudnessSignal()
    ok, _ = eng.available(CTX)
    assert ok
    m_sine = MaterialRef("m", str(sine), "fp", 12000)
    m_silent = MaterialRef("m", str(silent), "fp", 12000)
    s_sine = eng.extract(m_sine, lambda ev: None).series["loudness"]
    s_silent = eng.extract(m_silent, lambda ev: None).series["loudness"]
    assert len(s_sine) == 3 and len(s_silent) == 3
    assert max(s_sine) > 0.05  # 正弦有可测响度
    assert max(s_silent) < 0.001  # 静音近零
    assert s_sine[0] > s_silent[0]


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")
def test_loudness_invalid_media_degrades(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not-media")
    track = LoudnessSignal().extract(MaterialRef("m", str(bad), "fp", 10000), lambda ev: None)
    assert track.series == {}  # 解码失败不输出假零值


# ---------- 管线集成 ----------

@pytest.fixture()
def pipe_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 's.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake-media-content")
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    yield sf, settings, ctx, media, tmp_path
    engine.dispose()


def _make_project(sf, media, danmaku=None):
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri=str(media),
                           file_path=str(media), fingerprint="fp", duration_ms=60000,
                           danmaku_path=danmaku)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="信号测试", live_type="ecommerce",
                          profile="cpu-cloud")
        s.add(p)
        s.commit()
        return p.id


def test_pipeline_danmaku_track_present(pipe_env):
    sf, settings, ctx, media, tmp_path = pipe_env
    xml = tmp_path / "d.xml"
    xml.write_text('<i><d p="2.0,1,25,0,0,0,u,0">666</d></i>', encoding="utf-8")
    pid = _make_project(sf, media, danmaku=str(xml))
    result = Pipeline(settings, ctx, sf).run(pid)
    assert result.status == "candidates_ready"
    with sf() as s:
        task = s.execute(select(StudioTask)).scalars().one()
    # 弹幕路有数据；假媒体音频解码失败与 mock 兜底均显式记录
    assert not any(d["engine"] == "danmaku" for d in result.degraded)


def test_pipeline_no_danmaku_degrades(pipe_env):
    sf, settings, ctx, media, tmp_path = pipe_env
    pid = _make_project(sf, media, danmaku=None)
    result = Pipeline(settings, ctx, sf).run(pid)
    assert result.status == "candidates_ready"
    d = [x for x in result.degraded if x["engine"] == "danmaku"]
    assert d and "降维" in d[0]["reason"]