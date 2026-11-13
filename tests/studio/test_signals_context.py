"""信号上下文与共振测试（P1-5 验收：摘要/同桶共振/单信号不加/封顶/管线证据）。"""
from studio.pipeline.signals_context import apply_resonance, summarize_tracks


def track(series, bucket_ms=5000):
    return {"bucket_ms": bucket_ms, "series": series}


def cand(start=0, end=15900, rank=80.0):
    return {
        "start_ms": start, "end_ms": end, "title": "t",
        "rank_score": rank,
        "scores": {"hook": 8, "flow": 8, "value": 8, "trend": 8},
    }


def test_summarize_empty():
    assert "降维" in summarize_tracks({})


def test_summarize_peaks_with_time_range():
    tracks = {"danmaku": track({"danmaku": [0, 0, 9, 0, 0]})}
    text = summarize_tracks(tracks)
    assert "danmaku/danmaku" in text
    assert "10-15s:9.00" in text  # 桶2 = 10-15s


def test_summarize_all_zero_skipped():
    assert "降维" in summarize_tracks({"audio": track({"loudness": [0, 0, 0]})})


def test_resonance_two_signals_same_bucket():
    tracks = {
        "danmaku": track({"danmaku": [0, 0, 9, 0]}),
        "audio": track({"loudness": [0.1, 0.1, 0.9, 0.1]}),
    }
    c = cand(start=10000, end=15000)  # 桶2
    apply_resonance([c], tracks)
    assert c["scores"]["resonance_bonus"] == 2.0
    assert c["scores"]["signal_evidence"][0]["signals"] == ["danmaku", "loudness"]
    assert c["rank_score"] == 82.0


def test_single_signal_no_bonus():
    tracks = {"danmaku": track({"danmaku": [0, 0, 9, 0]})}
    c = cand(start=10000, end=15000)
    apply_resonance([c], tracks)
    assert c["scores"]["resonance_bonus"] == 0.0
    assert c["rank_score"] == 80.0  # 单路峰值不加（防噪声）


def test_resonance_cap_and_rank_ceiling():
    # 8 个桶全共振 → bonus 封顶 8；rank 封顶 99
    tracks = {
        "danmaku": track({"danmaku": [9] * 8}),
        "audio": track({"loudness": [0.9] * 8}),
    }
    c = cand(start=0, end=40000, rank=95.0)
    apply_resonance([c], tracks)
    assert c["scores"]["resonance_bonus"] == 8.0
    assert c["rank_score"] == 99.0


def test_no_tracks_zero_change():
    c = cand()
    apply_resonance([c], {})
    assert "resonance_bonus" not in c["scores"]
    assert c["rank_score"] == 80.0


def test_pipeline_writes_signal_evidence(tmp_path, monkeypatch):
    """管线集成：mock 信号（弹幕 i%5 + 恒定响度）→ 候选带共振证据。"""
    from pathlib import Path

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from studio.engines.base import EngineContext
    from studio.models import Base, StudioCandidate, StudioMaterial, StudioProject
    from studio.pipeline.orchestrator import Pipeline
    from studio.settings import load_settings

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    media = tmp_path / "m.mp4"
    media.write_bytes(b"fake")
    with sf() as s:
        m = StudioMaterial(source_type="local_file", source_uri=str(media),
                           file_path=str(media), fingerprint="fp", duration_ms=60000)
        s.add(m)
        s.flush()
        p = StudioProject(material_id=m.id, name="p", live_type="ecommerce", profile="cpu-cloud")
        s.add(p)
        s.commit()
        pid = p.id

    settings = load_settings(Path(__file__).resolve().parents[2] / "profiles",
                             "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    result = Pipeline(settings, ctx, sf).run(pid)
    assert result.status == "candidates_ready"
    with sf() as s:
        c = s.execute(select(StudioCandidate)).scalars().one()
        assert "resonance_bonus" in c.scores
        assert "signal_evidence" in c.scores
        # mock 弹幕桶3=3（高位）+ 响度恒定高位 → 桶3 共振，bonus=2
        assert c.scores["resonance_bonus"] == 2.0
    engine.dispose()