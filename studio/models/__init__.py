"""Studio 数据模型（04 号方案 §4，SQLite 落地版）。

与上游共存规则：所有表加 studio_ 前缀，避免与上游 project/clip/task 冲突；
UUID→TEXT(36)、JSONB→JSON、TIMESTAMPTZ→DateTime。语义与 04 号 DDL 一致。
"""
from studio.models.base import Base
from studio.models.entities import (
    StudioArtifact,
    StudioCandidate,
    StudioFeedback,
    StudioLicense,
    StudioMaterial,
    StudioProject,
    StudioPrompt,
    StudioPublication,
    StudioTask,
    StudioTranscript,
)

__all__ = [
    "Base",
    "StudioMaterial",
    "StudioLicense",
    "StudioTranscript",
    "StudioProject",
    "StudioCandidate",
    "StudioTask",
    "StudioArtifact",
    "StudioPublication",
    "StudioFeedback",
    "StudioPrompt",
]