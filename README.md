# Query

Research that can show its work. Query is a fresh application informed by aibyai;
it does not depend on aibyai at runtime. This bootstrap supplies development tools,
settings, a model boundary, durable investigation contracts, and supplied-text evidence snapshots. Automated research
and review workflows come next.

## Development

Install [mise](https://mise.jdx.dev), then in a separate jj workspace:

```sh
mise trust
mise install
mise run setup
mise run check
```

Python and uv versions are pinned in `mise.toml`; application and dev dependencies
are locked in `uv.lock`. Use `mise run format` for formatting and `mise exec -- uv add`
for dependency changes. Dependency installation needs network access; tests do not.
CI runs the same lint and pytest tasks with no provider credentials, plus an isolated
PostgreSQL 18 service for persistence tests.

Copy `.env.example` to `.env` only when configuring application/eval execution.
`QUERY_DATABASE_URL` is required by application settings and has no implicit fallback.
Unit tests need neither a database nor a `.env`. Provider keys are optional until used.

## Supplied-text evidence API

After applying migrations to your explicit Query database, use the store API:

```python
from datetime import UTC, datetime

from query.db import database_engine
from query.evidence import CandidateSelection, FreezeSnapshot, IngestText
from query.store import InvestigationStore

store = InvestigationStore(database_engine(database_url))  # Explicit Query database URL.
run = store.load(investigation_id)  # An investigation created with a Brief and Limits.
observation = store.ingest_text(
    run.id,
    run.revision,
    IngestText(
        operation_key="supply-document-1",
        document_key="document-1",
        title="Supplied capacity announcement",
        observed_at=datetime.now(UTC),
        text="Exact supplied source text, including whitespace.\n",
    ),
)
if observation.status == "accepted":
    run = store.load(run.id)
    evidence = store.freeze_snapshot(
        run.id,
        run.revision,
        FreezeSnapshot(
            operation_key="freeze-1",
            candidates=(CandidateSelection(version_id=observation.version_id),),
        ),
    )
    # Only a ready snapshot has usable included passages.
    print(evidence.snapshot.status, evidence.snapshot.id)
else:
    print(observation.reason, observation.byte_count, observation.max_bytes)
```

Version IDs name immutable exact text. `load_source_version` resolves passages;
`load_snapshot` reconstructs a manifest, saved versions, and frozen provenance.
Oversized inputs and selections are recorded explicitly. See
[persistence](docs/persistence.md#supplied-evidence) for limits, exclusions, retry
identity, and lifecycle constraints. The [lifecycle CLI](docs/cli.md) exposes
ingestion, snapshot freezing, explicit advance/resume, deterministic fixture
steps, inspection, and self-contained exports.

## Dependency updates

The Renovate workflow runs every six hours and can also be started from Actions →
Renovate → Run workflow after it is merged to `main`. It uses
`renovatebot/github-action@v46.3.9` and only processes `grepory/query`.
`renovate.json` enables recommended update PRs and weekly `uv.lock` maintenance;
Renovate opens at most five PRs at a time, with no hourly creation limit so merged
updates immediately free slots for the next run. Updates require review and are
not automatically merged. Python tool upgrades are
manual because `mise.toml`'s Python pin and `UV_PYTHON`, the project Python constraint,
and Ruff's target version must stay aligned. Other mise tools and GitHub Actions
are managed by Renovate.

Before the first run, add a dedicated `RENOVATE_TOKEN` repository Actions secret in
[GitHub settings](https://github.com/grepory/query/settings/secrets/actions).
For this public repository, a classic personal access token needs `public_repo`
and `workflow` scopes. The workflow scope lets Renovate update Actions files.
Use a dedicated token rather than the workflow's `GITHUB_TOKEN` so dependency PRs
can trigger CI normally. Rotate the secret before its token expires.
See the [Renovate action documentation](https://github.com/renovatebot/github-action#token).

After merging, run the workflow manually and check its log and Dependency Dashboard
issue. Missing credentials produce an explicit setup error. No checkout is needed:
Renovate fetches the repository and its configuration using its own token.

## Tests and evals

`mise run test` uses Pydantic AI test models and fake clients. Real model requests
are disabled before collection, and pytest blocks network sockets (Unix sockets
remain available for the async event loop). This includes local model servers. PostgreSQL integration tests use psycopg/libpq
against an explicitly configured loopback test database; they make no provider calls.

## Persistence checks

The PostgreSQL database name in `.env.example` is `query`. Create it yourself, then
run `mise run db:migrate` to apply migrations using `QUERY_DATABASE_URL`. This task
writes only to the explicitly configured database; never use aibyai production.

For local integration tests, create a separate database named `query_test`:

```sh
createdb query_test
QUERY_TEST_DATABASE_URL=postgresql://localhost/query_test mise run check
```

Each test migrates a fresh randomly named schema and drops only that schema afterward.
The test harness rejects non-loopback hosts, other database names, and URL query
parameters. Integration tests skip locally when the variable is absent; CI requires it.
Unit tests and all model calls remain offline. See [persistence](docs/persistence.md)
for lifecycle, versioning, and recovery guarantees.

For an explicit connectivity and structured-output eval, select a backend and model
in `.env`, then run `mise run eval:smoke`. This makes one logical model request;
provider SDK transport retries may repeat HTTP attempts. It refuses CI execution.
No eval is included in `check`. This smoke check is not a quality benchmark.

Supported settings:

| Backend | Required settings |
| --- | --- |
| `anthropic` | `QUERY_MODEL_BACKEND`, `QUERY_MODEL_NAME`, `QUERY_ANTHROPIC_API_KEY` |
| `openai` | `QUERY_MODEL_BACKEND`, `QUERY_MODEL_NAME`, `QUERY_OPENAI_API_KEY` |
| `local` | `QUERY_MODEL_BACKEND`, `QUERY_MODEL_NAME`, `QUERY_LOCAL_BASE_URL` |

Local endpoints use the OpenAI-compatible chat API, for example
`http://127.0.0.1:1234/v1` with LM Studio. Set the model name to the server's loaded
model identifier; `QUERY_LOCAL_API_KEY` defaults to `local`. Hosted backends use their
fixed official API URLs and ignore ambient SDK base-URL overrides. Structured-output and
tool compatibility depends on the chosen server/model and must be evaluated.

Use local evals for quick prompt and schema experiments. Use Anthropic and OpenAI
for the final research-quality benchmark: a cheaper model is not a substitute for
measuring the models that will perform the research and review. Keep quality evals
separate from deterministic correctness tests.

See [architecture decisions](docs/architecture.md) for scope and persistence choices
and [local model candidates](docs/local-models.md) for the initial shortlist.
The [review invariant inventory](docs/review-invariants.md) maps the pinned aibyai
reference to synthetic Query histories for the upcoming review controller.
The [stopping-decision suite and rubric](docs/stopping-decisions.md) supplies the
nine stopping histories, decision-changing boundaries, and offline human scoring.
