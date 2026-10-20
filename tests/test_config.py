import pytest
from pydantic import ValidationError

from query.config import ModelSettings, Settings


def test_database_must_be_explicit():
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_settings_use_query_namespace(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/aibyai")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("QUERY_DATABASE_URL", "postgresql://localhost/query_test")
    assert Settings(_env_file=None).database_url.path == "/query_test"


def test_keys_are_redacted():
    settings = Settings(
        _env_file=None, database_url="postgresql://localhost/query_test", exa_api_key="secret-value"
    )
    assert "secret-value" not in repr(settings)


@pytest.mark.parametrize("backend", ["anthropic", "openai"])
def test_hosted_models_require_explicit_keys(backend):
    with pytest.raises(ValidationError, match="API_KEY is required"):
        ModelSettings(_env_file=None, model_backend=backend, model_name="test-model")


def test_local_model_requires_endpoint():
    with pytest.raises(ValidationError, match="LOCAL_BASE_URL"):
        ModelSettings(_env_file=None, model_backend="local", model_name="test-model")
