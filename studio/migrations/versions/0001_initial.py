"""studio 初始表结构（04 号方案 §4 的 SQLite 落地，表名 studio_ 前缀与上游共存）

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_license",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("licensor", sa.String(255), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("proof_path", sa.Text()),
        sa.Column("expires_at", sa.Date()),
        sa.Column("notes", sa.Text()),
    )
    op.create_table(
        "studio_material",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text()),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("fps", sa.Float()),
        sa.Column("danmaku_path", sa.Text()),
        sa.Column("license_id", sa.String(36), sa.ForeignKey("studio_license.id")),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_studio_material_fingerprint", "studio_material", ["fingerprint"])
    op.create_table(
        "studio_transcript",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("studio_material.id"), nullable=False),
        sa.Column("cache_key", sa.String(255), nullable=False, unique=True),
        sa.Column("engine", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("words", sa.JSON(), nullable=False),
        sa.Column("sentences", sa.JSON(), nullable=False),
        sa.Column("hotwords", sa.JSON()),
        sa.Column("word_level", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_studio_transcript_material_id", "studio_transcript", ["material_id"])
    op.create_table(
        "studio_prompt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("live_type", sa.String(32)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "studio_project",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("studio_material.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("live_type", sa.String(32), nullable=False),
        sa.Column("intent", sa.Text()),
        sa.Column("background", sa.Text()),
        sa.Column("prompt_id", sa.String(36), sa.ForeignKey("studio_prompt.id")),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_studio_project_material_id", "studio_project", ["material_id"])
    op.create_table(
        "studio_candidate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("studio_project.id"), nullable=False),
        sa.Column("layer", sa.String(8), nullable=False),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("studio_candidate.id")),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("clip_type", sa.String(32)),
        sa.Column("scores", sa.JSON()),
        sa.Column("rank_score", sa.Float()),
        sa.Column("quote", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("caption", sa.Text()),
        sa.Column("hook_plan", sa.JSON()),
        sa.Column("adopted", sa.Boolean()),
        sa.Column("rejected_reason", sa.Text()),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime()),
        sa.CheckConstraint("layer IN ('clips0','clips1')", name="ck_candidate_layer"),
    )
    op.create_index("ix_studio_candidate_project_id", "studio_candidate", ["project_id"])
    op.create_table(
        "studio_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("studio_project.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("progress", sa.JSON()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("receipt", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )
    op.create_index("ix_studio_task_project_id", "studio_task", ["project_id"])
    op.create_table(
        "studio_artifact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("studio_project.id")),
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("studio_candidate.id")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("platform", sa.String(32)),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("meta", sa.JSON()),
        sa.Column("content_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime()),
    )
    op.create_index("ix_studio_artifact_content_id", "studio_artifact", ["content_id"])
    op.create_table(
        "studio_publication",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("studio_artifact.id"), nullable=False),
        sa.Column("platform", sa.String(32)),
        sa.Column("account", sa.String(128)),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("url", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False),
    )
    op.create_index("ix_studio_publication_artifact_id", "studio_publication", ["artifact_id"])
    op.create_table(
        "studio_feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("studio_publication.id"), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime()),
        sa.Column("source_file", sa.Text()),
    )
    op.create_index("ix_studio_feedback_publication_id", "studio_feedback", ["publication_id"])


def downgrade() -> None:
    for table in (
        "studio_feedback",
        "studio_publication",
        "studio_artifact",
        "studio_task",
        "studio_candidate",
        "studio_project",
        "studio_prompt",
        "studio_transcript",
        "studio_material",
        "studio_license",
    ):
        op.drop_table(table)