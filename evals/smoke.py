"""Check model connectivity and structured output; not a research quality benchmark."""

import argparse
import os

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from query.config import ModelSettings, SmokeAnswer
from query.models import build_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-model-requests", action="store_true")
    args = parser.parse_args()
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        parser.error("Real-model evals are disabled in CI")
    if not args.allow_model_requests:
        parser.error("Pass --allow-model-requests to opt into a real model call")
    settings = ModelSettings()
    agent = Agent(build_model(settings), output_type=SmokeAnswer, retries=0)
    result = agent.run_sync(
        "Given only this source: 'The site opened in 2024.' What year did the site open?",
        model_settings={"max_tokens": 256, "timeout": 60},
        usage_limits=UsageLimits(request_limit=1),
    )
    print(result.output.model_dump_json())
    print(f"Model: {settings.model_backend}:{settings.model_name}; usage: {result.usage()}")
    if "2024" not in result.output.answer:
        raise SystemExit("Smoke eval failed: the answer did not contain the source year")


if __name__ == "__main__":
    main()
