# Lifecycle CLI

Install with `mise run setup`. `mise exec -- uv run --locked query` and
`mise exec -- uv run --locked python -m query` expose the same commands. `--help` needs no
configuration. All other commands require an explicit `QUERY_DATABASE_URL` from
the environment or `.env`. Apply migrations with `mise run db:migrate`; the CLI
does not create a database or migrate it automatically.

Successful commands print JSON to stdout. Errors go to stderr with nonzero status.
Mutations require `--revision` from the last `inspect` or returned investigation/
ingestion record. Stale revisions change nothing: inspect again and reconsider
before retrying. No command calls providers or requires model configuration.

## Supplied-document example

After configuring and migrating an explicit Query database:

```sh
mise run setup
mise run db:migrate
mise exec -- uv run --locked python examples/supplied/run.py > investigation.json
```

The example creates a synthetic brief, ingests exact supplied text, freezes a
snapshot, moves to reviewing, commits a simulated answer step, pauses, resumes,
and exports. Each command runs in a fresh process. The export records the ID for
later `query inspect ID` and `query export ID`. Rerunning creates a new investigation.
The answer is marked as a **fixture**, usage is simulated, and the run remains
in reviewing. No model review or sufficiency assessment has occurred.

## Commands

| Command | Input and result |
| --- | --- |
| `create --brief brief.json --limits limits.json` | Validated `Brief` and absolute `Limits`; returns an investigation in scope. Example JSON files show the formats. |
| `inspect ID` | Current checkpoint, briefs, pending/finished steps, usage and decision events. Read-only. |
| `ingest ID --revision N --file text.txt --operation-key KEY --document-key DOC --title TITLE --observed-at ISO` | Exact UTF-8 with original newlines; returns accepted version or recorded rejection and revision. Optional `--published-at`, `--origin-url`, `--max-bytes`. Timestamps need timezones. |
| `freeze ID --revision N --request snapshot.json` | `FreezeSnapshot` JSON; returns immutable manifest, versions, passages and provenance. |
| `advance ID --revision N --to PHASE` | One explicit lifecycle transition. Holds require `--reason`; invalid edges report current/requested states. |
| `advance ID --revision N --fixture step.json` | Plan and finish one explicitly simulated step with durable identity and simulated usage. |
| `resume ID --revision N [--limits limits.json]` | Resume paused, parked or limit-stopped work. Optional absolute allowances explicitly increase limits without resetting spend. Grant and resume are separate commits: inspect after errors. |
| `revise ID --revision N --brief brief.json` | Append a brief version, including clarification for awaiting-input investigations. |
| `export ID [--output new-file.json]` | Self-contained audit JSON. Output files must be new; default is stdout. |

Normal phases follow scope → planning → gathering → assessing → reviewing.
Supplied-document work may go directly from planning to reviewing. The CLI records
explicit caller choices; it does not choose the next phase automatically.

Freezing all passages from an accepted version uses:

```json
{
  "operation_key": "freeze-1",
  "candidates": [{"version_id": "UUID_FROM_INGEST"}]
}
```

Partial selection adds `passage_ids` and an `exclusion_reason`. Passage IDs are in
freeze results and exported source versions; `max_bytes` bounds selected text.
Empty/oversized inputs and limited snapshots are successful recording operations:
check `status` and `reason` before use. There is no silent truncation.

## Fixture identity and recovery

`examples/supplied/step.json` specifies `key`, `lane`, `reserved_tokens`, and a
`StepResult` with `status`, `used_tokens`, `output`, and an `error` for failed
results. Review steps require reviewing; research steps require an active research
phase. The store checks reservations and records simulated usage, including failed
steps and overruns. Fixture steps never synthesize completion checks.

The CLI saves a `kind=fixture` request with its expected result as input, then
commits `output.fixture=true` and the supplied payload under `output.data`. After
interruption between those commits, inspect and rerun the identical fixture/key at
the current revision. A matching committed step returns unchanged; changed input
or result under the same key fails. Other pending work must be reconciled before
starting a different operation or changing phase.

Held work requires explicit resume. Limit stops resume in planning; move back to
reviewing for pending review-lane fixtures. Complete/canceled work stays terminal.
This local simulation does not implement provider execution ownership (NOT-40).

For explicit lifecycle tests, `advance --to complete` accepts `--checks` with
`CompletionChecks` JSON and `--outcome answered|qualified|unresolved`. Both checks
must pass for the current brief from reviewing. These are caller-supplied
attestations at the existing store boundary, not assessed completion or validation
of the named answer. Actual model/review policy belongs to later tickets.

## Audit export

Format version 1 contains `investigation`, `evidence_inputs`, `source_versions`,
and `snapshots`. One shared investigation lock keeps all records consistent at
the same revision. Exports contain every acquired version (including unfrozen
versions), provenance observations, rejections, frozen snapshot observation keys,
and included/excluded passages. Rejected oversized text is not saved; its hash,
size, limit and rejection remain.

Current answer state and decisions live in caller-supplied step results,
completion attestations and lifecycle events, all exported verbatim. There is no
synthesized final answer or inferred sufficiency decision. Saved exact text and
passage offsets let old snapshots resolve after subsequent imports. Exports are
audit artifacts, not an import/restore format.
