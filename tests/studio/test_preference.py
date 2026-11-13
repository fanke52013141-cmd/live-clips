"""偏好回流测试（P3-6：采纳/否决→偏好摘要→选段 prompt 注入）。"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import EngineContext
from studio.engines.registry import EngineRegistry
from studio.feedback.preference import preference_prompt_block, preference_summary
from studio.models import Base, StudioCandidate, StudioMaterial, StudioProject, StudioTask
from studio.pipeline.orchestrator import Pipeline
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'pref.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri="x",
                           file_path=str(tmp_path / "m.mp4"), fingerprint="fp", duration_ms=60000)
        (tmp_path / "m.mp4").write_bytes(b"fake")
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="偏好", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.commit()
        pid = p.id
    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    yield sf, settings, ctx, pid
    engine.dispose()


def _decisions(sf, pid, spec):
    with sf() as s:
        for clip_type, adopted, reason in spec:
            s.add(StudioCandidate(
                project_id=pid, layer="clips1", start_ms=0, end_ms=16000,
                clip_type=clip_type, adopted=adopted, rejected_reason=reason,
                created_by="user:t",
            ))
        s.commit()


def test_summary_rates_and_keywords(env):
    sf, _, _, pid = env
    _decisions(sf, pid, [
        ("福利", True, None), ("福利", True, None), ("福利", True, None), ("福利", False, "价格不实"),
        ("干货", False, "太啰嗦"), ("干货", False, "太啰嗦"), ("干货", False, "节奏拖沓"),
    ])
    s = preference_summary(sf)
    assert s["total_decisions"] == 7
    by = {t["clip_type"]: t for t in s["by_type"]}
    assert by["福利"]["adopt_rate"] == 0.75 and by["福利"]["n"] == 4
    assert by["干货"]["adopt_rate"] == 0.0
    kws = {k["word"] for k in s["top_reject_keywords"]}
    assert "太啰嗦" in kws


def test_prompt_block_gates(env):
    sf, _, _, pid = env
    # 无决策 → 空串（不注入噪声）
    assert preference_prompt_block(preference_summary(sf)) == ""
    # 小样本（n<3）不给类型偏置
    _decisions(sf, pid, [("福利", True, None), ("福利", True, None)])
    assert preference_prompt_block(preference_summary(sf)) == ""
    # 足量 → 优先/谨慎类型
    _decisions(sf, pid, [
        ("福利", True, None), ("干货", False, "拖沓"), ("干货", False, "拖沓"), ("干货", False, "拖沓"),
    ])
    block = preference_prompt_block(preference_summary(sf))
    assert "优先类型：福利" in block and "谨慎类型：干货" in block


def test_pipeline_injects_preference(env):
    sf, settings, ctx, pid = env
    pipe = Pipeline(settings, ctx, sf, registry=EngineRegistry(settings, ctx))
    # 无历史决策 → preference_applied False
    r1 = pipe.run(pid)
    assert r1.status == "candidates_ready"
    with sf() as s:
        t = s.execute(select(StudioTask).order_by(StudioTask.created_at.desc())).scalars().first()
        assert t.receipt["preference_applied"] is False
    # 造足量偏好 → 注入 True
    _decisions(sf, pid, [("福利", True, None)] * 4)
    r2 = pipe.run(pid)
    assert r2.status == "candidates_ready"
    with sf() as s:
        t = s.execute(select(StudioTask).order_by(StudioTask.created_at.desc())).scalars().first()
        assert t.receipt["preference_applied"] is True