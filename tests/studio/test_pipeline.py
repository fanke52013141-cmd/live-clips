"""管线编排测试（P0-5 验收：mock 全链路/数字进度/缓存续跑/取消/失败续跑/防幻觉）。"""
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import EngineContext
from studio.engines.mock import MockASR, MockLLM
from studio.engines.registry import EngineRegistry, default_factories
from studio.models import Base, StudioCandidate, StudioMaterial, StudioProject, StudioTask
from studio.pipeline.orchestrator import Pipeline
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)

    media = tmp_path / "media.mp4"
    media.write_bytes(b"fake-media-content")

    with sf() as s:
        m = StudioMaterial(
            source_type="local_file",
            source_uri=str(media),
            file_path=str(media),
            fingerprint="fp",
            duration_ms=60_000,
        )
        s.add(m)
        s.flush()
        p = StudioProject(
            material_id=m.id, name="测试", live_type="ecommerce", profile="cpu-cloud"
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
    yield sf, settings, ctx, pid
    engine.dispose()


def make_pipeline(env, factories=None):
    sf, settings, ctx, pid = env
    reg = EngineRegistry(settings, ctx, factories=factories)
    return Pipeline(settings, ctx, sf, registry=reg), pid, sf


def test_full_run_mock_pipeline(env):
    pipe, pid, sf = make_pipeline(env)
    events = []
    result = pipe.run(pid, progress=events.append)

    assert result.status == "candidates_ready"
    assert len(result.candidate_ids) == 1
    assert result.degraded  # 云缺 Key/本地缺包 → 显式降级记录
    assert result.error is None

    # 数字进度事件真实存在
    assert events
    assert all(isinstance(e.done, int) and isinstance(e.total, int) for e in events)
    assert any(e.unit for e in events)

    with sf() as s:
        cand = s.execute(select(StudioCandidate)).scalars().all()
        assert len(cand) == 1
        assert cand[0].layer == "clips0"
        assert cand[0].created_by == "ai"
        assert cand[0].rank_score is not None
        task = s.execute(select(StudioTask)).scalars().one()
        assert task.status == "done"
        assert task.receipt["degraded"]
        assert task.receipt["review"]  # P1-2：复审环每轮落回执
        assert task.receipt["review"][0]["verdict"] == "pass"
        proj = s.get(StudioProject, pid)
        assert proj.status == "candidates_ready"


def test_cache_resume_skips_retranscribe(env):
    calls = {"n": 0}

    class CountingASR(MockASR):
        def transcribe(self, *a, **kw):
            calls["n"] += 1
            return super().transcribe(*a, **kw)

    factories = default_factories()
    factories["mock_asr"] = CountingASR
    pipe, pid, sf = make_pipeline(env, factories)

    r1 = pipe.run(pid)
    r2 = pipe.run(pid)  # 续跑：转写必须命中缓存
    assert r1.status == r2.status == "candidates_ready"
    assert calls["n"] == 1


def test_cancel_before_run(env):
    pipe, pid, sf = make_pipeline(env)
    cancel = threading.Event()
    cancel.set()
    result = pipe.run(pid, cancel=cancel)
    assert result.status == "cancelled"
    assert result.candidate_ids == []
    with sf() as s:
        task = s.execute(select(StudioTask)).scalars().one()
        assert task.status == "cancelled"
        assert s.execute(select(StudioCandidate)).scalars().all() == []


def test_failure_keeps_cache_and_resumable(env):
    calls = {"n": 0}

    class CountingASR(MockASR):
        def transcribe(self, *a, **kw):
            calls["n"] += 1
            return super().transcribe(*a, **kw)

    class FailLLM(MockLLM):
        def chat_json(self, *a, **kw):
            raise RuntimeError("LLM 服务不可用")

    factories = default_factories()
    factories["mock_asr"] = CountingASR
    factories["mock_llm"] = FailLLM
    pipe, pid, sf = make_pipeline(env, factories)

    r1 = pipe.run(pid)
    assert r1.status == "failed"
    assert "LLM 服务不可用" in r1.error
    with sf() as s:
        task = s.execute(select(StudioTask)).scalars().one()
        assert task.status == "failed" and task.error

    # 换回正常 LLM 续跑：转写缓存仍在，不重转写
    factories["mock_llm"] = MockLLM
    pipe2, _, _ = make_pipeline(env, factories)
    r2 = pipe2.run(pid)
    assert r2.status == "candidates_ready"
    assert calls["n"] == 1


def test_fabricated_quote_discarded(env):
    class HallucinatingLLM(MockLLM):
        def chat_json(self, system, user, schema):
            out = super().chat_json(system, user, schema)
            out["candidates"][0]["quote"] = "这句话转写里根本没有"
            return out

    factories = default_factories()
    factories["mock_llm"] = HallucinatingLLM
    pipe, pid, sf = make_pipeline(env, factories)
    result = pipe.run(pid)

    assert result.status == "candidates_ready"
    assert result.candidate_ids == []  # 幻觉候选被丢弃
    assert len(result.discarded) == 1
    assert "防幻觉" in result.discarded[0]["reason"]
    with sf() as s:
        assert s.execute(select(StudioCandidate)).scalars().all() == []