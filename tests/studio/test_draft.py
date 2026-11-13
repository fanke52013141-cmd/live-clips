"""剪映草稿导出测试（P2-6：fake transport 零网络，验证请求构造与失败路径）。"""
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.export.capcut_mate import CapCutMateClient, DraftError
from studio.export.draft_service import export_draft
from studio.models import (
    Base,
    StudioArtifact,
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTranscript,
)

SENTENCES = [
    {"text": "价格三十九块九", "start_ms": 0, "end_ms": 6000, "speaker": None},
    {"text": "好今天下单", "start_ms": 6300, "end_ms": 12000, "speaker": None},
]


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(
            source_type="local_file", source_uri="x", file_path=str(tmp_path / "src.mp4"),
            fingerprint="fp", duration_ms=12000,
        )
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="草稿", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        s.add(StudioTranscript(
            material_id=m.id, cache_key="k", engine="test", model="test",
            words=[], sentences=SENTENCES, word_level=False,
        ))
        c0 = StudioCandidate(
            project_id=p.id, layer="clips0", start_ms=0, end_ms=12000,
            quote="价格", created_by="ai",
        )
        s.add(c0)
        s.flush()
        c1 = StudioCandidate(
            project_id=p.id, layer="clips1", parent_id=c0.id,
            start_ms=0, end_ms=12000, adopted=True, created_by="user:t",
        )
        s.add(c1)
        s.flush()
        s.add(StudioArtifact(
            project_id=p.id, candidate_id=c1.id, kind="mp4",
            file_path=str(tmp_path / "out.mp4"), size_bytes=1000,
            meta={"start_ms": 0, "end_ms": 12000, "selfcheck": {"duration_s": 12.0}},
        ))
        s.commit()
        pid, cid = p.id, c1.id
    yield sf, pid, cid
    engine.dispose()


class FakeTransport:
    def __init__(self, fail=False, bad_response=False):
        self.calls = []
        self.fail = fail
        self.bad_response = bad_response

    def __call__(self, method, url, payload):
        self.calls.append((method, url, payload))
        if self.fail:
            raise DraftError("capcut-mate 服务不可达或响应非法: 连接拒绝")
        if self.bad_response:
            return {"code": 0}  # 缺 draft_id/draft_url
        if url.endswith("create_draft"):
            return {"draft_id": "draft-1", "draft_url": "http://127.0.0.1:30000/get_draft?draft_id=draft-1"}
        return {"code": 0, "msg": "ok"}


def test_draft_full_flow(env):
    sf, pid, cid = env
    ft = FakeTransport()
    client = CapCutMateClient("http://127.0.0.1:30000", transport=ft)
    receipt = export_draft(sf, pid, client=client, video_urls={cid: "http://127.0.0.1:8000/out.mp4"})

    paths = [c[1].rsplit("/", 1)[-1] for c in ft.calls]
    assert paths == ["create_draft", "add_videos", "add_captions", "save_draft"]
    # 画布竖屏
    assert ft.calls[0][2] == {"width": 1080, "height": 1920}
    # video_infos 为 JSON 字符串，微秒单位
    vi = json.loads(ft.calls[1][2]["video_infos"])
    assert vi == [{"video_url": "http://127.0.0.1:8000/out.mp4", "start": 0, "end": 12_000_000}]
    # 字幕：两句，微秒
    ci = json.loads(ft.calls[2][2]["caption_infos"])
    assert [c["text"] for c in ci] == ["价格三十九块九", "好今天下单"]
    assert ci[1]["start"] == 6_300_000
    assert receipt["drafts"][0]["draft_id"] == "draft-1"
    assert receipt["drafts"][0]["url_note"] is None
    assert receipt["license"] == "Apache-2.0"
    with sf() as s:
        art = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "draft")).scalars().one()
        assert art.meta["drafts"][0]["candidate_id"] == cid


def test_draft_caption_remap_with_segments(env):
    """有剪辑段（cold_open 复制）→ 字幕按段映射重排。"""
    sf, pid, cid = env
    with sf() as s:
        art = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        art.meta = {
            "start_ms": 0, "end_ms": 12000, "selfcheck": {"duration_s": 18.0},
            "editlist": {"segments": [
                {"start_ms": 0, "end_ms": 6000, "kind": "cold_open"},
                {"start_ms": 0, "end_ms": 12000, "kind": "body"},
            ]},
        }
        s.commit()
    ft = FakeTransport()
    client = CapCutMateClient("http://127.0.0.1:30000", transport=ft)
    export_draft(sf, pid, client=client)
    ci = json.loads(ft.calls[2][2]["caption_infos"])
    # 句A 命中 cold_open 段 → 0；句B 在 body 段 → 6000+6300=12300ms
    assert ci[0]["start"] == 0
    assert ci[1]["start"] == 12_300_000


def test_draft_local_path_fallback_note(env):
    sf, pid, _ = env
    ft = FakeTransport()
    receipt = export_draft(sf, pid, client=CapCutMateClient("http://x", transport=ft))
    assert "未核实" in receipt["drafts"][0]["url_note"]


def test_draft_no_adopted_raises(env):
    sf, pid, cid = env
    with sf() as s:
        s.get(StudioCandidate, cid).adopted = False
        s.commit()
    with pytest.raises(DraftError, match="采纳"):
        export_draft(sf, pid, client=CapCutMateClient("http://x", transport=FakeTransport()))


def test_draft_no_mp4_raises(env):
    sf, pid, _ = env
    with sf() as s:
        art = s.execute(select(StudioArtifact).where(StudioArtifact.kind == "mp4")).scalars().one()
        s.delete(art)
        s.commit()
    with pytest.raises(DraftError, match="成片"):
        export_draft(sf, pid, client=CapCutMateClient("http://x", transport=FakeTransport()))


def test_draft_service_unreachable(env):
    sf, pid, _ = env
    with pytest.raises(DraftError, match="不可达"):
        export_draft(sf, pid, client=CapCutMateClient("http://x", transport=FakeTransport(fail=True)))


def test_draft_bad_response_raises(env):
    sf, pid, _ = env
    with pytest.raises(DraftError, match="关键字段"):
        export_draft(sf, pid, client=CapCutMateClient("http://x", transport=FakeTransport(bad_response=True)))