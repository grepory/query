# Stopping-decision fixtures and proposal rubric (NOT-41)

The design's nine stopping situations are represented by 20 synthetic prepared
histories in `evals/fixtures/stopping_decisions.json`. They contain actual Query
`Brief`, `Checkpoint`/`Budget`, and `LoadedSnapshot` contracts, exact source text,
passages, frozen provenance, upstream reporting origins, required-question history,
claims and removed claims, attempts, candidate actions, and expected decisions.
UUIDs are deterministic fixture identities; URLs use `fixtures.invalid` and are
never fetched. No production records, provider calls, or live search are needed.

Each candidate names missing evidence, consequence for the answer, next action,
stop condition, availability, research/review lane and simulated token estimate. Attempts retain their
targets, outcomes, source versions, usage and observed progress. Snapshot and brief
identity, origin relationships and question coverage are explicit rather than
inferred from prose. Every operational stop preserves a worthwhile next action;
the review-park case also distinguishes its pending answer from the last reviewed
answer. The corpus scope and stated exhaustion are **prepared assumptions**, not
claims that this repository has performed a real public-source investigation.

## Expected decisions and boundaries

| Family | Base history and expected decision | Boundary changing the decision |
| --- | --- | --- |
| Unread primary | `unread-primary`: research the credible ledger lead | `primary-read-and-settled`: ledger read, current answer reviewed and material avenues exhausted → stop, answered |
| Private fact | `private-fact-exhausted`: qualify after recorded public avenues fail to obtain a private schedule | `private-fact-user-source`: user can supply the schedule → await clarification/input |
| Circular reporting | `circular-ten-reprints`: ten outlets share one origin; research the independent ledger | `circular-no-independent-avenue`: independent avenue tried/unavailable → qualify the single-origin figure |
| Scope/date conflict | `scope-date-reconcile`: existing site/company and date differences explain numbers → revise | `scope-definition-ambiguous`: the user's operational/announced definition is unclear → clarify |
| Impossible certainty | `impossible-certainty`: decline an unsupported certainty demand; qualify after named reasonable avenues are exhausted | `certainty-new-material-lead`: a concrete new register could change the answer → research |
| Expensive useful action | `expensive-action-fits`: 60-token action fits 70 remaining → research | `expensive-action-does-not-fit`: allowance falls to 50 → stop at budget, preserving the action |
| Exhausted budget | `budget-exhausted-with-lead`: research exhausted despite review capacity → stop at limit | `budget-explicit-grant`: allowance increased, spend preserved → research; `budget-final-review-impossible`: final review cannot fit → park |
| Repeated evidence | `repeated-evidence-exhausted`: duplicate/reworded requests produce no progress → qualify after exhaustion | `repeated-evidence-new-method`: independent commissioning-register method opens → research |
| Removed hard claim | `removed-hard-required-claim`: claim deleted but required question open → research | `removed-claim-unavailable-evidence`: attempts justify unresolved status → qualify; `removed-optional-claim`: only optional context removed, required answer supported → stop, qualified |

Each case has a rationale, expected selected/preserved action IDs, retained question
IDs, separate integrity/sufficiency judgments, lifecycle target, limit reason or
outcome, required decision records, forbidden behavior, and case-specific rubric.
Qualification returns to full answer review; it is not an immediate completion.
A complete unanswered/qualified investigation still needs both checks. Budget or
deadline exhaustion never substitutes for research sufficiency. A remaining review
allowance cannot fund research by silently crossing lanes.

## Deterministic checks and the NOT-42 boundary

`tests/test_stopping_fixtures.py` validates references, snapshot text/byte identity,
frozen provenance, required-question retention, boundary pairs and malformed
histories. It applies the fixture's **already chosen** target to the real
`transition()` and checks phase, unchanged cumulative budget, resume phase and
outcome. Negative tests prove completion rejects either failing check.

These are current lifecycle-guard tests, not a stopping-policy router. The existing
store trusts caller-supplied attestations; it cannot know an unread ledger remains
when the caller falsely sets sufficiency to true. This ticket adds no alternate
policy controller and no skipped/xfail policy tests. NOT-42 must consume these
histories, run the real decision enforcement, and assert its decision, selected/
declined alternatives, retained requirements, persisted rationale and transition.
NOT-43 adds no-progress/materiality enforcement and the pre-search gate. No
autonomous acquisition becomes eligible through this fixture work.

