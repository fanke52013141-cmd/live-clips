"""P4 工厂化核心测试（监听/闸门/发布泵 dry-run gated/战报）。"""
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.api.app import create_app
from studio.engines.base import EngineContext
from studio.factory.gate import gate_decision, gate_project
from studio.factory.pump import PublishBlocked, plan_pump, schedule_times
from studio.factory.report import battle_report
from studio.factory.watcher import ingest_stable, poll_stable
from studio.media import ffmpeg_available, make_lavfi_fixture
from studio.models import (
    Base,
    StudioArtifact,
    StudioCandidate,
    StudioFeedback,
    StudioMaterial,
    StudioProject,
    StudioPublication,
)
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'fac.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri="x",
                           file_path="x.mp4", fingerprint="fp-fac", duration_ms=20000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="工厂", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        c = StudioCandidate(project_id=p.id, layer="clips1", start_ms=0, end_ms=16000,
                            rank_score=90.0, adopted=True, created_by="user:t")
        s.add(c)
        s.flush()
        a = StudioArtifact(project_id=p.id, candidate_id=c.id, kind="mp4",
                           file_path="out.mp4", platform="douyin", content_id="cid-1", meta={})
        s.add(a)
        s.flush()
        pub = StudioPublication(artifact_id=a.id, platform="douyin", status="published",
                                published_at=datetime(2026, 11, 1))
        s.add(pub)
        s.flush()
        s.add(StudioFeedback(publication_id=pub.id,
                             metrics={"views": 1000, "likes": 50, "comments": 10, "shares": 5, "orders": 2}))
        s.commit()
        pid, cid, aid = p.id, c.id, a.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    yield sf, settings, ctx, pid, cid, aid, tmp_path
    engine.dispose()


# ---------- 监听 ----------

