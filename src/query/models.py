"""Construct Pydantic AI models without making requests."""

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
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
                openai_client=AsyncOpenAI(
                    base_url=str(settings.local_base_url),
                    api_key=settings.local_api_key.get_secret_value(),
                    max_retries=0,
                ),
            ),
        )
    if settings.model_backend == "anthropic":
        assert settings.anthropic_api_key is not None
        return AnthropicModel(
            settings.model_name,
            provider=AnthropicProvider(
                anthropic_client=AsyncAnthropic(
                    api_key=settings.anthropic_api_key.get_secret_value(),
                    base_url="https://api.anthropic.com",
                    max_retries=0,
                ),
            ),
        )
    assert settings.openai_api_key is not None
    return OpenAIChatModel(
        settings.model_name,
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(
                api_key=settings.openai_api_key.get_secret_value(),
                base_url="https://api.openai.com/v1",
                max_retries=0,
            ),
        ),
    )
