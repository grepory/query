import socket

import pytest
from pydantic_ai import Agent, models
from pydantic_ai.models.test import TestModel
from pytest_socket import SocketBlockedError

from query.config import ModelSettings, SmokeAnswer
from query.models import build_model


def test_offline_agent_can_produce_structured_output():
    agent = Agent(TestModel(custom_output_args={"answer": "2024"}), output_type=SmokeAnswer)
    assert agent.run_sync("What year?").output.answer == "2024"


def test_local_endpoint_configuration_does_not_need_cloud_keys():
    model = build_model(
        ModelSettings(
            _env_file=None,
            model_backend="local",
            model_name="loaded-model",
            local_base_url="http://127.0.0.1:1234/v1",
        )
    )
    assert model.model_name == "loaded-model"
    assert str(model.client.base_url) == "http://127.0.0.1:1234/v1/"


def test_real_model_requests_are_blocked():
    assert models.ALLOW_MODEL_REQUESTS is False
    model = build_model(
        ModelSettings(
            _env_file=None,
            model_backend="openai",
            model_name="test-model",
            openai_api_key="not-a-real-key",
        )
    )
    with pytest.raises(RuntimeError, match="not allowed"):
        Agent(model).run_sync("Must not reach a provider")


def test_network_sockets_are_blocked():
    with pytest.warns(UserWarning, match="socket.socket"), pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("openai", "https://api.openai.com/v1/"), ("anthropic", "https://api.anthropic.com")],
)
def test_hosted_models_ignore_foreign_base_urls(monkeypatch, backend, expected):
    monkeypatch.setenv(f"{backend.upper()}_BASE_URL", "http://127.0.0.1:9999/unrelated-proxy")
    settings = ModelSettings(
        _env_file=None,
        model_backend=backend,
        model_name="test-model",
        **{f"{backend}_api_key": "not-a-real-key"},
    )
    model = build_model(settings)
    assert str(model.client.base_url).rstrip("/") == expected.rstrip("/")