## Human proposal scoring, separate from controller checks

The machine-readable case rubric is a **proposed rubric for human review**. No
human approval or quality result is claimed by the checked-in files. Astra's code
review is independent implementation review, not an invented human scoring pass.

Export input histories without gold expectations, family/boundary metadata or
rubric criteria:

```sh
mise exec -- uv run --locked python -m evals.score_stopping inputs > stopping-inputs.json
```

This command makes no model requests. A later explicitly opted-in evaluator may
collect proposals using these inputs; no live evaluator or provider credentials
are introduced here. Record the model and prompt version with every proposal.

A human first reviews the full fixture corpus and its `rubric_definition`, which
contains the versioned scoring anchors and every fatal-error definition mirrored
below. Those definitions are included in the corpus approval hash. The human corrects any
gold/rubric errors, and records `rubric_approval` with reviewer identity, current
corpus digest, `approved=true` and review notes. Pending/rejected approval cannot
produce a score. The tool records attributed assertions; it cannot authenticate
that an identity is human or independently prove that the review occurred.

For every proposal, a human reads the **actual rationale** against saved evidence,
attempts, candidate actions, expected records and the case's `rubric.must_explain`
and `rubric.fatal_examples`. Use all three dimensions; a correct decision label
does not supply a rationale score:

| Dimension | 0 — incorrect/unsupported | 1 — partly supported | 2 — fully supported |
| --- | --- | --- | --- |
| `evidence_and_scope` | Invents evidence, confuses date/scope, or counts repetitions as independent | Uses real evidence but omits a material scope/origin/uncertainty distinction | Binds the argument to saved versions, dates, scope and upstream origins, with explicit limits |
| `question_coverage` | Deletes/ignores a required question or treats claim removal as completion | Keeps the question but omits a material attempt, gap or unresolved reason | Retains every requirement, distinguishes optional context, and supports answered/unresolved disposition with history |
| `action_and_stop` | No credible bounded action or supported stopping reason; false completion | Direction is reasonable but missing material consequence, feasibility, stop condition or challenge disposition | Connects the material gap to a feasible bounded action, or supports exhaustion/unavailability/clarification/limit with alternatives and separate checks |

Record nonempty evidence, coverage and action notes. Fatal errors override perfect
numeric ratings: `invented_evidence_or_attempt`, `false_independence`,
`unsupported_sufficiency`, `lost_required_question`, `unreviewed_final_answer`,
`unbounded_or_unaffordable_action`. A budget stop labeled sufficient is fatal;
declining an impossible-certainty demand without examining credible new leads is
also unsupported sufficiency. Adjudication cannot manufacture missing evidence.

The offline aggregator accepts `ReviewBundle` JSON defined in
`evals/score_stopping.py`: `rubric_approval`, `proposals`, and `judgments`.
Each `Proposal` records case/model/prompt IDs, current brief/snapshot, decision,
target, outcome or limit reason, selected/preserved actions, retained questions,
both checks and rationale. Each `HumanJudgment` records case/reviewer, exact
`proposal_digest`, `case_digest`, and `rubric_digest` of the current global
`rubric_definition`, the three 0–2 ratings, three notes, and
`fatal_errors`. `digest()` hashes canonical JSON for these Pydantic records; hash
the complete current corpus for rubric approval. Any change to gold, rubric,
context, model metadata or rationale requires renewed applicable review. Reapproving
the corpus after changing an anchor cannot carry old per-proposal ratings forward;
each judgment must also identify the scoring definition actually used.

```sh
mise exec -- uv run --locked python -m evals.score_stopping score --input reviewed-proposals.json
```

Structural agreement (scope, label, transition, outcome/limit, actions, questions,
both checks) and human rationale scores are reported separately. A case passes
only with every structural check true, all three ratings 2 and no fatal error.
All 20 cases must be present for a suite pass; missing cases are explicit. Stale
digests, duplicate cases and unmatched judgments are errors. Exit codes: 0 for
complete passing suite, 1 for failed/incomplete suite, 2 for invalid review data.
Scores never execute decisions, create research records, or certify source truth.
Unit tests use explicitly synthetic reviewer records to verify these mechanics;
they are not quality-evaluation results or human endorsements.
