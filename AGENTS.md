# Development instructions

- Use `jj` with colocated Git. Work in a separate jj workspace outside the default workspace; preserve unrelated changes. Use `codex/` bookmark names for new work.
- Keep the relevant Linear ticket updated as work progresses. Include its identifier and full URL in every PR description; use `Closes NOT-123` when the PR completes that ticket.
- Use mise for tool versions and tasks. Run `mise run check` before submitting changes; commit dependency updates with `uv.lock`.
- Keep architecture concrete. Avoid premature abstraction; extract shared behavior when repeated patterns emerge. Keep provider I/O separate from domain decisions.
- Write meaningful pytest tests consistently for behavior, failure paths, and regressions. Tests must be deterministic and offline. Use Pydantic AI test models and fake clients; real model calls belong only in explicit evals and must never run in CI.
- Keep code comments short, clear, and focused on intent or non-obvious constraints.
