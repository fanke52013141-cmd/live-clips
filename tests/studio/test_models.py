"""数据模型与迁移测试（P0-3 验收：可升可降、双层候选约束、缓存键唯一）。"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from studio.models import (
    StudioCandidate,
    StudioMaterial,
    StudioProject,
    StudioTranscript,
)

STUDIO_DIR = Path(__file__).resolve().parents[2] / "studio"

EXPECTED_TABLES = {
    "studio_license",
    "studio_material",
    "studio_transcript",
    "studio_prompt",
    "studio_project",
    "studio_candidate",
    "studio_task",
    "studio_artifact",
    "studio_publication",
    "studio_feedback",
    "studio_alembic_version",
}


def make_config(db_path: Path) -> Config:
    cfg = Config(str(STUDIO_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(STUDIO_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "studio_test.db"
    cfg = make_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    yield engine, cfg
    engine.dispose()


def test_upgrade_creates_all_tables(db):
    engine, _ = db
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_downgrade_drops_all_tables(db):
    engine, cfg = db
    command.downgrade(cfg, "base")
    tables = set(inspect(engine).get_table_names())
    # Alembic 预期行为：downgrade 到 base 保留版本表本身（清空版本行），数据表必须全删
    data_tables = EXPECTED_TABLES - {"studio_alembic_version"}
    assert not (data_tables & tables)
    with engine.connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(text("SELECT COUNT(*) FROM studio_alembic_version")).scalar()
    assert rows == 0


def _seed_project(session: Session) -> StudioProject:
    m = StudioMaterial(
        source_type="local_file",
        source_uri="x.mp4",
        fingerprint="fp-test",
        duration_ms=60_000,
    )
    session.add(m)
    session.flush()
    p = StudioProject(
        material_id=m.id, name="测试项目", live_type="ecommerce", profile="cpu-cloud"
    )
    session.add(p)
    session.flush()
    return p


def test_candidate_layer_constraint(db):
    engine, _ = db
    with Session(engine) as s:
        p = _seed_project(s)
        s.add(
            StudioCandidate(
                project_id=p.id, layer="bogus", start_ms=0, end_ms=1000, created_by="ai"
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_clips0_to_clips1_adoption_roundtrip(db):
    engine, _ = db
    with Session(engine) as s:
        p = _seed_project(s)
        ai = StudioCandidate(
            project_id=p.id,
            layer="clips0",
            start_ms=0,
            end_ms=4000,
            quote="今天这个价格直接给你干到三十九块九。",
            scores={"hook": 9, "flow": 8, "value": 9, "trend": 7},
            rank_score=88.0,
            created_by="ai",
        )
        s.add(ai)
        s.flush()
        human = StudioCandidate(
            project_id=p.id,
            layer="clips1",
            parent_id=ai.id,
            start_ms=120,  # 人工微调切点
            end_ms=4000,
            title="人工改过的标题",
            adopted=True,
            created_by="user:tester",
        )
        s.add(human)
        s.commit()

        loaded = s.get(StudioCandidate, human.id)
        assert loaded.parent_id == ai.id
        assert loaded.adopted is True
        assert loaded.created_by == "user:tester"
        assert s.get(StudioCandidate, ai.id).layer == "clips0"  # AI 原始候选保留可审计


def test_transcript_cache_key_unique(db):
    engine, _ = db
    with Session(engine) as s:
        p = _seed_project(s)
        t1 = StudioTranscript(
            material_id=p.material_id,
            cache_key="fp|mock|mock|mock",
            engine="mock",
            model="mock",
            words=[],
            sentences=[],
            word_level=True,
        )
        s.add(t1)
        s.flush()
        s.add(
            StudioTranscript(
                material_id=p.material_id,
                cache_key="fp|mock|mock|mock",
                engine="mock",
                model="mock",
                words=[],
                sentences=[],
                word_level=True,
            )
        )
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()