"""Each test gets a disposable schema in an explicitly selected local test database."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from query.db import database_engine


@pytest.fixture
def database():
    url = os.environ.get("QUERY_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI requires QUERY_TEST_DATABASE_URL for PostgreSQL tests")
        pytest.skip("Set QUERY_TEST_DATABASE_URL to run PostgreSQL integration tests")
    parsed = make_url(url)
    if (
        parsed.host not in {"localhost", "127.0.0.1", "::1"}
        or parsed.database != "query_test"
        or parsed.query
    ):
        pytest.fail("Integration tests require a loopback PostgreSQL database named query_test")
    schema = f"test_{uuid4().hex}"
    admin = database_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = database_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    try:
        with engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        yield engine, config
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
