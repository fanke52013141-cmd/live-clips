"""转写缓存测试（P0-4 验收：指纹确定性、缓存键语义、命中不重跑）。"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from studio.engines.base import Sentence, TranscriptResult, Word
from studio.engines.cache import (
    TranscriptCache,
    file_fingerprint,
    make_cache_key,
    params_hash,
)
from studio.models import Base


@pytest.fixture()
def cache(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'cache.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield TranscriptCache(sessionmaker(bind=engine))
    engine.dispose()


def _tr() -> TranscriptResult:
    return TranscriptResult(
        words=(Word("你", 0, 200), Word("好", 200, 400)),
        sentences=(Sentence("你好", 0, 400),),
        engine="mock_asr",
        model="mock",
        params_hash="abc",
        word_level=True,
    )


def test_fingerprint_deterministic_and_content_sensitive(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"x" * 1024)
    b = tmp_path / "b.bin"
    b.write_bytes(b"x" * 1024)
    c = tmp_path / "c.bin"
    c.write_bytes(b"y" * 1024)
    assert file_fingerprint(a) == file_fingerprint(b)
    assert file_fingerprint(a) != file_fingerprint(c)


def test_cache_key_semantics():
    fp = "fp"
    k1 = make_cache_key(fp, "whisper_cpu", "small", {"language": "zh"})
    k2 = make_cache_key(fp, "whisper_cpu", "small", {"language": "zh"})
    k3 = make_cache_key(fp, "whisper_cpu", "small", {"language": "en"})
    k4 = make_cache_key(fp, "paraformer_cpu", "small", {"language": "zh"})
    k5 = make_cache_key("other", "whisper_cpu", "small", {"language": "zh"})
    assert k1 == k2
    assert len({k1, k3, k4, k5}) == 4  # 参数/引擎/指纹任一变化 → 键变化
    assert params_hash({"a": 1}) == params_hash({"a": 1})


def test_cache_roundtrip(cache):
    tr = _tr()
    key = make_cache_key("fp", tr.engine, tr.model, {})
    assert cache.get(key) is None  # 未命中
    cache.put("material-1", key, tr)
    hit = cache.get(key)
    assert hit is not None
    assert hit.words == tr.words
    assert hit.sentences == tr.sentences
    assert hit.word_level is True
    assert hit.engine == "mock_asr"


def test_cache_duplicate_key_rejected(cache):
    tr = _tr()
    key = make_cache_key("fp", tr.engine, tr.model, {})
    cache.put("material-1", key, tr)
    with pytest.raises(IntegrityError):
        cache.put("material-1", key, tr)