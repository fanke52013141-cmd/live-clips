"""生成式钩子测试（P3：门槛/确认强制/预算/mock 标记/配置驱动不猜端点）。"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.api.app import create_app
from studio.engines.base import EngineContext
from studio.engines.gen_providers import CloudGenProvider, CostEstimate, MockGenProvider
from studio.models import Base, StudioArtifact, StudioCandidate, StudioMaterial, StudioProject
from studio.pipeline.hook_gen_service import HookGenError, estimate_hooks, generate_hooks
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'g.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri="x",
                           file_path="x.mp4", fingerprint="fp", duration_ms=60000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="钩子", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        cids = []
        for i in range(5):
            c = StudioCandidate(
                project_id=p.id, layer="clips1", start_ms=0, end_ms=16000,
                title=f"标题{i}", quote=f"金句{i}", rank_score=90.0 - i * 5,
                adopted=True, created_by="user:t",
                hook_plan={"l4_gen": {"prompt": f"自定义prompt{i}"}} if i == 0 else None,
            )
            s.add(c)
            s.flush()
            cids.append(c.id)
        s.commit()
        pid = p.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    yield sf, settings, ctx, pid, cids
    engine.dispose()


def test_estimate_l4_top3_gate(env):
    sf, _, ctx, pid, cids = env
    est = estimate_hooks(sf, pid, level=4, ctx=ctx)
    assert len(est["items"]) == 3  # L4 仅 top3
    assert est["items"][0]["candidate_id"] == cids[0]
    assert est["confirm_required"] is True and est["total"] == 0.0
    assert "top3" in est["gate"]


def test_estimate_l3_top5(env):
    sf, _, ctx, pid, _ = env
    assert len(estimate_hooks(sf, pid, level=3, ctx=ctx)["items"]) == 5


def test_generate_requires_confirm(env):
    sf, settings, ctx, pid, _ = env
    with pytest.raises(HookGenError, match="人工确认"):
        generate_hooks(sf, settings, ctx, pid, level=4, confirm=False)


def test_generate_mock_zero_cost_and_marker(env):
    sf, settings, ctx, pid, cids = env
    r = generate_hooks(sf, settings, ctx, pid, level=4, confirm=True)
    assert len(r["generated"]) == 3 and r["estimated_total"] == 0.0
    with sf() as s:
        arts = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "hook_video")).scalars().all()
        assert len(arts) == 3
        for a in arts:
            assert a.meta["mock"] is True and a.meta["aigc"] is True
            assert a.meta["cost"]["total"] == 0.0
            assert Path(a.file_path).is_file()
    # 自定义 prompt 优先（hook_plan.l4_gen.prompt）
    assert any(a.meta["prompt"] == "自定义prompt0" for a in arts)


def test_generate_out_of_gate_rejected(env):
    sf, settings, ctx, pid, cids = env
    with pytest.raises(HookGenError, match="门槛"):
        generate_hooks(sf, settings, ctx, pid, level=4, confirm=True, candidate_ids=[cids[4]])


def test_budget_cap_rejected(env):
    sf, settings, ctx, pid, _ = env

    class PaidMock(MockGenProvider):
        def __init__(self):
            super().__init__("video")
            self.name = "mock_video"

        def estimate(self, params):
            return CostEstimate(self.name, "video", 10.0, 1, note="测试定价")

    with pytest.raises(HookGenError, match="预算"):
        generate_hooks(sf, settings, ctx, pid, level=4, confirm=True,
                       max_cost=5.0, providers={"mock_video": PaidMock()})


def test_paid_switch_blocks_cloud(env):
    sf, settings, ctx, pid, _ = env

    class FakeCloud:
        name = "cloud_video"
        kind = "video"

        def available(self, ctx):
            return True, ""

        def estimate(self, params):
            return CostEstimate(self.name, "video", 1.0, 1)

        def generate(self, params, out_path, ctx):
            raise AssertionError("不应执行到生成")

    assert settings.allow_paid_generation is False
    with pytest.raises(HookGenError, match="付费生成开关"):
        generate_hooks(sf, settings, ctx, pid, level=4, confirm=True,
                       provider_name="cloud_video",
                       providers={"cloud_video": FakeCloud()})


def test_cloud_unavailable_reason_no_guessed_endpoint(env):
    _, _, ctx, _, _ = env
    ok, reason = CloudGenProvider("video").available(ctx)
    assert ok is False and "待配置" in reason and "不预设猜测地址" in reason


def test_api_hooks_flow(env):
    sf, settings, ctx, pid, _ = env
    client = TestClient(create_app(settings, ctx, sf))
    r = client.post(f"/studio/projects/{pid}/hooks/estimate", json={"level": 4})
    assert r.status_code == 200 and len(r.json()["items"]) == 3
    # 未确认 → 400
    r = client.post(f"/studio/projects/{pid}/hooks/generate", json={"level": 4, "confirm": False})
    assert r.status_code == 400 and "人工确认" in r.json()["detail"]
    r = client.post(f"/studio/projects/{pid}/hooks/generate", json={"level": 4, "confirm": True})
    assert r.status_code == 200 and len(r.json()["generated"]) == 3