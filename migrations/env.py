"""Run migrations against an explicit Query database or a supplied test connection."""

from alembic import context

from query.config import Settings
from query.db import database_engine, metadata


def run(connection):
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
elif context.is_offline_mode():
    context.configure(
        url=str(Settings().database_url),
        target_metadata=metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = database_engine(str(Settings().database_url))
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
