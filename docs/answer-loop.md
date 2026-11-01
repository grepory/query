# Durable answer review (NOT-39)

`query.answer_loop` reconstructs decisions from committed Query steps. The store
validates each planned operation and result under the investigation row lock.
There is no in-memory round counter or mutable draft pointer. Existing step JSON
stores typed drafts, full reviews, rulings, issue responses, application records,
model/prompt versions and usage; this change needs no schema migration.

## Execution

Freeze a ready supplied-document snapshot, then move the investigation to
`reviewing`. Execute one action at a time with explicit role configuration:

```sh
uv run query advance INVESTIGATION_ID --revision REVISION \
  --answer-models /path/to/models.json --call-limits /path/to/call-limits.json
```

`models.json` is `ReviewModels` JSON. For example, replace the placeholder model
names with explicit hosted model identifiers:

```json
{
  "author": {"model_backend": "anthropic", "model_name": "YOUR_AUTHOR_MODEL"},
  "reviewer": {"model_backend": "openai", "model_name": "YOUR_REVIEWER_MODEL"}
}
```

Set `QUERY_ANTHROPIC_API_KEY` and `QUERY_OPENAI_API_KEY` in the environment; keep
credentials out of configuration files. Adjudication defaults to the reviewer
model. The author/reviewer must use different hosted vendors. All three role
model identities are pinned for a snapshot's answer loop, preventing vendor
swaps between separate advances. A new snapshot starts a new loop.

Optional call-limits JSON contains `max_output_tokens`, `max_prompt_bytes`, and
`timeout_seconds`. The default limits are those of `ReviewClients`. Before each
paid operation, the controller reserves prompt-byte cap plus output-token cap
against the existing review and total token allowances. This deliberately
conservative admission bound may require a larger allowance than actual usage.
The input estimate includes schema/provider framing. NOT-40 additionally protects
binding edits and final full review, records attempt usage and handles closeout,
ownership and failure holds; see [review-recovery.md](review-recovery.md). These
token bounds are not a dollar budget.

Inspect after each action to obtain the next revision. The returned `LoopState`
contains `next_operation`, typed history, current reviewed answer and routing
reason. The caller chooses when to advance again. Settled/research-blocked/limited
states perform no model call and do not complete the investigation. No autonomous
research is enabled.

## Routing and identity

1. Write the initial full answer, then review it against the same immutable
   context. Every author revision receives a full review before becoming visible.
2. Fresh factual/inference objections reach the author with per-issue responses.
   Editorial-only rejection settles without buying another round; factual issues
   block even when the reviewer says `publishable=true`.
3. A repeated exact claim with a saved response reaches adjudication. Identity
   includes brief, snapshot, claim ID, exact text and full claim support/premises;
   changed claims do not inherit approval. Objection wording and issue-occurrence
   IDs do not establish a new dispute. Prior responses retain their original
   review/issue IDs and saved review in the adjudication packet.
4. Persist each ruling separately. Supported claims cannot be relitigated under
   new objection prose. Other fresh defects still reach the author. Even if no
   edit is needed, save a zero-cost unchanged draft and perform a final full review.
5. Narrow/remove rulings require explicit application records and full review.
   An unchanged ruled sentence in title/body cannot escape a binding edit by
   being removed/renamed in the claim inventory or receiving different citations.
   Ignored edits route back to revision without another adjudication. Exact text
   checks cannot certify semantic compliance; the full reviewer still assesses it.
6. Coverage objections are separate from claim rulings. Repeated coverage gaps,
   unresolved required questions and insufficient-evidence rulings return
   `needs_research` with their reasons. Removing a claim retains every required
   question. Research decisions and sufficiency enforcement belong to NOT-42.

`settled` means no unresolved integrity objections in this loop. It establishes
neither source truth nor research sufficiency. Lifecycle completion still uses
caller-supplied attestations until NOT-42 connects assessed completion checks.

## Durability and export

Operation keys and output UUIDs derive from snapshot and operation sequence.
Persist intent before I/O, then commit the typed result and usage before advancing.
A fresh controller resumes an already committed draft at review and a partial
adjudication round at the next unsaved ruling. Results cannot be overwritten.
Pending operations pin their role/model, prompt version, context and call bounds;
changing their request is rejected.

Exports add two optional fields to format version 1:

- `answer_loop`: current-brief/latest-snapshot state, including
  `current_answer` paired with its exact full review. While a new draft awaits
  review, the previous reviewed answer remains current within that snapshot.
- `last_reviewed_answer`: historical answer/review pair across snapshots and
  briefs. Its original brief/snapshot IDs remain explicit. It remains inspectable
  after a new snapshot or brief clears current approval; it is not approval under
  the new evidence.

Raw operation history remains in `investigation.steps`, including unreviewed
drafts. Consumers must use the reviewed pair fields for displayed answers rather
than selecting the last author result. New evidence retains historical rulings
but cannot suppress new-snapshot objections.

Provider failures pause or park while preserving the intent; unknown remote
outcomes require a conservative reconciliation and explicit resume before retry.
A PostgreSQL session lock prevents competing local executors, with ownership
revalidated before paid I/O and result commit. Remote calls already in flight
remain subject to outcome/billing uncertainty. See the recovery guide for retry
times, lifetime ceilings, reserve and per-attempt accounting.

## Regression provenance

`tests/integration/test_answer_loop.py` materializes the NOT-36 synthetic histories
for pending revision, defense/concession, repeated/fresh disputes, binding edits,
editorial feedback and missing required questions. Their pinned aibyai provenance
is in [review-invariants.md](review-invariants.md). These tests execute the actual
controller/store with fake clients and an end-to-end Pydantic AI `FunctionModel`
loop. Provider calls never run in CI. Additional cases cover historical visibility,
pinned model identities, invalid result commits, failure/restart, and independent
adjudication commits. NOT-40 adds the recovery regressions; NOT-47/50 retain their documented boundaries.
