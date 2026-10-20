# Local eval candidates

Assuming an M4 Max with **128 GB** unified memory, start with a quantized
[Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B) and compare
[Qwen3.6-27B](https://huggingface.co/Qwen/Qwen3.6-27B). The former is a sparse model;
the latter is dense. These are candidates, not measured recommendations for Query.

At four bits per weight, 35B and 27B parameters imply roughly 17.5 GB and 13.5 GB
of raw weights respectively. This is a sizing estimate, not total runtime memory:
quantization metadata, caches, context length, the server, and macOS need additional
headroom. Start with a modest context and one request at a time. No model weights
or inference runtime are installed by this repository.

[LM Studio](https://lmstudio.ai/docs/developer/core/server) exposes a local API.
Choose a runtime-supported quantized artifact and verify structured outputs and
tool calls using that exact artifact. Query uses Pydantic AI's
[OpenAI-compatible endpoint support](https://pydantic.dev/docs/ai/models/compatible-apis/).

Use local models to iterate on prompts and schema compatibility; run the eventual
research-quality evals against the selected Anthropic and OpenAI models too. Record
model artifact/quantization, context, server version, latency, usage, and task scores.
Neither local nor hosted inference belongs in pytest or CI. Shortlist checked
2026-10-20; no on-device throughput or quality measurements have been made.
