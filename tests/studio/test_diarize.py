"""CAM++ 说话人分离测试（P1-7 验收：探测/注入映射/格式防御/管线开关）。

真实模型验收待下载批准（用户仅批准了 SeACo-Paraformer），此处为注入测试。
"""
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from studio.engines.base import EngineContext, Sentence, TranscriptResult, Word
from studio.engines.diarize_campp import CamppDiarizer
from studio.models import Base, StudioMaterial, StudioProject, StudioTranscript
from studio.pipeline.orchestrator import Pipeline
from studio.settings import load_settings

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
CTX = EngineContext(profile="cpu-cloud", data_dir=Path("."), models_dir=Path("."), gpu_available=False)


def make_tr():
    words = (Word("你", 0, 500), Word("好", 500, 1000), Word("价", 6000, 6500), Word("格", 6500, 7000))
    sentences = (Sentence("你好", 0, 1000), Sentence("价格", 6000, 7000))
    return TranscriptResult(words=words, sentences=sentences, engine="t", model="t",
                            params_hash="t", word_level=True)


class FakeModel:
    def __init__(self, segments):
        self.segments = segments

    def generate(self, input):
        return [self.segments]


def test_available_model_not_downloaded():
    ok, reason = CamppDiarizer().available(CTX)
    assert not ok and "模型未下载" in reason


def test_available_injected():
    ok, _ = CamppDiarizer(model_factory=lambda: FakeModel([])).available(CTX)
    assert ok


def test_assign_speakers_seconds_format():
    segs = [{"start": 0.0, "end": 1.0, "spk": 0}, {"start": 6.0, "end": 7.0, "spk": 1}]
    d = CamppDiarizer(model_factory=lambda: FakeModel(segs))
    out = d.assign_speakers(make_tr(), "x.wav", lambda ev: None)
    assert [w.speaker for w in out.words] == ["0", "0", "1", "1"]
    assert [s.speaker for s in out.sentences] == ["0", "1"]
    # 时间戳不变
    assert [w.start_ms for w in out.words] == [0, 500, 6000, 6500]


def test_assign_speakers_ms_format():
    segs = [{"start_ms": 0, "end_ms": 1000, "speaker": "A"},
            {"start_ms": 6000, "end_ms": 7000, "speaker": "B"}]
    d = CamppDiarizer(model_factory=lambda: FakeModel(segs))
    out = d.assign_speakers(make_tr(), "x.wav", lambda ev: None)
    assert [w.speaker for w in out.words] == ["A", "A", "B", "B"]


def test_bad_format_raises():
    d = CamppDiarizer(model_factory=lambda: FakeModel("not-a-list"))
    with pytest.raises(RuntimeError, match="格式不符"):
        d.assign_speakers(make_tr(), "x.wav", lambda ev: None)
    d2 = CamppDiarizer(model_factory=lambda: FakeModel([{"start": 0}]))
    with pytest.raises(RuntimeError, match="缺字段"):
        d2.assign_speakers(make_tr(), "x.wav", lambda ev: None)


def test_empty_segments_raises():
    d = CamppDiarizer(model_factory=lambda: FakeModel([]))
    with pytest.raises(RuntimeError, match="不伪造"):
        d.assign_speakers(make_tr(), "x.wav", lambda ev: None)


def test_pipeline_diarize_switch(tmp_path, monkeypatch):
    """开关开 → 转写带说话人落缓存；开关关（默认）→ 无说话人。"""
    from studio.engines.registry import EngineRegistry, default_factories

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'd.db').as_posix()}")
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

    settings = load_settings(PROFILES_DIR, "cpu-cloud", data_dir=tmp_path)
    ctx = EngineContext(profile="cpu-cloud", data_dir=tmp_path,
                        models_dir=tmp_path / "models", gpu_available=False)
    segs = [{"start": 0.0, "end": 30.0, "spk": 0}]
    factories = default_factories()
    factories["campp_cpu"] = lambda: CamppDiarizer(model_factory=lambda: FakeModel(segs))
    reg = EngineRegistry(settings, ctx, factories=factories)

    # 默认关：无说话人
    r0 = Pipeline(settings, ctx, sf, registry=reg).run(pid)
    assert r0.status == "candidates_ready"
    with sf() as s:
        tr0 = s.execute(select(StudioTranscript)).scalars().one()
        assert all(w.get("speaker") is None for w in tr0.words)

    # 开关开：说话人落缓存。缓存键按真实文件内容计算，
    # 故第二个素材必须用不同内容的文件，避免命中第一次的转写缓存。
    media2 = tmp_path / "m2.mp4"
    media2.write_bytes(b"fake-other-content")
    with sf() as s:
        m2 = StudioMaterial(source_type="local_file", source_uri=str(media2),
                            file_path=str(media2), fingerprint="fp2", duration_ms=60000)
        s.add(m2)
        s.flush()
        p2 = StudioProject(material_id=m2.id, name="p2", live_type="ecommerce", profile="cpu-cloud")
        s.add(p2)
        s.commit()
        pid2 = p2.id
    settings_on = replace(settings, diarize_enabled=True)
    reg2 = EngineRegistry(settings_on, ctx, factories=factories)
    r1 = Pipeline(settings_on, ctx, sf, registry=reg2).run(pid2)
    assert r1.status == "candidates_ready", r1.error
    with sf() as s:
        trs = s.execute(select(StudioTranscript)).scalars().all()
        tr1 = next(t for t in trs if t.cache_key != tr0.cache_key)
        assert all(w.get("speaker") == "0" for w in tr1.words)
    engine.dispose()