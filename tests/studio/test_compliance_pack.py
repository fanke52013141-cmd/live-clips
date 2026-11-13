"""合规四件套与发布包测试（P2-7/P2-8）。"""
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.compliance.report import (
    aigc_check,
    build_compliance_report,
    license_check,
    lint_project_texts,
    transform_score,
    write_evidence,
)
from studio.export.pack import PackError, build_pack
from studio.export.platforms import fit_text, get_platform
from studio.models import (
    Base,
    StudioArtifact,
    StudioCandidate,
    StudioLicense,
    StudioMaterial,
    StudioProject,
    StudioTranscript,
)

# ---------- 单元 ----------

def test_lint_subtitle_timecode():
    sens = [{"text": "这是全网最低价", "start_ms": 1000, "end_ms": 2000}]
    hits = lint_project_texts("最好用的锅", "正常文案", sens)
    fields = {h["field"] for h in hits}
    assert fields == {"title", "subtitle"}
    subs = [h for h in hits if h["field"] == "subtitle"]
    # 同句命中多词（最低价/全网最低）都报，且全部带时间码
    assert {h["word"] for h in subs} == {"最低价", "全网最低"}
    assert all(h["start_ms"] == 1000 for h in subs)


def test_lint_clean():
    assert lint_project_texts("今天价格实惠", "拍一发三", [{"text": "正常", "start_ms": 0, "end_ms": 1}]) == []


def test_transform_score_monotonic():
    bare = transform_score({"portrait": False, "subtitles": None})
    assert bare["score"] == 0 and "非平台" in bare["disclaimer"]
    rich = transform_score({
        "portrait": True, "subtitles": "ass_word_highlight",
        "editlist": {"cuts": [{"start_ms": 1, "end_ms": 2}], "cold_open": [0, 100], "flash_forward": [5, 6]},
        "hooks": {"l1_overlay": {"text": "x"}, "l2_audio": "y"},
    })
    assert rich["score"] > bare["score"]
    assert rich["score"] <= 100


def test_aigc_check():
    assert aigc_check({})["ok"] is True  # 无 AI 内容无需标识
    assert aigc_check({"hooks": {"l2_audio": "x"}})["ok"] is False  # 有 TTS 未标识
    # 元数据+角标双到位才 ok
    assert aigc_check({"hooks": {"l2_audio": "x", "aigc_labeled": True, "aigc_badge": True}})["ok"] is True
    assert aigc_check({"hooks": {"l2_audio": "x", "aigc_labeled": True}})["ok"] is False  # 缺角标
    # B-roll/生视频同样触发 AIGC 义务（P3 渲染集成）
    assert aigc_check({"hooks": {"broll_image": "b.png"}})["required"] is True
    assert aigc_check({"hooks": {"hook_video": "v.mp4"}})["required"] is True


def test_fit_text():
    assert fit_text("短", 10) == ("短", False)
    t, over = fit_text("一二三四五六七八九十", 5)
    assert over is True and len(t) == 5 and t.endswith("…")


def test_unknown_platform():
    with pytest.raises(ValueError, match="未知平台"):
        get_platform("nope")


# ---------- 集成：DB 夹具 ----------

@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'c.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    mp4 = tmp_path / "out.mp4"
    mp4.write_bytes(b"fake-mp4-content" * 100)
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"fake-jpg" * 100)
    with sf() as s:
        lic = StudioLicense(
            licensor="主播A", scope={"platforms": ["douyin", "kuaishou"]},
            expires_at=date.today() + timedelta(days=30),
        )
        s.add(lic)
        s.flush()
        m = StudioMaterial(
            source_type="local_file", source_uri="x", file_path=str(mp4),
            fingerprint="fp-c", duration_ms=12000, license_id=lic.id,
        )
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="合规", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        s.add(StudioTranscript(
            material_id=m.id, cache_key="kc", engine="t", model="t",
            words=[], sentences=[{"text": "全网最低价来了", "start_ms": 0, "end_ms": 3000}],
            word_level=False,
        ))
        c0 = StudioCandidate(project_id=p.id, layer="clips0", start_ms=0, end_ms=12000,
                             quote="q", created_by="ai")
        s.add(c0)
        s.flush()
        c1 = StudioCandidate(
            project_id=p.id, layer="clips1", parent_id=c0.id, start_ms=0, end_ms=12000,
            title="史上最低价的锅", caption="长文案" * 400, adopted=True, created_by="user:t",
        )
        s.add(c1)
        s.flush()
        s.add(StudioArtifact(
            project_id=p.id, candidate_id=c1.id, kind="mp4", file_path=str(mp4),
            size_bytes=mp4.stat().st_size,
            meta={"portrait": True, "subtitles": "ass_word_highlight",
                  "hooks": {"l2_audio": "tts.wav", "aigc_labeled": True, "aigc_badge": True}},
        ))
        s.add(StudioArtifact(
            project_id=p.id, candidate_id=c1.id, kind="cover", file_path=str(cover),
            size_bytes=cover.stat().st_size, meta={},
        ))
        s.commit()
        pid, cid, mid, lid = p.id, c1.id, m.id, lic.id
    yield sf, pid, cid, mid, lid, tmp_path
    engine.dispose()


