"""真实媒体集成测试（P0-6a：lavfi 夹具 + ffprobe 探测 + mock 管线全链路）。

无 ffmpeg 环境自动跳过（不伪造通过）。
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import EngineContext
from studio.media import ffmpeg_available, make_lavfi_fixture, probe_media
from studio.models import Base, StudioCandidate, StudioMaterial, StudioProject
from studio.pipeline.orchestrator import Pipeline
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg/ffprobe 不可用")


def test_probe_lavfi_fixture(tmp_path):
    media = make_lavfi_fixture(tmp_path / "sample.mp4", seconds=6)
    info = probe_media(media)
    assert 5500 <= info["duration_ms"] <= 6800
    assert info["width"] == 640 and info["height"] == 360
    assert info["fps"] == 25.0


def test_pipeline_on_real_media(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    media = make_lavfi_fixture(tmp_path / "live.mp4", seconds=8)
    info = probe_media(media)

    engine = create_engine(f"sqlite:///{(tmp_path / 'i.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(
            source_type="local_file",
            source_uri=str(media),
            file_path=str(media),
            fingerprint="fp-real",
            duration_ms=info["duration_ms"],
            width=info["width"],
            height=info["height"],
            fps=info["fps"],
        )
        s.add(m)
        s.flush()
        p = StudioProject(
            material_id=m.id, name="真实媒体集成", live_type="ecommerce", profile="cpu-cloud"
        )
        s.add(p)
        s.commit()
        pid = p.id

    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(
        profile="cpu-cloud",
        data_dir=tmp_path,
        models_dir=tmp_path / "models",
        gpu_available=False,
    )
    result = Pipeline(settings, ctx, sf).run(pid)
    assert result.status == "candidates_ready"
    assert len(result.candidate_ids) == 1

    # 续跑命中缓存（真实文件指纹）
    result2 = Pipeline(settings, ctx, sf).run(pid)
    assert result2.status == "candidates_ready"

    with sf() as s:
        cands = s.execute(select(StudioCandidate)).scalars().all()
        assert len(cands) == 2  # 两次 analyze 各产一条 clips0（转写只跑一次）
    engine.dispose()