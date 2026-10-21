"""Immutable supplied sources, observations, passages and evidence snapshots."""

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sources",
        Column("id", Uuid, primary_key=True),
        Column("document_key", Text, nullable=False, unique=True),
        Column("title", Text, nullable=False),
        Column("origin_url", Text),
    )
    op.create_table(
        "source_versions",
        Column("id", Uuid, primary_key=True),
        Column("source_id", Uuid, ForeignKey("sources.id"), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("text", Text, nullable=False),
        Column("byte_count", Integer, nullable=False),
        Column("codepoint_count", Integer, nullable=False),
        Column("partition_version", Integer, nullable=False),
        UniqueConstraint("source_id", "content_hash", name="uq_source_version_hash"),
        CheckConstraint("byte_count > 0 AND codepoint_count > 0", name="ck_source_size"),
        CheckConstraint("partition_version = 1", name="ck_partition_version"),
    )
    op.create_table(
        "passages",
        Column("id", Uuid, primary_key=True),
        Column("version_id", Uuid, ForeignKey("source_versions.id"), nullable=False),
        Column("start", Integer, nullable=False),
        Column("end", Integer, nullable=False),
        UniqueConstraint("version_id", "start", "end", name="uq_passage_range"),
        CheckConstraint('start >= 0 AND "end" > start', name="ck_passage_range"),
    )
    op.create_table(
        "evidence_inputs",
        Column("investigation_id", Uuid, primary_key=True),
        Column("operation_key", Text, primary_key=True),
        Column("brief_version", Integer, nullable=False),
        Column("fingerprint", String(64), nullable=False),
        Column("request", JSONB, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("version_id", Uuid, ForeignKey("source_versions.id")),
        Column("status", String, nullable=False),
        ForeignKeyConstraint(
            ["investigation_id", "brief_version"],
            ["brief_versions.investigation_id", "brief_versions.version"],
        ),
        CheckConstraint(
            "(status = 'accepted' AND version_id IS NOT NULL) OR "
            "(status = 'rejected' AND version_id IS NULL)",
            name="ck_input_status",
        ),
    )
    op.create_table(
        "evidence_snapshots",
        Column("id", Uuid, primary_key=True),
        Column("investigation_id", Uuid, nullable=False),
        Column("operation_key", Text, nullable=False),
        Column("brief_version", Integer, nullable=False),
        Column("format_version", Integer, nullable=False),
        Column("status", String, nullable=False),
        Column("fingerprint", String(64), nullable=False),
        Column("request", JSONB, nullable=False),
        Column("manifest", JSONB, nullable=False),
        UniqueConstraint("investigation_id", "operation_key", name="uq_snapshot_operation"),
        ForeignKeyConstraint(
            ["investigation_id", "brief_version"],
            ["brief_versions.investigation_id", "brief_versions.version"],
        ),
        CheckConstraint("format_version = 1", name="ck_snapshot_format"),
        CheckConstraint("status IN ('ready', 'limited')", name="ck_snapshot_status"),
    )
    op.create_table(
        "snapshot_versions",
        Column("snapshot_id", Uuid, ForeignKey("evidence_snapshots.id"), primary_key=True),
        Column("version_id", Uuid, ForeignKey("source_versions.id"), primary_key=True),
    )


def downgrade():
    for name in (
        "snapshot_versions",
        "evidence_snapshots",
        "evidence_inputs",
        "passages",
        "source_versions",
        "sources",
    ):
        op.drop_table(name)
