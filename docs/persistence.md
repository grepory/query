# Investigation persistence

`InvestigationStore` uses SQLAlchemy Core and PostgreSQL. The store makes no model
calls. Briefs and checkpoints use validated Pydantic contracts; Alembic creates
four tables: investigations, brief_versions, steps, and investigation_events.

## Versioning and transitions

Creation saves brief version 1, a scope checkpoint, token allowances, and a creation
event atomically. A revised brief creates a new version including its cutoff and
required questions; prior versions and spent tokens remain. Revision returns active or awaiting-input work to
scope and invalidates prior completion eligibility. Paused, parked, and limit-stopped
work stays held with its reason intact; explicit resume returns the revised brief to scope. Questions have unique keys and
explicit acceptance criteria. No company-specific schema or exclusive claims exist.

Normal phases are scope → planning → gathering → assessing → reviewing. Planning
may go directly to reviewing for supplied documents; assessing and reviewing can
return to planning. Paused and parked work remembers the interrupted phase.
Awaiting-input work proceeds through a revised brief. Explicit resume reopens
limit-stopped work if any token allowance remains, returning to planning (or scope
after a brief revision). A grant can increase
allowances but never reduce them or clear usage. Complete and canceled are terminal.

Completion requires both answer integrity and research sufficiency for the current
brief, an answer reference, rationale, and answered/qualified/unresolved outcome.
These are caller-supplied attestations at this layer; actual answer records, evidence
snapshot references, and assessment policies arrive in later tickets. Budget or
deadline stops are distinct states and never produce a completed outcome.

## Atomicity and recovery

Every mutation locks the investigation row and requires the caller's expected
revision. A stale caller reloads before deciding again. Each successful mutation
increments the revision and records an event in the same transaction. Reads hold a
shared row lock while loading the checkpoint and history. Recovery loads the latest
committed checkpoint and pending step; it does not rerun earlier completed steps.

An operation key identifies one request and brief version. Planning saves inputs
and a token reservation before external work. Only one step can be pending per
investigation. Repeating a key with changed inputs is rejected. Repeating a committed
result is idempotent after reloading; changing that result is rejected. Brief changes
and normal phase transitions require reconciliation of the pending operation first.
Pause and cancellation can occur while a call is in flight. Its eventual result may
still be recorded for accurate usage; cancellation stays terminal.

Research and review token allowances are separate and bounded by a total ceiling.
Review calls are admitted only in reviewing; research calls use research phases.
Usage and the step result commit together, including failed requests and actual
usage above the reservation. Exhausting the active allowance records a budget stop.
Token counts are not prices. Search/scrape cost accounting, deadlines, provider
idempotency, and closing-review reserves belong to later controller work.

The step ledger deduplicates local records, not remote calls. It is not a worker
lease: concurrent executors must not both act on a pending operation. An executor
must reconcile uncertain outcomes after a crash; it must not assume a planned step
was never sent. Execution ownership and provider idempotency belong to NOT-40 and
acquisition work. No exactly-once billing claim is made.

## Schema changes and validation

Use `mise run db:migrate` with an explicit `QUERY_DATABASE_URL`. Migrations contain
fixed table definitions independent of runtime metadata. Future contract changes
that affect stored JSON require a migration or an explicit compatibility strategy.
Application writes go through the store; database keys and foreign keys enforce
identity and ownership, while Pydantic validates payloads. Direct SQL writes bypass
lifecycle validation and are not a supported application interface.

Tests apply the actual migration to disposable PostgreSQL schemas, compare metadata,
and exercise downgrade/upgrade, racing writers, stale decisions, budget overruns,
rollback, and recovery. SQLite is not substituted for PostgreSQL locking behavior.
