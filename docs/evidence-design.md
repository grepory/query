# NOT-34: supplied documents and immutable evidence snapshots

Ticket: https://linear.app/nothanks-io/issue/NOT-34/ingest-supplied-documents-into-immutable-evidence-snapshots

Status: design agreed with Sol on 2026-10-21; implementation and acceptance
coverage are included in this change. Implementation review is tracked on NOT-34.

## Scope and repository fit

Implement supplied Unicode text ingestion, globally reusable source identities and
versions, exact passages, investigation membership, and immutable snapshots. Follow
SQLAlchemy Core, PostgreSQL, fixed Alembic migrations, frozen Pydantic contracts,
row locks, expected revisions, and atomic events already used by InvestigationStore.
No provider calls, search, scraping, file parsers, findings, citation verification,
model packet construction, CLI, review policy, or automatic lifecycle transitions.
NOT-35/37/38/45/46 own those consumers. Supplied text is evidence, never a generated
summary substituted for its source. Cutoff is brief context, not an automatic filter.

## Text, identity, and provenance

Add evidence contracts and pure preparation/selection helpers in query/evidence.py.
Override Contract's str_strip_whitespace=True for evidence text: preserve every
code point, including CRLF, leading/trailing whitespace, and Unicode normalization.
Reject NUL and unpaired surrogates before PostgreSQL insertion. Hash exact UTF-8
bytes with SHA-256; offsets are Python Unicode code point offsets, end exclusive.
Do not use byte offsets, normalize whitespace, or infer semantic equivalence.

A caller supplies a stable document key, a nonblank display title, optional origin
URL, timezone-aware observed_at, and optional timezone-aware published_at. The key
is an opaque, case-sensitive document identifier; no filesystem path or URL
canonicalization is inferred. Origin URL is provenance only and is not fetched.
The same key can be used by multiple investigations. Different keys never merge
merely because content matches. An existing key's title/origin cannot silently be
replaced: conflict is rejected; aliases and metadata correction are deferred.

Source version identity is (source ID, content hash). A changed text creates a new
immutable version; reimporting identical text reuses it, including A -> B -> A.
Retrieval/publication metadata belongs to an immutable ingestion observation, not
the deduplicated version, so subsequent observations do not rewrite provenance.
An investigation acquires versions through observations; sources are not consumed.

Partition accepted text into consecutive slices of at most 2,000 code points,
covering the entire text without overlap or gaps. This is deterministic storage
addressing, not semantic chunking. Passage identity is (version ID, start, end),
with a UUID for convenient references. Passage text is always saved_text[start:end];
do not store a second editable copy. Empty text is a recorded rejection.
Persist partition format version 1; later algorithm changes must not regenerate
existing passage identities. Citations in NOT-38 will name a version and passage.

## Size limits and explicit exclusions

The API receives text, not a file reader. Validate encoding first; do not silently
decode or slice a byte stream. Default ingestion ceiling is 1 MiB UTF-8 per document.
If exceeded, commit a rejected observation containing source key, exact hash,
byte/code point counts, timestamps, configured limit, and reason `document_too_large`;
do not save partial text or create a source version. Return a typed rejection after
commit (not an exception that rolls the transaction back). Unsupported text and
empty input are treated separately: empty valid UTF-8 is a recorded rejection;
NUL, unpaired surrogates, and invalid typed metadata fail validation before mutation.
Unsupported text is never embedded in PostgreSQL text or JSONB. Limit configuration is positive and
is recorded per observation. The caller can explicitly retry with a larger limit
under a new operation key; this is not a model-token or budget limit.

Snapshot requests enumerate distinct candidate version IDs acquired by this
investigation and optionally explicit passage IDs per version. The candidate list
is the explicitly declared corpus boundary, not a claim that every previously
acquired document was considered. Persist that boundary in the manifest. An omitted selection
means all passages; an explicitly empty selection excludes that version with a
required reason. Partial selection requires a reason, retained for every omitted
range. No implicit newest-version lookup or future automatic membership expansion.
All candidate versions must appear in the manifest even when entirely excluded.
Rejected observations for the current brief are copied into the manifest as input
exclusions; callers cannot omit them to imply a complete corpus. Prior-brief
rejections remain in history, and reuse requires a new explicit observation.

Default snapshot ceiling is 256 KiB of selected UTF-8 passage text. Compute it in
code, not estimated tokens. If the requested selection exceeds the ceiling, commit
an immutable `limited` manifest with each requested passage explicitly excluded
for `snapshot_too_large`, the requested size, and configured ceiling. It contains
no usable included passages and must not be presented as a ready snapshot.
An empty selection is likewise `limited` with an explicit reason. A new operation
can select fewer passages or increase the limit. Partial but nonempty selections
within the ceiling are `ready` with their omissions visible. Ready means usable
selected text, not research completeness or source truth. No silent truncation.

## Persistence and transaction contract

Add a fixed 0002 migration and matching db.py tables:

* sources: UUID PK, unique document key, immutable title and origin URL.
* source_versions: UUID PK, source FK, hash, exact text, byte/code point lengths,
  partition version; unique (source_id, hash).
* passages: UUID PK, version FK, start/end; unique (version_id, start, end),
  nonnegative start and end > start. Validate bounds against saved text in code.
* evidence_inputs: (investigation_id, operation_key) PK, brief-version composite
  FK, immutable request fingerprint and observation payload, nullable version FK,
  accepted/rejected status. A rejected observation has no version. Accepted
  observations provide the many-to-many investigation/version relationship.
* evidence_snapshots: UUID PK, investigation FK, operation key, brief version,
  format version, ready/limited status, immutable JSONB manifest and fingerprint;
  unique (investigation_id, operation_key), composite FK to the brief.