def test_compliance_report(env):
    sf, pid, _, _, _, _ = env
    r = build_compliance_report(sf, pid, platform="douyin")
    assert r["license"]["status"] == "ok"
    words = {h["word"] for h in r["banned_words"]["hits"]}
    assert "史上最低" in words and "全网最低" in words
    assert r["artifacts"][0]["aigc"]["ok"] is True
    assert r["artifacts"][0]["transform"]["score"] > 0
    assert r["blockers"] == []
    assert any(x["layer"] == "clips0" for x in r["lineage"])


def test_license_expired_blocks(env):
    sf, pid, _, mid, lid, _ = env
    with sf() as s:
        s.get(StudioLicense, lid).expires_at = date.today() - timedelta(days=1)
        s.commit()
        m = s.get(StudioMaterial, mid)
    assert license_check(sf, m)["status"] == "expired"
    r = build_compliance_report(sf, pid)
    assert "授权过期" in r["blockers"]


def test_license_scope_mismatch(env):
    sf, _, _, mid, _, _ = env
    with sf() as s:
        m = s.get(StudioMaterial, mid)
    assert license_check(sf, m, platform="bilibili")["status"] == "scope_mismatch"


def test_evidence_package(env):
    sf, pid, _, _, _, tmp = env
    r = write_evidence(sf, pid, tmp / "evidence")
    ev = Path(r["evidence_path"])
    assert ev.is_file()
    data = json.loads(ev.read_text(encoding="utf-8"))
    assert data["material_fingerprint"] == "fp-c"
    assert data["artifact_hashes"][0]["sha256"]  # 真实文件指纹
    assert len(data["artifact_hashes"][0]["sha256"]) == 64
    with sf() as s:
        assert s.execute(select(StudioArtifact).where(StudioArtifact.kind == "evidence")).scalars().one()


def test_pack_douyin_and_bilibili_warning(env):
    sf, pid, cid, _, _, tmp = env
    r = build_pack(sf, pid, ["douyin", "bilibili"], tmp / "pack")
    assert Path(r["zip_path"]).is_file() and r["zip_bytes"] > 0
    # 竖屏成片 → 抖音匹配，B站画幅警告（不静默拉伸）
    assert any("bilibili" in w and "不匹配" in w for w in r["warnings"])
    assert not any(w.startswith("douyin/") and "不匹配" in w for w in r["warnings"])
    # 文案超限截断+原文保留
    assert any("截断" in w for w in r["warnings"])
    sub = tmp / "pack" / "douyin" / cid
    assert (sub / "video.mp4").is_file() and (sub / "cover.jpg").is_file()
    assert (sub / "caption_full.txt").is_file()
    meta = json.loads((sub / "meta.json").read_text(encoding="utf-8"))
    assert meta["caption_overflow"] is True and meta["aspect_ok"] is True
    sa = json.loads((tmp / "pack" / "douyin" / "safe_area.json").read_text(encoding="utf-8"))
    assert "经验值" in sa["note"]
    with zipfile.ZipFile(r["zip_path"]) as zf:
        names = zf.namelist()
        assert any("safe_area.json" in n for n in names)
    with sf() as s:
        assert s.execute(select(StudioArtifact).where(StudioArtifact.kind == "publish_pack")).scalars().one()


def test_pack_unknown_platform(env):
    sf, pid, _, _, _, tmp = env
    with pytest.raises(ValueError, match="未知平台"):
        build_pack(sf, pid, ["nope"], tmp / "pack")


def test_pack_no_adopted(env):
    sf, pid, cid, _, _, tmp = env
    with sf() as s:
        s.get(StudioCandidate, cid).adopted = False
        s.commit()
    with pytest.raises(PackError, match="采纳"):
        build_pack(sf, pid, ["douyin"], tmp / "pack")