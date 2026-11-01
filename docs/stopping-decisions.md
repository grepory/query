# Stopping-decision fixtures and proposal rubric (NOT-41)

The design's nine stopping situations are represented by 20 synthetic prepared
histories authored in [`stopping_decisions.yaml`](../evals/fixtures/stopping_decisions.yaml).
Its shared sources, attempts, actions, defaults, and scoring anchors live in
[`stopping_context.yaml`](../evals/fixtures/stopping_context.yaml).
The generated `evals/fixtures/stopping_decisions.json` contains actual Query
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
Each history exposes its finite research universe in `scope_assumptions`. The
repeated-search, unavailable-claim, and impossible-certainty cases record separate
register/ledger and operator attempts; repeated queries alone do not establish
sufficiency. New leads explicitly expand that universe. These fixtures test
decisions given prepared histories, not whether a model can discover every public
avenue or prove that actual web research was exhaustive.

## Authoring and reviewing fixtures

Edit YAML, then regenerate the contracts:

```sh
mise run fixtures:stopping
mise run fixtures:check
mise exec -- uv run --locked python -m evals.generate_stopping review --case expensive-action-does-not-fit
```

The review command prints a resolved YAML case with its actual source text,
attempts, actions, budget, expected decision, and rubric, without UUIDs or snapshot
plumbing. Omit `--case` to inspect all cases. The scenario file is the short review
entry point; the context file holds each reusable record once.

Base cases inherit `defaults`. A boundary's `extends` names one base case and
replaces whole fields: lists and mappings are never recursively merged or
implicitly appended. Every case declares its own `expected` object and uses only
the common `expected_defaults` for omitted fields. A variant cannot extend another
variant. Unknown fields/references and duplicate YAML keys are rejected.

The compiler preserves parsed source text exactly and generates content-bound
source/version IDs, hashes, byte counts, passage ranges, frozen provenance and
snapshot manifests. YAML `>-` folds prose into a single line; use `|` for source
text whose newlines matter. A changed source regenerates its version identity and
snapshot membership. The result validates against the real Query contracts.
`mise run check` rejects stale generated JSON, so authors and tests cannot silently
use different histories. Commit both YAML files and regenerated JSON together.
The JSON is marked generated for GitHub diffs; review the authored YAML and compiler.

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
Both expected assessments describe the current history at the decision point.
`answer_integrity` concerns `answer_ref`, not a hypothetical future qualification:
it remains true when a clean current answer needs additional research or a later
qualification. Every newly written answer must still receive full review. The
five qualify cases therefore keep current integrity true; the scope/date mistake
and pending unreviewed answer fail it. An unchanged answer and evidence do not
gain or lose integrity merely because a research action becomes available.
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

## Human rubric approval and attributed proposal reviews

The machine-readable case rubric is a **proposed rubric for human review**. No
human approval or quality result is claimed by the checked-in files. Astra's code
review is independent implementation review, not an invented human scoring pass.

Export input histories without gold expectations, descriptive case names,
family/boundary metadata or rubric criteria:

```sh
mise exec -- uv run --locked python -m evals.score_stopping inputs > stopping-inputs.json
```

This command makes no model requests. A later explicitly opted-in evaluator may
collect proposals using these inputs; no live evaluator or provider credentials
are introduced here. Record the model and prompt version with every proposal.
Each exported `id` is an opaque, stable UUID; use it as `Proposal.case_id` and
`Judgment.case_id`. Descriptive case names are rejected by the scorer. The operator
can obtain the separate case mapping and current review digests with:

```sh
mise exec -- uv run --locked python -m evals.score_stopping manifest > stopping-manifest.json
```

Keep this manifest and the gold/rubric examples out of the proposal model's input.
Changing a case's evidence preserves its evaluation ID but invalidates its digest.

A human first reviews the full fixture corpus and its `rubric_definition`, which
contains the versioned scoring anchors and every fatal-error definition mirrored
below. Those definitions are included in the corpus approval hash. The human corrects any
gold/rubric errors, and records `rubric_approval` with reviewer identity, current
corpus digest, `approved=true` and review notes. Pending/rejected approval cannot
produce a score. The tool records attributed assertions; it cannot authenticate
that an identity is human or independently prove that the review occurred.

For each proposal, either a human or a model reads the **actual rationale** against saved evidence,
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
both checks and rationale. Each `Judgment` records case/reviewer, exact
`proposal_digest`, `case_digest`, and `rubric_digest` of the current global
`rubric_definition`, the three 0–2 ratings, three notes,
`fatal_errors`, and `status` (`assessed` or `needs_human_review`). Reviewer attribution
is structured and mandatory:

```json
[
  {"kind": "human", "name": "reviewer identity"},
  {"kind": "model", "name": "judge role", "model": "exact-model-version", "prompt_version": "judge-prompt-v1"}
]
```

`rubric_approval.reviewer` accepts only the human shape. Proposal judgments may
use either shape, including mixed reviewers across a suite. Model reviews cannot
substitute for rubric approval and are never reported as human scores. The tool
validates attributed records; it cannot authenticate whether the named person
actually reviewed them. Synthetic records in tests are not approval.

`digest()` hashes canonical JSON for these Pydantic records; hash
the complete current corpus for rubric approval. Any change to gold, rubric,
context, model metadata or rationale requires renewed applicable review. Reapproving
the corpus after changing an anchor cannot carry old per-proposal ratings forward;
each judgment must also identify the scoring definition actually used.

Human review owns initial gold/rubric approval, policy changes and disputed cases;
it is not mandatory for every routine proposal. For a model judge, first request
an independent decision from the blind history and policy, then compare the
proposal's rationale with evidence and the approved criteria. Require concrete
source/attempt references and counterarguments. Hide the proposing model's identity
from the judge where practical. Use `needs_human_review` when the record cannot
support a confident assessment; all numeric ratings still require notes. This
status prevents a pass even with perfect scores. A human may replace the judgment
after resolving the disagreement; retain previous bundles as the audit history.
Calibrate the judge against human-scored examples and seeded fatal errors, and
audit a sample of later results. A different model is useful but does not prove
independence or accuracy. This offline aggregator does not run a judge or claim
that any judge has been calibrated.

```sh
mise exec -- uv run --locked python -m evals.score_stopping score --input reviewed-proposals.json
```

Structural agreement (scope, label, transition, outcome/limit, actions, questions,
both checks) and rationale scores are reported separately. Reports retain each
reviewer's kind, identity and model/prompt metadata in `reviewer`, with neutral
`rationale_scores` and `review_notes` fields. A case passes only with every
structural check true, all three ratings 2, status `assessed` and no fatal error.
All 20 cases must be present for a suite pass; missing cases are explicit. Stale
digests, duplicate cases and unmatched judgments are errors. Exit codes: 0 for
complete passing suite, 1 for failed/incomplete suite, 2 for invalid review data.
Scores never execute decisions, create research records, or certify source truth.
Unit tests use explicitly synthetic reviewer records to verify these mechanics;
they are not quality-evaluation results or human endorsements.