* snapshot_versions: (snapshot_id, version_id) PK and both FKs; all candidate
  versions, including excluded ones. Manifest passage IDs must belong to these
  versions and to the named investigation's accepted observations (checked in code).

At freeze time, copy the ordered accepted observation keys available for each
candidate version into the manifest. Each key must name this investigation and
that version. Preserve their original brief version and metadata. Snapshot reads
resolve only these frozen references, never observations imported afterward.
Earlier-brief observations can be explicitly reused as candidates without copying
or changing their historical brief; the snapshot itself names the current brief.

JSONB manifests store ordered candidate versions, ordered passage references,
included/excluded dispositions and reasons, rejected-input copies, totals, and
size policy. They do not duplicate source text. Ordering is explicit, not row order.
Application writes append only to evidence records; updates/deletes are unsupported.
Use FKs without cascading deletes. As in existing persistence, direct SQL bypasses
application validation; do not introduce a trigger-based security framework.

Add concrete methods to InvestigationStore: ingest_text, freeze_snapshot,
load_source_version, and load_snapshot. Keep evidence decisions in pure helpers;
store methods handle transactions only. No generic repository layer. Evidence read
methods return typed records independently of Investigation, avoiding changes to
existing persisted checkpoint/step contracts. Loading a snapshot reconstructs the
exact saved texts and selections, including all exclusions, with stable ordering.

Each mutation locks its investigation, verifies expected_revision, rejects terminal
or held phases, and requires no pending step. Permit SCOPE/PLANNING/GATHERING/
ASSESSING, reject REVIEWING so new evidence does not quietly change a review corpus.
No automatic transition or budget spending. Preserve current brief in the record.
Every new accepted/rejected input or ready/limited snapshot increments the revision
once and writes one event atomically. Failure rolls back everything. Reusing an
operation key after reloading is a no-op only if the full request fingerprint and
brief match; changed inputs are rejected. Check expected revision first, matching
existing step semantics. Compute request fingerprints from deterministic canonical
JSON over validated request fields: exact UTF-8 text hash and counts, source key,
metadata, and limits for ingestion; ordered candidates/selections/reasons and
limits for snapshots. Do not include generated IDs, database timestamps, or derived
observation lists in request identity. Exact retry returns the previously frozen
result even if newer observations exist. Validate full stored request fields in
addition to the digest; reject a content hash collision with unequal saved text.
Snapshots never update previous manifests or a current
snapshot pointer. Different keys may create equivalent snapshots intentionally;
NOT-47 owns no-change acquisition behavior.

For global source/version races, use PostgreSQL ON CONFLICT DO NOTHING plus
reselect and verify existing metadata/text. Never catch IntegrityError inside an
aborted transaction. Source identity is serialized by inserting/selecting the
source row FOR UPDATE before creating versions/passages. Lock investigation first,
then source; ingest handles one source per transaction. Identical retries reuse
passages; matching hash with differing text fails explicitly. No sequential version
number allocation or cross-investigation ownership is needed.

## Implementation and validation plan

1. Add exact-text contracts, hashing/partitioning, selection and limit decisions;
   unit-test CRLF, whitespace, composed/decomposed Unicode, emoji offsets, empty and
   unsupported text, boundaries, invalid limits, duplicate and foreign passages. NUL and surrogate validation
   must leave the database untouched; empty/oversized UTF-8 retry identity is stable.
2. Add fixed migration and metadata, typed append-only store operations and reads.
   Extend actual PostgreSQL migration/metadata/downgrade tests without SQLite.
3. Exercise A/B/A imports, shared sources across investigations, independent keys
   with equal content, multiple observations, metadata conflicts, deterministic
   passage identities, and reconstructing the same old snapshot after newer source
   content and a new brief. Freeze, then import identical text with new observation
   metadata and verify the old snapshot provenance and idempotent retry stay equal.
   Test all exclusions, exact thresholds, and limited status.
4. Exercise same/different operation keys, stale revisions, held/terminal/reviewing
   phases, pending steps, concurrent imports into one and different investigations,
   concurrent snapshot/brief changes, and injected failure before event commit.
   Assert no partial rows/events/revision changes on rollback. Verify fresh engine
   reload retains exact text, manifest ordering, provenance, and excluded inputs.
5. Update persistence docs and README's supplied-text API description. Run
   `mise run check`, with QUERY_TEST_DATABASE_URL targeting only loopback query_test
   for the PostgreSQL suite. No new dependencies or uv.lock change is anticipated.

Preserve the implementation jj workspace until its changes are confirmed merged
to main, following AGENTS.md cleanup rules.

## Adversarial review outcome

Sol (gpt-6.1-sol) reviewed the design against the current repository, challenged
provenance reproducibility and unsupported Unicode rejection identity, then reviewed
the revisions. Accepted fixes freeze observation references in each manifest and
reject unrepresentable text before mutation. Retry identity excludes later-derived
observation lists, preserving the original result. The explicit candidate universe
defines corpus scope without claiming all historical inputs were considered.

Sol's final assessment: the revised design is sound and implementation-ready within
NOT-34's scope; no remaining design blockers. The primary designer agrees.

Implementation review: Sol identified silent byte decoding in the input text field.
The field is now strict, and regression tests reject bytes and bytearray inputs.
After reviewing the revision and acceptance coverage, Sol agreed the implementation
is sound with no remaining blockers. The implementing agent agrees; no unresolved
disagreement or refactoring proposal remains.

Implementation validation: `QUERY_TEST_DATABASE_URL=postgresql://127.0.0.1:55434/query_test
mise run check` passed against an isolated PostgreSQL 18 cluster: 85 tests passed,
none skipped, plus lint and formatting. Both agents audited all SQL interactions;
[the audit](sql-audit.md) records the clean result and regression coverage.
