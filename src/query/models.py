"""Construct Pydantic AI models without making requests."""

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from query.config import ModelSettings


def build_model(settings: ModelSettings) -> Model:
    if settings.model_backend == "local":
        return OpenAIChatModel(
            settings.model_name,
            provider=OpenAIProvider(
                base_url=str(settings.local_base_url),
                api_key=settings.local_api_key.get_secret_value(),
            ),
        )
    if settings.model_backend == "anthropic":
        assert settings.anthropic_api_key is not None
        return AnthropicModel(
            settings.model_name,
            provider=AnthropicProvider(
                api_key=settings.anthropic_api_key.get_secret_value(),
                base_url="https://api.anthropic.com",
            ),
        )
    assert settings.openai_api_key is not None
    return OpenAIChatModel(
        settings.model_name,
        provider=OpenAIProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url="https://api.openai.com/v1",
        ),
    )
