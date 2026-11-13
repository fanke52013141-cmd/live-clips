"""回流对账测试（P3-5：CSV 导入/保守匹配/歧义显式/A/B 门槛）。"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.api.app import create_app
from studio.engines.base import EngineContext
from studio.feedback.reconcile import FeedbackError, build_ab_report, import_feedback, parse_csv
from studio.models import (
    Base,
    StudioArtifact,
    StudioFeedback,
    StudioMaterial,
    StudioProject,
    StudioPublication,
)
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
NOW = datetime(2026, 11, 13, 12, 0, 0)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'f.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri="x",
                           file_path="x.mp4", fingerprint="fp", duration_ms=60000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="回流", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        # 两条成片：不同钩子策略，同平台
        a1 = StudioArtifact(
            project_id=p.id, kind="mp4", file_path="a1.mp4", content_id="proj0001-cand0001",
            platform="douyin", meta={"hooks": {"l1_overlay": {"text": "x"}}},
        )
        a2 = StudioArtifact(
            project_id=p.id, kind="mp4", file_path="a2.mp4", content_id="proj0001-cand0002",
            platform="douyin", meta={"hooks": {}},
        )
        s.add_all([a1, a2])
        s.flush()
        # 发布记录：a1 两条（不同钩子组各需 n≥2 → 再加两条同策略发布）
        pubs = []
        for art, hours, views in ((a1, 100, 1000), (a1, 100, 2000), (a2, 100, 800), (a2, 100, 900)):
            pub = StudioPublication(
                artifact_id=art.id, platform="douyin", account="acc",
                url=f"http://v/{art.id}-{hours}-{views}", published_at=NOW - timedelta(hours=hours),
                status="published",
            )
            s.add(pub)
            s.flush()
            s.add(StudioFeedback(
                publication_id=pub.id,
                metrics={"views": views, "likes": views * 0.05, "comments": views * 0.01,
                         "shares": views * 0.005, "orders": 0},
            ))
            pubs.append(pub.id)
        s.commit()
        pid, aid1, aid2 = p.id, a1.id, a2.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    yield sf, settings, ctx, pid, aid1, aid2, tmp_path
    engine.dispose()


def _csv(tmp: Path, text: str) -> Path:
    p = tmp / "fb.csv"
    p.write_text(text, encoding="utf-8")
    return p


# ---------- CSV 解析 ----------

def test_parse_chinese_headers_and_wan(tmp_path):
    p = _csv(tmp_path, "平台,内容ID,播放量,点赞,发布时间\ndouyin,proj0001-cand0001,1.2万,300,2026-11-01 10:00:00\n")
    rows = parse_csv(p)
    assert rows[0]["views"] == 12000.0
    assert rows[0]["likes"] == 300.0
    assert rows[0]["published_at"] == datetime(2026, 11, 1, 10, 0, 0)


def test_parse_missing_file():
    with pytest.raises(FeedbackError, match="不存在"):
        parse_csv("no-such.csv")


# ---------- 导入匹配 ----------

def test_import_content_id_exact(env):
    sf, _, _, _, aid1, _, tmp = env
    p = _csv(tmp, "platform,content_id,url,views,likes\n"
                  "douyin,proj0001-cand0001,http://new/1,5000,250\n")
    r = import_feedback(sf, p)
    assert r["imported"] == 1 and r["ambiguous"] == [] and r["unmatched"] == []
    assert r["matched"][0]["artifact_id"] == aid1
    with sf() as s:
        fb = s.execute(select(StudioFeedback).order_by(StudioFeedback.imported_at.desc())).scalars().first()
        assert fb.metrics["views"] == 5000


def test_import_url_fallback(env):
    sf, _, _, _, aid1, _, tmp = env
    with sf() as s:
        pub = s.execute(select(StudioPublication).where(StudioPublication.artifact_id == aid1)).scalars().first()
        url = pub.url
    p = _csv(tmp, f"platform,url,views\ndouyin,{url},700\n")
    r = import_feedback(sf, p)
    assert r["imported"] == 1


def test_import_ambiguous_explicit(env):
    sf, _, _, pid, _, _, tmp = env
    # 人为制造 content_id 多命中
    with sf() as s:
        s.add(StudioArtifact(project_id=pid, kind="mp4", file_path="dup.mp4",
                             content_id="proj0001-cand0001", meta={}))
        s.commit()
    p = _csv(tmp, "content_id,views\nproj0001-cand0001,100\n")
    r = import_feedback(sf, p)
    assert r["imported"] == 0
    assert len(r["ambiguous"]) == 1 and "多命中" in r["ambiguous"][0]["reason"]


def test_import_unmatched_explicit(env):
    sf, _, _, _, _, _, tmp = env
    p = _csv(tmp, "content_id,url,标题,views\nno-such-id,http://no/where,孤儿行,100\n")
    r = import_feedback(sf, p)
    assert r["imported"] == 0 and len(r["unmatched"]) == 1
    assert r["unmatched"][0]["title"] == "孤儿行"


# ---------- A/B 报告 ----------

def test_ab_report_groups_and_direction(env):
    sf, _, _, pid, _, _, _ = env
    r = build_ab_report(sf, pid, now=NOW)
    assert len(r["groups"]) == 2  # L1 文字 vs 无钩子，同平台
    g = {x["hook_strategy"]: x for x in r["groups"]}
    assert g["L1 文字"]["n"] == 2 and g["无钩子"]["n"] == 2
    assert "方向性" in r["conclusion"] and "显著性" not in r["conclusion"].replace("不做显著性宣称", "")
    assert "不构成因果结论" in r["disclaimer"]


def test_ab_report_gates(env):
    sf, _, _, pid, aid1, _, _ = env
    with sf() as s:
        # 低播放 + 新发布 各一条 → 排除
        pub_low = StudioPublication(artifact_id=aid1, platform="douyin",
                                    published_at=NOW - timedelta(hours=100), status="published")
        pub_new = StudioPublication(artifact_id=aid1, platform="douyin",
                                    published_at=NOW - timedelta(hours=10), status="published")
        s.add_all([pub_low, pub_new])
        s.flush()
        s.add(StudioFeedback(publication_id=pub_low.id, metrics={"views": 100}))
        s.add(StudioFeedback(publication_id=pub_new.id, metrics={"views": 9999}))
        s.commit()
    r = build_ab_report(sf, pid, now=NOW)
    reasons = " ".join(x["reason"] for x in r["excluded"])
    assert "< 500" in reasons and "< 72h" in reasons


def test_ab_report_insufficient_samples(env):
    sf, _, _, pid, _, aid2, _ = env
    with sf() as s:
        # 删掉无钩子组的一条 → n=1，结论必须明示样本不足
        pubs = s.execute(select(StudioPublication).where(StudioPublication.artifact_id == aid2)).scalars().all()
        s.delete(pubs[0])
        s.commit()
    r = build_ab_report(sf, pid, now=NOW)
    assert "样本不足" in r["conclusion"]


# ---------- API ----------

def test_api_feedback_flow(env):
    sf, settings, ctx, pid, _, _, tmp = env
    client = TestClient(create_app(settings, ctx, sf))
    p = _csv(tmp, "content_id,views\nproj0001-cand0002,600\n")
    r = client.post("/studio/feedback/import", json={"csv_path": str(p)})
    assert r.status_code == 200 and r.json()["imported"] == 1
    r = client.get(f"/studio/projects/{pid}/ab-report")
    assert r.status_code == 200 and "groups" in r.json()
    r = client.post("/studio/feedback/import", json={"csv_path": str(tmp / "no.csv")})
    assert r.status_code == 400