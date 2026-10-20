"""Disable real model requests before test collection."""

import os

import pytest
from pydantic_ai import models

models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def isolated_query_environment(monkeypatch):
    for key in os.environ:
        if key.startswith("QUERY_") and key != "QUERY_TEST_DATABASE_URL":
            monkeypatch.delenv(key)
