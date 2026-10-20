"""Versioned briefs, investigation checkpoints, steps, and lifecycle events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "investigations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checkpoint", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("revision >= 1", name="ck_investigation_revision"),
    )
    op.create_table(
        "brief_versions",
        sa.Column(
            "investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), primary_key=True
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("version >= 1", name="ck_brief_version"),
    )
    op.create_table(
        "steps",
        sa.Column("investigation_id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("brief_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id", "brief_version"],
            ["brief_versions.investigation_id", "brief_versions.version"],
        ),
    )
    op.create_table(
        "investigation_events",
        sa.Column(
            "investigation_id", sa.Uuid(), sa.ForeignKey("investigations.id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("revision >= 1", name="ck_event_revision"),
    )


def downgrade():
    for name in ("investigation_events", "steps", "brief_versions", "investigations"):
        op.drop_table(name)
