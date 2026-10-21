# Investigation persistence

`InvestigationStore` uses SQLAlchemy Core and PostgreSQL. The store makes no model
calls. Briefs and checkpoints use validated Pydantic contracts; Alembic creates
the lifecycle tables: investigations, brief_versions, steps, and investigation_events,
plus the supplied-evidence tables described below.

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

## Supplied evidence

`IngestText` accepts supplied Unicode text and explicit provenance. `ingest_text`
returns an `EvidenceInput` with the committed revision and accepted version ID or
rejection reason. Reload the investigation before the next mutation. The default
per-document limit is 1 MiB of UTF-8. Empty or oversized text commits a rejection
with its hash, counts, metadata, and limit; it never saves partial text. NUL,
unpaired surrogates, invalid metadata, and naive timestamps fail before mutation.

Document keys are exact, case-sensitive identities shared across investigations.
The same key must retain its title and origin URL. Within that identity, exact
UTF-8 hashes deduplicate versions, including A → B → A. Each import records its own
observation timestamps. Text preserves whitespace, CRLF and Unicode normalization.
Passages partition the entire accepted text in slices of at most 2,000 Unicode
code points, using end-exclusive offsets. `SourceVersion.passage_text` resolves a
passage only against its named version.

`FreezeSnapshot` names an explicit ordered corpus of candidate versions already
acquired by the investigation. A `CandidateSelection` with `passage_ids=None`
includes all its passages. Partial or empty selections require an exclusion reason.
The snapshot saves each selected or excluded passage and freezes its provenance
observation keys; subsequent imports cannot expand those references. Current-brief
rejected inputs are copied into the manifest. Earlier versions and observations
remain available after brief revisions. The candidate list defines corpus scope;
it does not assert that every historical document has been considered.

The default selected-text limit is 256 KiB of UTF-8, independently of token budgets.
Oversized selections commit a `limited` snapshot with no included passages and
explicit `snapshot_too_large` exclusions. Empty selections are also `limited`.
Explicitly select fewer passages or increase the limit under a new operation key.
A nonempty selection within the limit is `ready`; this does not attest to research
sufficiency, source truth, or answer integrity. `load_snapshot` returns the manifest,
exact source versions, and only the frozen accepted observations.

Evidence mutations use the existing investigation lock, expected revision, pending
step guard, and atomic revision/event transaction. New evidence is allowed in scope,
planning, gathering, or assessing. It cannot be added while reviewing, held, complete,
or canceled. An exact operation-key retry after reloading returns the original
result without a revision/event change; a changed request or brief is rejected.
Global source row locks serialize version creation across investigations. No token
usage, checkpoint phase, current-snapshot pointer, or existing evidence is changed.

Migration `0002` adds sources, source_versions, passages, evidence_inputs,
evidence_snapshots, and snapshot_versions. These records are append-only through
the application API. Typed JSONB manifests carry ordered references and exclusions;
foreign keys enforce the source/version/brief identities. As elsewhere in this store,
direct SQL updates are outside the supported interface. No database-specific
application role or immutable-row trigger is introduced.
