# Review behavior inventory (NOT-36)

This inventory maps the aibyai review loop to prepared **Query** histories before
porting model calls or building the review controller. The reference is
[`grepory/aibyai` at `ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9`](https://github.com/grepory/aibyai/tree/ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9).
All source paths below refer to that revision. The history corpus lives in
[`tests/fixtures/review_invariants.json`](../tests/fixtures/review_invariants.json).
Each case records its exact source test/function, future implementation ticket,
changed assumptions, saved drafts/reviews/defenses/rulings, budget, trigger,
expected actions, forbidden actions, and last visible reviewed answer. Ruling IDs
link committed adjudicator results; `after_rulings` explicitly marks later drafts
that were created after those rulings, even if the author ignored their edits.

These are synthetic examples, not production research or captured provider
responses. No aibyai code, configuration, database, imports, or checkout is needed
to load or test them. No implementation helpers were copied: the future tickets
should port a helper only when their concrete implementation needs it.

## What passes today

`mise run check` checks corpus parsing, valid references, identity uniqueness,
budget values, saved-result links, reviewed output references, and failure cases
for malformed histories. It does **not** prove the future review actions work.
There is no alternate review controller, fake policy engine, skipped controller
test, or xfail standing in for NOT-39/40.

The test-only schema in `tests/review_cases.py` describes fixture records, not
proposed production schemas. Local IDs such as `d1`, `v1`, and `s1` are readable
fixture aliases; downstream tests allocate real Query UUIDs/records. `v3` is saved
but excluded from `s1`; `s2` replaces `v1` with changed evidence `v4`. The brief
restricts sufficiency to supplied documents. Review `publishable` is a deliberately
fallible model assertion, never the controller's settlement result.

`review_case` and `review_corpus` are shared pytest fixtures in `tests/conftest.py`.
Consumers should select cases for their ticket, materialize the prepared records,
run the real Query component with fake clients or Pydantic AI test models, and
assert the listed next actions, forbidden calls, persisted results, visible draft,
and cumulative usage. Ordered `expected.actions` may include free bookkeeping
before or after a provider call; they are acceptance instructions, not executable
workflow syntax. `trigger` describes the injected event or fake-clock condition.
Tests added with the controller must supply that event and observe its effects.

## Inherited invariants and deliberate changes

### Final review and reconstructed state — NOT-39, NOT-40

Sources: `src/services/authoring.py` (`_step`, `_review`, `_deliberate`, `_result`),
`src/services/authoring_state.py` (`load_state`, `reviewed_post`);
`tests/test_digest_authoring.py::test_the_stored_post_was_always_reviewed` and
`tests/test_authoring_resume.py::test_a_round_with_nothing_to_change_still_gets_a_final_review`.

Reconstruct the current draft and its review from durable records. Approval of an
older draft does not review a later draft. Exports retain the latest reviewed
answer while a new draft is pending. Even when every repeated objection is
upheld and no text changes, save/re-present that answer for a final full review.
Reviews and paid drafts remain append-only; Query needs no legacy digest-draft
fallback.

Cases: `review-pending-revision`, `upheld-still-final-review`,
`final-review-unaffordable`.

**Changed budget assumption:** aibyai `_exhausted` checks between adjudications
and revisions, then always reviews a paid revision even over its allowance.
Query reserves review capacity and pre-admits bounded calls. If final review
cannot fit, park with the last reviewed draft and preserve the pending revision;
do not copy the unconditional overspend behavior.

### Per-issue responses — NOT-37, NOT-39

Sources: `src/services/authoring.py` (`_author_response`, `_checked_response`,
`_critique`), `src/clients/author.py` (`IssueResponse`);
`tests/test_digest_authoring.py` tests
`test_author_concession_and_targeted_revision_are_persisted` and
`test_reviewer_can_withdraw_after_an_issue_specific_defense`.

Preserve each defense, concession, checked evidence item, and applied edit.
A fresh objection gives the author an opportunity to respond before escalation;
the next reviewer can withdraw it. An unstructured response remains visible but
cannot be represented as an evidence-backed per-issue defense.

Cases: `issue-specific-defense`, `concession-saved`, `free-form-is-not-evidence`.

### Repeated disputes and productive revision — NOT-39

Sources: `src/services/authoring.py::_deliberate`,
`src/services/authoring_state.py::claim_key`; `tests/test_digest_authoring.py`
tests `test_a_reworded_objection_to_the_same_claim_still_escalates`,
`test_new_objections_reach_the_author_in_a_round_that_adjudicates`,
`test_a_moving_argument_is_not_a_stall`, and
`test_review_count_no_longer_ends_a_productive_factual_review`.

Escalate a repeated dispute over the same scoped claim, regardless of how the
reviewer rephrases its objection. Fresh defects still reach the author in a round
that adjudicates older disputes. A new defect is not a stall just because the
round count increased; budgets still bound work.

Cases: `reworded-objection-escalates`, `fresh-and-repeated-issues`,
`fresh-claim-not-a-stall`.

**Changed identity assumption:** the source key lowercases a quoted sentence,
collapses whitespace, and strips edge punctuation. It does not establish semantic
equivalence. Query carries stable IDs with claim text and snapshot scope;
conservative matching must avoid attaching a ruling to a changed assertion.

### Binding rulings — NOT-39; new evidence integration in NOT-47

Sources: `src/services/authoring.py` (`_deliberate`, `_adjudicate`, `_render_rulings`),
`src/services/authoring_state.py::load_state`; `tests/test_digest_authoring.py`
tests `test_adjudicator_narrowing_or_removal_is_applied_then_finally_approved`,
`test_a_ruling_the_author_missed_is_reapplied_not_readjudicated`, and
`test_an_upheld_claim_is_not_relitigated_under_new_wording`.

Apply narrowing/removal rulings, persist the author's applied edit, and review the
whole edited answer. If the author ignores a standing ruling, reapply it without
buying another adjudication. An upheld unchanged claim cannot be relitigated
under a new objection wording against the same snapshot.

Cases: `binding-narrow-reapplied`, `binding-remove-reapplied`,
`upheld-same-scope`, `new-snapshot-reopens-ruling`,
`changed-claim-reopens-ruling`.

**Changed scope assumption:** source `upheld` and `standing` are run-wide maps.
Query rulings apply only to the exact claim against the adjudicated snapshot.
Reused IDs, a changed quantity, or a new snapshot cannot inherit approval. Keep
old rulings in history and reassess affected claims.

### Editorial feedback and factual blockers — NOT-39

Sources: `src/services/authoring.py::_review`; `tests/test_digest_authoring.py`
tests `test_an_editorial_objection_cannot_cost_a_round`,
`test_editorial_notes_ship_with_the_post`, and
`test_a_factual_objection_overrules_the_reviewers_own_sign_off`.

Editorial-only rejection cannot cost a round, block settlement, or request
research. Retain the note. Conversely, a factual defect blocks settlement even
when the reviewer asserts `publishable=true`. Settling the answer loop is not
investigation completion: both integrity and sufficiency checks are still needed.

Cases: `editorial-only-rejection`, `signoff-with-factual-blocker`.

### Checked evidence and adjudication packets — NOT-37, NOT-38, NOT-39

Sources: `src/services/evidence.py` (`normalize`, `EvidenceIndex.verify`,
`EvidenceIndex.check`, `EvidenceIndex.relevant`),
`src/services/authoring.py` (`_adjudicate`, `_revision_sources`);
`tests/test_digest_authoring.py` tests
`test_excerpts_are_checked_against_the_corpus`,
`test_the_adjudicator_reads_source_neither_party_quoted`, and
`test_the_reviewer_sees_the_same_corpus_as_the_author`.

Check quotes from both parties mechanically and preserve verification failures.
The adjudicator gets both arguments and independently selected relevant saved
passages, including sources neither party quoted. Author and reviewer use the
same frozen evidence. Model summaries only help navigation.

Cases: `wrong-source-real-quote`, `excluded-source-real-quote`,
`fabricated-excerpt`, `ordered-normalized-quote`, `reversed-elision-rejected`,
`cross-source-elision-rejected`, `adjudicator-gets-independent-passages`.

**Changed verification assumptions:** the reference searches summaries/company
blocks across the corpus, ignores the evidence source label, and accepts elision
fragments in any order provided they occur in one block. Preserve useful NFKC,
typographic-quote, whitespace and case normalization; reject wrong source IDs,
out-of-snapshot passages, altered text, reversed fragments and fabricated joins.
Its four-block packet cap has no omission metadata. Query must expose selected
passages, truncation and missing context rather than silently presenting a
partial packet as complete. Quote occurrence alone does not establish a claim's
meaning or an inference's validity; those remain review judgments.

### Closeout, allowances and accounting — NOT-40

Sources: `src/services/authoring.py` (`_deliberate`, `_reserve`, `_exhausted`,
`_reopen`, `grant`), `src/services/authoring_state.py::load_state`, `src/usage.py`;
`tests/test_authoring_budget.py` tests
`test_the_soft_limit_switches_to_closeout`,
`test_entering_closeout_survives_a_failure_and_a_grant`,
`test_the_reserve_counts_the_last_paid_revision`,
`test_the_hard_limit_parks_before_paying_the_author`, and
`test_a_grant_is_clamped_to_the_lifetime_ceiling`.

Enter closeout as a separately committed step before the next paid call; failure
and a grant must not undo it. Fresh factual objections go straight to rulings in
closeout; preserve capacity for binding edits and full final review. A zero-cost
unchanged/settlement draft must not hide the last paid revision when estimating
reserve. Reconstruct cumulative usage from committed work, refresh grants, and
never reset spend on resume. Check allowance between separate adjudications.

Cases: `closeout-fresh-dispute`, `closeout-survives-failure-grant`,
`hard-limit-preserves-reviewed`, `final-review-unaffordable`.

**Changed status/accounting assumptions:** source uses one token allowance and
labels reviewed parked runs `unresolved`. Query separates research/review
allowances and a lifetime ceiling; stopped-at-limit or parked is not an answer
outcome or sufficiency decision. Provider pricing, input estimates and output
bounds need explicit admission/accounting. Source's hardcoded retry timings,
reserve estimator and grant mechanism are reference behavior, not new Query
settings fixed by these fixtures. NOT-48 owns full cross-provider budgets.

### Resume, provider failure and ownership — NOT-40; extended faults in NOT-50

Sources: `src/services/authoring.py` (`_drive`, `_eligible`, `_pause`, `_park`),
`src/services/retry.py` (`call_with_retries`, `next_retry_at`),
`src/services/run_lock.py`; `tests/test_authoring_resume.py`,
`tests/test_authoring_retry.py`, `tests/test_authoring_state.py`.

Commit each paid result before advancing. Resume an unreviewed paid draft at
review, and a partial round at its pending adjudication. Permanent provider
failures do not retry; transient failures retry only the same step, then pause if
exhausted. Unrelated programming exceptions propagate. Source in-process waits
are 5/20/60 seconds and delayed pauses back off 1/2/4 hours within 24 hours, capped
at the deadline; Query need not copy those constants or DBOS scheduling.

Cases: `resume-paid-draft`, `resume-partial-round`,
`resume-between-adjudications`, `resume-remote-outcome-unknown`,
`permanent-failure-parks`, `stale-retry`,
`not-due-retry`.

**Changed recovery assumption:** persist Query operation intent before I/O and
result before advance. Remote success followed by a pre-commit crash can repeat
a billable call; local idempotency is not exactly-once remote billing. Preserve
the pending operation and mark uncertainty for reconciliation. Revalidate due
time, current state and ownership immediately before work. Stale/canceled
continuations and competing executors must do no paid work. Do not port digest
retry generations, the weekly job, scheduler, publishing recovery or DBOS IDs.

### Coverage is not omission bookkeeping — NOT-39, NOT-41, NOT-42

Sources: `src/services/authoring_state.py` (`coverage_problems`, `settle_coverage`);
`tests/test_digest_authoring.py` tests
`test_an_article_neither_covered_nor_omitted_blocks_approval`,
`test_an_article_id_from_nowhere_blocks_approval`, and
`test_a_repeated_coverage_gap_is_settled_mechanically`.

Case: `missing-required-question`.

**Changed requirement assumption:** aibyai can mechanically account for omitted
articles and discard unknown coverage IDs. Query retains the brief's required
questions, their evidence, attempts and unresolved reasons. Removing a hard claim
does not close its question. Unknown citations fail rather than being silently
trimmed. The full nine-case stopping fixture suite and boundary variants belong
to NOT-41; these histories identify their inherited entry point and do not
replace that task. No autonomous acquisition before the NOT-43 gate.

## Pickup boundaries

NOT-37 consumes response/packet cases for new model contracts; NOT-38 implements
the source-bound verifier; NOT-39 implements the durable controller against these
histories; NOT-40 supplies execution ownership, recovery and closeout. NOT-35's
CLI remains an independent prerequisite of NOT-39. Preserve the provenance and
changed assumptions when converting a case into an executable behavior test.
Do not port digest tables, company selection, article claims/queues, Ghost,
scheduling, deployment scripts or production data.
