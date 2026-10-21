"""Query's PostgreSQL tables and explicit engine construction."""

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url

metadata = MetaData()
json_type = JSON().with_variant(JSONB(), "postgresql")
investigations = Table(
    "investigations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("checkpoint", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("revision >= 1", name="ck_investigation_revision"),
)
briefs = Table(
    "brief_versions",
    metadata,
    Column("investigation_id", Uuid, ForeignKey("investigations.id"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("payload", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("version >= 1", name="ck_brief_version"),
)
steps = Table(
    "steps",
    metadata,
    Column("investigation_id", Uuid, primary_key=True),
    Column("key", String, primary_key=True),
    Column("brief_version", Integer, nullable=False),
    Column("payload", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    ForeignKeyConstraint(
        ["investigation_id", "brief_version"],
        ["brief_versions.investigation_id", "brief_versions.version"],
    ),
)
events = Table(
    "investigation_events",
    metadata,
    Column("investigation_id", Uuid, ForeignKey("investigations.id"), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("kind", String, nullable=False),
    Column("details", json_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("revision >= 1", name="ck_event_revision"),
)


sources = Table(
    "sources",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("document_key", Text, nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("origin_url", Text),
)
source_versions = Table(
    "source_versions",
    metadata,
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
passages = Table(
    "passages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("version_id", Uuid, ForeignKey("source_versions.id"), nullable=False),
    Column("start", Integer, nullable=False),
    Column("end", Integer, nullable=False),
    UniqueConstraint("version_id", "start", "end", name="uq_passage_range"),
    CheckConstraint('start >= 0 AND "end" > start', name="ck_passage_range"),
)
evidence_inputs = Table(
    "evidence_inputs",
    metadata,
    Column("investigation_id", Uuid, primary_key=True),
    Column("operation_key", Text, primary_key=True),
    Column("brief_version", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("request", json_type, nullable=False),
    Column("payload", json_type, nullable=False),
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
evidence_snapshots = Table(
    "evidence_snapshots",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("investigation_id", Uuid, nullable=False),
    Column("operation_key", Text, nullable=False),
    Column("brief_version", Integer, nullable=False),
    Column("format_version", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("request", json_type, nullable=False),
    Column("manifest", json_type, nullable=False),
    UniqueConstraint("investigation_id", "operation_key", name="uq_snapshot_operation"),
    ForeignKeyConstraint(
        ["investigation_id", "brief_version"],
        ["brief_versions.investigation_id", "brief_versions.version"],
    ),
    CheckConstraint("format_version = 1", name="ck_snapshot_format"),
    CheckConstraint("status IN ('ready', 'limited')", name="ck_snapshot_status"),
)
snapshot_versions = Table(
    "snapshot_versions",
    metadata,
    Column("snapshot_id", Uuid, ForeignKey("evidence_snapshots.id"), primary_key=True),
    Column("version_id", Uuid, ForeignKey("source_versions.id"), primary_key=True),
)


def database_engine(url: str, **kwargs) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not parsed.database:
        raise ValueError("An explicit PostgreSQL database is required")
    return create_engine(parsed.set(drivername="postgresql+psycopg"), **kwargs)
