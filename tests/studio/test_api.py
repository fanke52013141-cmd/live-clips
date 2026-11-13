"""studio API 测试（P1-6b 验收：审核流/错误码/提示词/引擎探测/后台管线/取消）。"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.api.app import create_app
from studio.engines.base import EngineContext, ProgressEvent
from studio.engines.mock import MockASR
from studio.engines.registry import EngineRegistry, default_factories
from studio.models import Base, StudioCandidate, StudioMaterial, StudioProject
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    media = tmp_path / "m.mp4"
    media.write_bytes(b"fake-media")
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri=str(media),
                           file_path=str(media), fingerprint="fp", duration_ms=60000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="p", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        c0 = StudioCandidate(project_id=p.id, layer="clips0", start_ms=0, end_ms=20000,
                             title="AI 标题", quote="原文", rank_score=88.0, created_by="ai")
        s.add(c0)
        s.commit()
        pid, cid = p.id, c0.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    yield sf, settings, ctx, pid, cid, tmp_path
    engine.dispose()


def make_client(env, factories=None):
    sf, settings, ctx, pid, cid, tmp_path = env
    reg = EngineRegistry(settings, ctx, factories=factories)
    return TestClient(create_app(settings, ctx, sf, registry=reg)), pid, cid


def test_review_flow(env):
    client, pid, cid = make_client(env)
    r = client.get(f"/studio/projects/{pid}/candidates", params={"layer": "clips0"})
    assert r.status_code == 200 and len(r.json()) == 1

    r = client.post(f"/studio/candidates/{cid}/adopt",
                    json={"user": "alice", "end_ms": 30000, "title": "人工标题"})
    assert r.status_code == 200
    c1id = r.json()["clips1_id"]

    r = client.patch(f"/studio/clips1/{c1id}",
                     json={"user": "alice", "start_ms": 500, "adopted": True})
    assert r.status_code == 200 and r.json()["start_ms"] == 500

    r = client.get(f"/studio/projects/{pid}/candidates", params={"layer": "clips1"})
    assert len(r.json()) == 1 and r.json()[0]["parent_id"] == cid


def test_review_errors(env):
    client, pid, cid = make_client(env)
    r = client.post(f"/studio/candidates/{cid}/adopt", json={"user": "u", "end_ms": 999999})
    assert r.status_code == 400 and "超素材时长" in r.json()["detail"]
    r = client.post(f"/studio/candidates/{cid}/reject", json={"user": "u", "reason": " "})
    assert r.status_code == 400
    r = client.post(f"/studio/candidates/{cid}/reject", json={"user": "u", "reason": "不行"})
    assert r.status_code == 200
    r = client.patch(f"/studio/clips1/{cid}", json={"user": "u", "title": "x"})
    assert r.status_code == 400  # clips0 不可精修
    r = client.get("/studio/projects/no-such/transcript")
    assert r.status_code == 404
    r = client.get(f"/studio/projects/{pid}/candidates", params={"layer": "clips9"})
    assert r.status_code == 400


def test_transcript_null_before_run(env):
    client, pid, _ = make_client(env)
    assert client.get(f"/studio/projects/{pid}/transcript").json() is None
    assert client.get(f"/studio/projects/{pid}/task").json() is None


def test_prompts_crud(env):
    client, _, _ = make_client(env)
    r = client.post("/studio/prompts", json={"name": "带货", "body": "挑爆点", "live_type": "ecommerce"})
    assert r.status_code == 200
    pid = r.json()["id"]
    r = client.patch(f"/studio/prompts/{pid}", json={"body": "挑爆点 v2"})
    assert r.json()["version"] == 2
    r = client.get("/studio/prompts", params={"live_type": "ecommerce"})
    assert len(r.json()) == 1
    assert client.delete(f"/studio/prompts/{pid}").json()["deleted"] is True
    r = client.post("/studio/prompts", json={"name": "x", "body": " "})
    assert r.status_code == 400


def test_engines_probe(env):
    client, _, _ = make_client(env)
    r = client.get("/studio/engines")
    assert r.status_code == 200
    data = r.json()
    assert data["asr"]["engine"] == "mock"  # 封闭环境：云缺 Key/本地缺模型
    assert data["asr"]["degraded"]
    assert data["signals"]["danmaku"]["available"] is True


def test_run_pipeline_and_task(env):
    client, pid, _ = make_client(env)
    r = client.post(f"/studio/projects/{pid}/run")
    assert r.status_code == 200 and r.json()["started"] is True
    deadline = time.time() + 30
    status = None
    while time.time() < deadline:
        t = client.get(f"/studio/projects/{pid}/task").json()
        if t and t["status"] in ("done", "failed", "cancelled"):
            status = t["status"]
            break
        time.sleep(0.2)
    assert status == "done"
    cands = client.get(f"/studio/projects/{pid}/candidates", params={"layer": "clips0"}).json()
    assert len(cands) == 2  # fixture 1 条 + 管线 1 条
    tr = client.get(f"/studio/projects/{pid}/transcript").json()
    assert tr and tr["word_level"] is True


class SlowASR(MockASR):
    def transcribe(self, audio_path, *, hotwords, language, progress):
        time.sleep(1.0)
        return super().transcribe(audio_path, hotwords=hotwords, language=language, progress=progress)


def test_double_run_409_and_cancel(env):
    factories = default_factories()
    factories["mock_asr"] = SlowASR
    client, pid, _ = make_client(env, factories)
    assert client.post(f"/studio/projects/{pid}/run").status_code == 200
    assert client.post(f"/studio/projects/{pid}/run").status_code == 409
    assert client.post(f"/studio/projects/{pid}/cancel").json()["cancelled"] is True
    deadline = time.time() + 30
    status = None
    while time.time() < deadline:
        t = client.get(f"/studio/projects/{pid}/task").json()
        if t and t["status"] in ("done", "failed", "cancelled"):
            status = t["status"]
            break
        time.sleep(0.2)
    assert status == "cancelled"
    assert client.post(f"/studio/projects/{pid}/cancel").json()["cancelled"] is False


# ---------- 素材入库与项目（P1-6c 前端端到端必需） ----------

def test_material_ingest_and_project_create(env, tmp_path):
    from studio.media import ffmpeg_available, make_lavfi_fixture

    if not ffmpeg_available():
        pytest.skip("ffmpeg 不可用")
    client, _, _ = make_client(env)
    media = make_lavfi_fixture(tmp_path / "ingest.mp4", seconds=6)
    r = client.post("/studio/materials", json={"file_path": str(media)})
    assert r.status_code == 200
    m = r.json()
    assert 5500 <= m["duration_ms"] <= 6800 and m["width"] == 640
    assert len(client.get("/studio/materials").json()) >= 1

    r = client.post("/studio/projects", json={
        "material_id": m["id"], "name": "端到端", "live_type": "ecommerce",
        "intent": "只要价格", "background": "卖耳机",
    })
    assert r.status_code == 200
    pid = r.json()["id"]
    projects = client.get("/studio/projects").json()
    assert any(p["id"] == pid and p["status"] == "ingested" for p in projects)


def test_material_ingest_errors(env, tmp_path):
    client, _, _ = make_client(env)
    r = client.post("/studio/materials", json={"file_path": str(tmp_path / "no.mp4")})
    assert r.status_code == 400 and "不存在" in r.json()["detail"]
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not-media")
    r = client.post("/studio/materials", json={"file_path": str(bad)})
    assert r.status_code == 400 and "探测失败" in r.json()["detail"]
    r = client.post("/studio/projects", json={
        "material_id": "no-such", "name": "x", "live_type": "ecommerce",
    })
    assert r.status_code == 400


# ---------- 产物文件服务与平台预设（前端批 F-1） ----------

def test_artifact_file_serving(env, tmp_path):
    from studio.models import StudioArtifact

    sf, settings, ctx, pid, _, _ = env
    client, _, _ = make_client(env)
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes" * 100)
    with sf() as s:
        a = StudioArtifact(project_id=pid, kind="mp4", file_path=str(f),
                           size_bytes=f.stat().st_size, meta={})
        s.add(a)
        s.commit()
        aid = a.id
    r = client.get(f"/studio/artifacts/{aid}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/mp4")
    assert r.content == f.read_bytes()
    # 不存在的产物 → 404
    assert client.get("/studio/artifacts/no-such/file").status_code == 404
    # 记录存在但文件缺失 → 404（不泄露任意路径读取）
    f.unlink()
    assert client.get(f"/studio/artifacts/{aid}/file").status_code == 404


def test_platforms_endpoint(env):
    client, _, _ = make_client(env)
    r = client.get("/studio/platforms")
    assert r.status_code == 200
    data = r.json()
    assert {"douyin", "kuaishou", "bilibili", "xhs", "shipinhao"} <= set(data)
    assert data["douyin"]["safe_area"]["bottom"] > 0


# ---------- 审核台时间轴数据源（R-1） ----------

def test_signals_endpoint(env):
    from studio.pipeline.orchestrator import Pipeline

    sf, settings, ctx, pid, _, _ = env
    client, _, _ = make_client(env)
    # 未运行 → null
    assert client.get(f"/studio/projects/{pid}/signals").json() is None
    Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx)).run(pid)
    r = client.get(f"/studio/projects/{pid}/signals")
    assert r.status_code == 200
    tracks = r.json()
    # 信号轨按链名存储（signals 链尾 mock → 键 "mock"）
    assert tracks and "mock" in tracks
    assert "danmaku" in tracks["mock"]["series"]
    assert tracks["mock"]["bucket_ms"] == 5000


def test_material_file_serving(env):
    sf, _, _, _, _, _ = env
    client, _, _ = make_client(env)
    with sf() as s:
        mid = s.execute(select(StudioMaterial)).scalars().first().id
    r = client.get(f"/studio/materials/{mid}/file")
    assert r.status_code == 200 and r.content == b"fake-media"
    assert client.get("/studio/materials/no-such/file").status_code == 404


def test_waveform_endpoint_and_cache(env, tmp_path):
    from studio.media import ffmpeg_available, make_lavfi_fixture

    if not ffmpeg_available():
        pytest.skip("ffmpeg 不可用")
    client, _, _ = make_client(env)
    media = make_lavfi_fixture(tmp_path / "wf.mp4", seconds=6)
    m = client.post("/studio/materials", json={"file_path": str(media)}).json()
    p = client.post("/studio/projects", json={
        "material_id": m["id"], "name": "波形", "live_type": "ecommerce",
    }).json()
    r = client.get(f"/studio/projects/{p['id']}/waveform")
    assert r.status_code == 200
    data = r.json()
    assert data["buckets"] == 1000 and len(data["peaks"]) == 1000
    assert max(data["peaks"]) == 1.0  # 归一化
    assert sum(data["peaks"]) > 0  # 正弦音轨非静音
    assert data["cached"] is False
    # 二次请求命中指纹缓存
    r2 = client.get(f"/studio/projects/{p['id']}/waveform")
    assert r2.json()["cached"] is True
    assert r2.json()["peaks"] == data["peaks"]