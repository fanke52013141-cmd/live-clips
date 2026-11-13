"""实体定义。核心约束：
- clip_candidate.layer 只允许 clips0/clips1（CheckConstraint）
- clips0 只由 AI 写（created_by='ai'），人工修改产生 clips1（parent_id 溯源）
- transcript.cache_key 唯一（fingerprint|engine|model|params_hash，跨机续跑键）
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from studio.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StudioLicense(Base):
    __tablename__ = "studio_license"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    licensor: Mapped[str] = mapped_column(String(255))
    scope: Mapped[dict] = mapped_column(JSON)  # 素材范围/账号/期限/可否投流/可否改编
    proof_path: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)


class StudioMaterial(Base):
    __tablename__ = "studio_material"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_type: Mapped[str] = mapped_column(String(32))  # local_file|url|m3u8|recorded
    source_uri: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    danmaku_path: Mapped[str | None] = mapped_column(Text)
    license_id: Mapped[str | None] = mapped_column(ForeignKey("studio_license.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StudioTranscript(Base):
    __tablename__ = "studio_transcript"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    material_id: Mapped[str] = mapped_column(ForeignKey("studio_material.id"), index=True)
    cache_key: Mapped[str] = mapped_column(String(255), unique=True)
    engine: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    words: Mapped[list] = mapped_column(JSON)
    sentences: Mapped[list] = mapped_column(JSON)
    hotwords: Mapped[list | None] = mapped_column(JSON)
    word_level: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StudioPrompt(Base):
    __tablename__ = "studio_prompt"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    live_type: Mapped[str | None] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class StudioProject(Base):
    __tablename__ = "studio_project"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    material_id: Mapped[str] = mapped_column(ForeignKey("studio_material.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    live_type: Mapped[str] = mapped_column(String(32))  # ecommerce|entertainment|knowledge|game|talent
    intent: Mapped[str | None] = mapped_column(Text)
    background: Mapped[str | None] = mapped_column(Text)
    prompt_id: Mapped[str | None] = mapped_column(ForeignKey("studio_prompt.id"))
    profile: Mapped[str] = mapped_column(String(32))  # cpu-cloud|gpu-local 快照
    status: Mapped[str] = mapped_column(String(32), default="ingested")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StudioCandidate(Base):
    __tablename__ = "studio_candidate"
    __table_args__ = (
        CheckConstraint("layer IN ('clips0','clips1')", name="ck_candidate_layer"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_project.id"), index=True)
    layer: Mapped[str] = mapped_column(String(8))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("studio_candidate.id"))
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    clip_type: Mapped[str | None] = mapped_column(String(32))
    scores: Mapped[dict | None] = mapped_column(JSON)
    rank_score: Mapped[float | None] = mapped_column(Float)
    quote: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    hook_plan: Mapped[dict | None] = mapped_column(JSON)
    adopted: Mapped[bool | None] = mapped_column(Boolean)  # 仅 clips1 有意义
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))  # ai | user:<id>
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StudioTask(Base):
    __tablename__ = "studio_task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("studio_project.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # transcribe|analyze|render|hook|draft|publish_pack
    stage: Mapped[str] = mapped_column(String(32))
    progress: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    receipt: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class StudioArtifact(Base):
    __tablename__ = "studio_artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("studio_project.id"))
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("studio_candidate.id"))
    kind: Mapped[str] = mapped_column(String(32))  # mp4|srt|cover|caption|draft|publish_pack|evidence
    platform: Mapped[str | None] = mapped_column(String(32))
    file_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict | None] = mapped_column(JSON)
    content_id: Mapped[str | None] = mapped_column(String(64), index=True)  # 回流对账键
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class StudioPublication(Base):
    __tablename__ = "studio_publication"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("studio_artifact.id"), index=True)
    platform: Mapped[str | None] = mapped_column(String(32))
    account: Mapped[str | None] = mapped_column(String(128))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="planned")


class StudioFeedback(Base):
    __tablename__ = "studio_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    publication_id: Mapped[str] = mapped_column(ForeignKey("studio_publication.id"), index=True)
    metrics: Mapped[dict] = mapped_column(JSON)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    source_file: Mapped[str | None] = mapped_column(Text)