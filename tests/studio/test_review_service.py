"""审核工作流测试（P1-6a 验收：clips0 只读、人工决定落 clips1、溯源、边界校验）。"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.models import (
    Base,
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTranscript,
)
from studio.review_service import (
    ReviewError,
    adopt_candidate,
    get_transcript,
    list_candidates,
    reject_candidate,
    update_clips1,
)


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'rv.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri="x",
                           file_path="x.mp4", fingerprint="fp", duration_ms=60000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="p", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.flush()
        c0 = StudioCandidate(project_id=p.id, layer="clips0", start_ms=0, end_ms=20000,
                             title="AI 标题", quote="原文", rank_score=88.0,
                             scores={"hook": 9}, created_by="ai")
        s.add(c0)
        s.commit()
        ids = (p.id, c0.id, m.id)
    yield sf, ids
    engine.dispose()


def test_adopt_creates_clips1_with_parent(env):
    sf, (pid, c0id, _) = env
    c1id = adopt_candidate(sf, c0id, user="alice")
    rows = list_candidates(sf, pid, layer="clips1")
    assert len(rows) == 1
    c1 = rows[0]
    assert c1["id"] == c1id and c1["parent_id"] == c0id
    assert c1["adopted"] is True and c1["created_by"] == "user:alice"
    assert c1["start_ms"] == 0 and c1["end_ms"] == 20000
    # clips0 原样保留（审计）
    c0 = list_candidates(sf, pid, layer="clips0")[0]
    assert c0["created_by"] == "ai" and c0["adopted"] is None


def test_adopt_with_overrides(env):
    sf, (pid, c0id, _) = env
    adopt_candidate(sf, c0id, user="bob", start_ms=1000, end_ms=30000,
                    title="人工标题", caption="人工文案")
    rows = list_candidates(sf, pid, layer="clips1")
    assert rows[0]["start_ms"] == 1000 and rows[0]["end_ms"] == 30000
    assert rows[0]["title"] == "人工标题" and rows[0]["caption"] == "人工文案"


def test_adopt_invalid_bounds_rejected(env):
    sf, (_, c0id, _) = env
    with pytest.raises(ReviewError):
        adopt_candidate(sf, c0id, user="u", start_ms=5000, end_ms=5000)
    with pytest.raises(ReviewError, match="超素材时长"):
        adopt_candidate(sf, c0id, user="u", end_ms=999999)
    assert list_candidates(sf, env[1][0], layer="clips1") == []


def test_reject_requires_reason_and_records(env):
    sf, (pid, c0id, _) = env
    with pytest.raises(ReviewError, match="理由"):
        reject_candidate(sf, c0id, user="u", reason="  ")
    reject_candidate(sf, c0id, user="u", reason="货不对板")
    c1 = list_candidates(sf, pid, layer="clips1")[0]
    assert c1["adopted"] is False and c1["rejected_reason"] == "货不对板"
    assert c1["parent_id"] == c0id


def test_clips0_not_editable(env):
    sf, (_, c0id, _) = env
    with pytest.raises(ReviewError, match="clips1"):
        update_clips1(sf, c0id, user="u", title="改 AI 候选")


def test_update_clips1(env):
    sf, (pid, c0id, _) = env
    c1id = adopt_candidate(sf, c0id, user="u")
    out = update_clips1(sf, c1id, user="editor", start_ms=500, end_ms=25000,
                        title="精修标题", hook_plan={"l1_text": {"text": "悬念"}},
                        adopted=False)
    assert out["start_ms"] == 500 and out["end_ms"] == 25000
    assert out["title"] == "精修标题" and out["adopted"] is False
    assert out["hook_plan"]["l1_text"]["text"] == "悬念"
    assert out["created_by"] == "user:editor"
    with pytest.raises(ReviewError):
        update_clips1(sf, c1id, user="u", end_ms=999999)


def test_list_layer_filter_and_invalid(env):
    sf, (pid, c0id, _) = env
    adopt_candidate(sf, c0id, user="u")
    assert len(list_candidates(sf, pid)) == 2
    assert len(list_candidates(sf, pid, layer="clips0")) == 1
    with pytest.raises(ReviewError):
        list_candidates(sf, pid, layer="clips2")


def test_transcript_latest_or_none(env):
    sf, (pid, _, mid) = env
    assert get_transcript(sf, pid) is None
    with sf() as s:
        s.add(StudioTranscript(material_id=mid, cache_key="k1", engine="e", model="m",
                               words=[{"w": "你"}], sentences=[{"text": "你好"}],
                               word_level=True))
        s.commit()
    tr = get_transcript(sf, pid)
    assert tr["sentences"] == [{"text": "你好"}] and tr["word_level"] is True
    with pytest.raises(ReviewError):
        get_transcript(sf, "no-project")