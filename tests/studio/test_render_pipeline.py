"""渲染阶段测试（P2-1d：只读 adopted clips1 / 产物落库 / 封面降级 / API）。"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.api.app import create_app
from studio.engines.base import EngineContext
from studio.engines.registry import EngineRegistry, default_factories
from studio.media import ffmpeg_available, make_lavfi_fixture
from studio.models import (
    Base,
    StudioArtifact,
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTask,
)
from studio.pipeline.orchestrator import Pipeline
from studio.review_service import adopt_candidate
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)

    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake-media-content")

    with sf() as s:
        m = StudioMaterial(
            source_type="local_file", source_uri=str(media), file_path=str(media),
            fingerprint="fp", duration_ms=60_000,
        )
        s.add(m)
        s.flush()
        p = StudioProject(
            material_id=m.id, name="渲染测试", live_type="ecommerce", profile="cpu-cloud"
        )
        s.add(p)
        s.commit()
        pid = p.id

    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(
        profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path / "models",
        gpu_available=False,
    )
    yield sf, settings, ctx, pid
    engine.dispose()


def _mock_render_registry(settings, ctx):
    """mock 逻辑测试专用：本机有 ffmpeg 时 x264 会真实胜出，必须显式禁用，
    让渲染链降级到 mock（同时验证降级记录）。真实渲染走独立真实媒体用例。"""
    factories = default_factories()

    class _NoX264:
        name = "x264"

        def available(self, ctx):
            return False, "测试强制禁用 x264"

    factories["x264"] = _NoX264
    return EngineRegistry(settings, ctx, factories=factories)


def _analyze(env):
    sf, settings, ctx, pid = env
    pipe = Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx))
    result = pipe.run(pid)
    assert result.status == "candidates_ready"
    return pipe, result


def test_render_requires_adopted(env):
    pipe, _ = _analyze(env)
    with pytest.raises(ValueError, match="采纳"):
        pipe.render_project(env[3])


def test_render_adopted_only_and_artifacts(env):
    sf, settings, ctx, pid = env
    pipe, result = _analyze(env)
    c0 = result.candidate_ids[0]
    c1 = adopt_candidate(sf, c0, user="tester")
    # 另造一条未采纳 clips1：不得被渲染
    with sf() as s:
        s.add(StudioCandidate(
            project_id=pid, layer="clips1", parent_id=c0,
            start_ms=0, end_ms=16000, adopted=False, created_by="user:tester",
        ))
        s.commit()

    render_pipe = Pipeline(settings, ctx, sf, registry=_mock_render_registry(settings, ctx))
    events = []
    rr = render_pipe.render_project(pid, progress=events.append)
    assert rr.status == "rendered" and rr.error is None
    assert rr.candidate_ids == [c1]  # 只渲染采纳项
    assert events and all(isinstance(e.done, int) for e in events)
    # x264 被禁用 → 显式降级到 mock，记录在案
    assert any(d["engine"] == "x264" for d in rr.degraded)

    with sf() as s:
        arts = s.execute(select(StudioArtifact)).scalars().all()
        mp4s = [a for a in arts if a.kind == "mp4"]
        assert len(mp4s) == 1 and mp4s[0].candidate_id == c1
        assert mp4s[0].meta.get("mock") is True
        # mock 产物非真实媒体 → 封面失败必须显式降级记录，不静默
        assert any(d["engine"] == "cover" for d in rr.degraded)
        task = s.execute(
            select(StudioTask).where(StudioTask.kind == "render")
        ).scalars().one()
        assert task.status == "done"
        assert task.receipt["renders"][0]["cover"] is None
        assert s.get(StudioProject, pid).status == "rendered"


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")
def test_render_real_media_end_to_end(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'real.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    src = make_lavfi_fixture(tmp_path / "src.mp4", seconds=25)
    with sf() as s:
        m = StudioMaterial(
            source_type="local_file", source_uri=str(src), file_path=str(src),
            fingerprint="fp-real", duration_ms=25_000,
        )
        s.add(m)
        s.flush()
        p = StudioProject(
            material_id=m.id, name="真实渲染", live_type="ecommerce", profile="cpu-cloud"
        )
        s.add(p)
        s.commit()
        pid = p.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(
        profile="cpu-cloud", data_dir=tmp_path, models_dir=tmp_path / "models",
        gpu_available=False,
    )
    pipe = Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx))
    result = pipe.run(pid)  # mock ASR/LLM（模型目录隔离），真实渲染链 x264
    assert result.status == "candidates_ready"
    c1 = adopt_candidate(sf, result.candidate_ids[0], user="tester")

    rr = pipe.render_project(pid, portrait=False, cold_open=False)
    assert rr.status == "rendered", rr.error
    with sf() as s:
        arts = s.execute(select(StudioArtifact)).scalars().all()
        mp4 = next(a for a in arts if a.kind == "mp4")
        cover = next(a for a in arts if a.kind == "cover")
        assert Path(mp4.file_path).is_file() and mp4.size_bytes > 1000
        assert Path(cover.file_path).is_file()
        assert mp4.meta["selfcheck"]["streams"] == ["audio", "video"]
        assert abs(mp4.meta["selfcheck"]["duration_s"] - 16.0) <= 0.5
        assert cover.meta["chosen_score"]["rejected"] is None
    engine.dispose()


# ---------- API ----------

def test_api_render_flow(env):
    sf, settings, ctx, pid = env
    app = create_app(settings, ctx, sf, registry=_mock_render_registry(settings, ctx))
    client = TestClient(app)

    # 未采纳 → 后台任务 failed 带原因
    r = client.post(f"/studio/projects/{pid}/render", json={})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(50):
        t = client.get(f"/studio/projects/{pid}/task").json()
        if t and t["kind"] == "render" and t["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert t["status"] == "failed" and "采纳" in t["error"]

    # 采纳后渲染成功，产物可查
    pipe, result = _analyze(env)
    adopt_candidate(sf, result.candidate_ids[0], user="tester")
    r = client.post(f"/studio/projects/{pid}/render", json={"portrait": False})
    assert r.status_code == 200
    for _ in range(100):
        t = client.get(f"/studio/projects/{pid}/task").json()
        if t and t["kind"] == "render" and t["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert t["status"] == "done", t.get("error")
    arts = client.get(f"/studio/projects/{pid}/artifacts").json()
    assert any(a["kind"] == "mp4" for a in arts)


def test_api_render_unknown_project(env):
    sf, settings, ctx, _ = env
    app = create_app(settings, ctx, sf, registry=EngineRegistry(settings, ctx))
    client = TestClient(app)
    r = client.post("/studio/projects/no-such/render", json={})
    assert r.status_code == 404