def test_watch_stability_two_polls(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    state = tmp_path / "state.json"
    f = d / "live.mp4"
    f.write_bytes(b"x" * 100)
    r1 = poll_stable(d, state)
    assert r1["stable"] == [] and str(f) in r1["pending"]  # 首轮只记录
    r2 = poll_stable(d, state)
    assert str(f) in r2["stable"]  # 第二轮大小不变→稳定
    # 仍在写入（大小变化）→ 不稳定
    f.write_bytes(b"x" * 200)
    r3 = poll_stable(d, state)
    assert str(f) in r3["pending"] and str(f) not in r3["stable"]


def test_watch_ignores_non_media(tmp_path):
    d = tmp_path / "rec"
    d.mkdir()
    (d / "note.txt").write_bytes(b"hello")
    state = tmp_path / "s.json"
    poll_stable(d, state)
    r = poll_stable(d, state)
    assert r["stable"] == [] and r["pending"] == []


@pytest.mark.skipif(not ffmpeg_available(), reason="ffmpeg 不可用")
def test_watch_ingest_dedup(env):
    sf, _, _, _, _, _, tmp = env
    d = tmp / "rec"
    d.mkdir()
    make_lavfi_fixture(d / "live.mp4", seconds=6)
    state = tmp / "s.json"
    poll_stable(d, state)  # 首轮记录
    r = ingest_stable(sf, d, state)  # 第二轮入库
    assert len(r["ingested"]) == 1 and r["ingested"][0]["duration_ms"] >= 5500
    # 再跑：指纹去重
    poll_stable(d, state)
    r2 = ingest_stable(sf, d, state)
    assert r2["ingested"] == [] and len(r2["skipped"]) == 1
    assert "指纹已入库" in r2["skipped"][0]["reason"]


# ---------- 闸门 ----------

def test_gate_hard_reject_blockers():
    d = gate_decision({"adopted": True, "rank_score": 95}, ["授权过期"])
    assert d["tier"] == "hard_reject"


def test_gate_hard_reject_rejected():
    assert gate_decision({"adopted": False, "rank_score": 95}, [])["tier"] == "hard_reject"


def test_gate_auto_pass():
    d = gate_decision({"adopted": True, "rank_score": 90, "scores": {}}, [])
    assert d["tier"] == "auto_pass"


def test_gate_human_default():
    assert gate_decision({"adopted": None, "rank_score": 90}, [])["tier"] == "human"
    assert gate_decision({"adopted": True, "rank_score": 70}, [])["tier"] == "human"
    assert gate_decision({"adopted": True, "rank_score": 95,
                          "scores": {"not_recommended": True}}, [])["tier"] == "human"


def test_gate_project_buckets():
    cs = [
        {"id": "a", "adopted": True, "rank_score": 90},
        {"id": "b", "adopted": None, "rank_score": 90},
        {"id": "c", "adopted": False, "rank_score": 90},
    ]
    g = gate_project(cs, [])
    assert g["auto_pass"] == ["a"] and g["human"] == ["b"] and g["hard_reject"] == ["c"]


# ---------- 发布泵 ----------

def test_schedule_spacing_and_jitter():
    start = datetime(2026, 11, 13, 12, 0, 0)
    t = schedule_times(3, interval_s=300, jitter_s=0, start=start, seed=1)
    assert (t[1] - t[0]).total_seconds() == 300
    assert (t[2] - t[1]).total_seconds() == 300
    # 抖动确定性（同 seed 同结果）
    a = schedule_times(5, interval_s=300, jitter_s=60, start=start, seed=42)
    b = schedule_times(5, interval_s=300, jitter_s=60, start=start, seed=42)
    assert a == b
    assert all(abs((a[i] - start).total_seconds() - 300 * i) <= 60 for i in range(5))


def test_pump_dry_run_plans(env):
    sf, _, _, pid, _, _, _ = env
    r = plan_pump(sf, pid, interval_s=300, jitter_s=0, seed=1)
    assert r["dry_run"] is True and r["count"] == 1
    with sf() as s:
        pubs = s.execute(select(StudioPublication).where(StudioPublication.status == "planned")).scalars().all()
        assert len(pubs) == 1


def test_pump_real_blocked(env):
    sf, _, _, pid, _, _, _ = env
    with pytest.raises(PublishBlocked, match="许可证"):
        plan_pump(sf, pid, dry_run=False)


# ---------- 战报 ----------

def test_battle_report_aggregates(env):
    sf, _, _, pid, _, _, _ = env
    r = battle_report(sf, pid)
    assert r["publications_with_data"] == 1
    assert r["totals"]["views"] == 1000 and r["totals"]["orders"] == 2
    assert r["by_platform"]["douyin"]["publications"] == 1
    assert r["top_clips"][0]["content_id"] == "cid-1"


def test_battle_report_empty(env):
    sf, _, _, _, _, _, _ = env
    r = battle_report(sf)  # 全局含一条
    assert r["publications_with_data"] >= 1


# ---------- API ----------

def test_api_factory(env):
    sf, settings, ctx, pid, _, _, tmp = env
    client = TestClient(create_app(settings, ctx, sf))
    # 闸门
    g = client.post(f"/studio/projects/{pid}/gate")
    assert g.status_code == 200 and g.json()["auto_pass"]
    # 泵 dry-run
    p = client.post(f"/studio/projects/{pid}/pump", json={"interval_s": 300, "jitter_s": 0})
    assert p.status_code == 200 and p.json()["dry_run"] is True
    # 真实发布 → 403
    p2 = client.post(f"/studio/projects/{pid}/pump", json={"dry_run": False})
    assert p2.status_code == 403 and "许可证" in p2.json()["detail"]
    # 战报
    b = client.get("/studio/battle-report", params={"project_id": pid})
    assert b.status_code == 200 and b.json()["totals"]["views"] == 1000
    # 监听（空目录）
    d = tmp / "empty"
    d.mkdir()
    w = client.post("/studio/factory/watch", json={"directory": str(d)})
    assert w.status_code == 200 and w.json()["ingested"] == []