# Bootstrap decisions

Query owns its records, controllers, migrations, and CLI. Build concrete behavior
first; extract shared patterns when they emerge. No generic agent framework is
needed. aibyai is a reference for behavior and selective ports, not a dependency.

- **Python, mise, uv:** mise pins tools and defines tasks; uv locks and installs the
  package. Use a `src/query` layout to test installed imports.
- **Pydantic AI:** typed model calls with explicit provider settings; Anthropic and
  OpenAI for hosted evals, OpenAI-compatible endpoints for local evals. SDK extras
  are limited to the providers used. No network I/O occurs during configuration.
- **PostgreSQL:** retain a familiar transactional store for durable investigation
  state. NOT-33 introduces SQLAlchemy/Alembic and migrations; bootstrap settings
  require an explicit PostgreSQL URL but do not connect or create tables. Tests in
  this milestone need no running database. Future database integration tests use
  an isolated Query test database, never aibyai production.
- **Exa search and Firecrawl retrieval:** SDK dependencies and optional keys are
  present; adapters, requests, budgets, and acquisition live in NOT-44 through
  NOT-46. Exa API product versions are not Python SDK version constraints.
- **Testing:** pytest tests behavior and failure paths offline. TestModel or
  FunctionModel replaces inference; socket blocking catches accidental SDK I/O.
  Real inference, including local models, is opt-in under `evals/` and forbidden
  in CI. A provider smoke eval establishes plumbing only; NOT-49 adds quality evals.

Keep credentials out of source control. `CLAUDE.md` is a relative symlink to
`AGENTS.md` so both entrypoints share the same checked-in development instructions.
