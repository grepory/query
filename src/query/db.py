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


def database_engine(url: str, **kwargs) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not parsed.database:
        raise ValueError("An explicit PostgreSQL database is required")
    return create_engine(parsed.set(drivername="postgresql+psycopg"), **kwargs)
