"""转写缓存与指纹（04 号方案 §5：跨机续跑核心）。

缓存键 = sha256(fingerprint | engine | model | params_json)。
fingerprint = sha256(文件大小 + 前 16MB 内容)，不依赖 ffprobe，跨机一致。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from studio.engines.base import Sentence, TranscriptResult, Word
from studio.models import StudioTranscript

_HEAD_BYTES = 16 * 1024 * 1024


def file_fingerprint(path: str | Path) -> str:
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    with p.open("rb") as f:
        remaining = _HEAD_BYTES
        while remaining > 0:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def params_hash(params: dict) -> str:
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def make_cache_key(fingerprint: str, engine: str, model: str, params: dict) -> str:
    raw = f"{fingerprint}|{engine}|{model}|{json.dumps(params, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tr_to_rows(tr: TranscriptResult) -> tuple[list, list]:
    words = [
        {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms, "speaker": w.speaker}
        for w in tr.words
    ]
    sentences = [
        {"text": s.text, "start_ms": s.start_ms, "end_ms": s.end_ms, "speaker": s.speaker}
        for s in tr.sentences
    ]
    return words, sentences


def _rows_to_tr(row: StudioTranscript) -> TranscriptResult:
    return TranscriptResult(
        words=tuple(Word(**w) for w in row.words),
        sentences=tuple(Sentence(**s) for s in row.sentences),
        engine=row.engine,
        model=row.model,
        params_hash="",  # 原始 hash 不可逆，缓存命中后无需再用
        word_level=row.word_level,
    )


class TranscriptCache:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def get(self, cache_key: str) -> TranscriptResult | None:
        with self._sf() as s:
            row = s.execute(
                select(StudioTranscript).where(StudioTranscript.cache_key == cache_key)
            ).scalar_one_or_none()
            return _rows_to_tr(row) if row else None

    def put(self, material_id: str, cache_key: str, tr: TranscriptResult) -> None:
        words, sentences = _tr_to_rows(tr)
        with self._sf() as s:
            s.add(
                StudioTranscript(
                    material_id=material_id,
                    cache_key=cache_key,
                    engine=tr.engine,
                    model=tr.model,
                    words=words,
                    sentences=sentences,
                    word_level=tr.word_level,
                )
            )
            s.commit()