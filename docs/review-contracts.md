# Author, reviewer and adjudicator contracts (NOT-37)

`query.review` defines Query's answer/review records. `query.review_clients`
performs explicit, bounded Pydantic AI calls. Neither module changes the durable
investigation lifecycle, runs research, or decides whether enough research has
been done. The [durable controller](answer-loop.md) wires persistence in NOT-39; execution
ownership, budget admission and scheduled retries belong to NOT-40. Mechanical
quote verification belongs to NOT-38 and stopping enforcement to NOT-42.

## Scope and evidence

A `ReviewContext` combines a versioned brief with a `LoadedSnapshot`. Construction
checks matching brief/snapshot scope, source membership, exact saved passage
ranges, observation/source ownership and selected-byte accounting. Its rendered
payload contains the brief, immutable manifest, frozen observation metadata and
only included source-passage text. Exclusions and limited-snapshot reasons remain
visible; excluded text is not sent. A limited snapshot has no usable passages and
can support an explicit unresolved answer, never a fabricated citation.

Initial author and full reviewer calls receive the same context payload. Every
successful call records its digest, configured vendor/model, role, prompt version,
usage and whether an offline test model was injected. IDs for new answers, reviews
and rulings are supplied by the caller. The client rejects returned IDs, brief
versions or snapshot IDs that do not match the request.

Citation records name immutable source-version and passage IDs and preserve exact
excerpt whitespace. The [citation verifier](citation-verification.md) checks quote
occurrence in the named selected passage and resolves it to original saved text.
It does not establish semantic support or inference validity; reviewers and
adjudicators still assess what the evidence means.

## Role records

| Record | Contents and constraints |
| --- | --- |
| `Answer` | Full title/body, scoped ID, claims, per-question coverage, issue responses and ruling applications. Every inventoried claim quotes an exact span of title/body. |
| `Claim` | Source assertion with citations, or inference with individually cited premises and explicit reasoning. Question IDs must exist in the brief. |
| `Coverage` | Every brief question appears exactly once, including unresolved and optional questions. Answered coverage names supporting claims; every disposition has a rationale. Removing a claim cannot delete coverage. |
| `Review` | Exact answer/snapshot/brief scope, advisory `publishable`, assessment, scoped issues and complete question coverage. A true sign-off may still contain factual issues; the record preserves that contradiction for the controller. |
| `Issue` | Stable issue key, factual/inference/coverage/editorial kind, exact claim ID/text when applicable, question target, evidence examined, gap explanation and suggestion. Missing evidence is representable without inventing an excerpt. |
| `IssueResponse` | Exact reviewed issue/claim and review ID, defense/concession/applied ruling, evidence, explanation and exact edit when conceding/applying. |
| `RulingApplication` | Supplied ruling ID, original claim ID/text and exact edit, including historical rulings absent from the latest review. |
| `Ruling` | Exact answer/review/issue/claim/snapshot scope, supported/narrow/remove/insufficient-evidence decision, rationale and evidence. Only narrowing has replacement wording; insufficient evidence identifies missing context. |

`write(context, answer_id)` creates an initial answer with no prior-review
responses. `revise(context, new_answer_id, previous, review, rulings=(),
closeout=False)` requires a new answer ID and a response to every non-editorial
issue. A response claiming to apply a ruling must have a supplied ruling for the
same scoped claim. Recorded edits and a well-formed answer do not certify that
the author correctly applied an edit: full review remains required.

Reviewer objections may target an exact title/body span with `claim_id=None` when
the author omitted that assertion from its inventory. Those independent objections
can be answered, adjudicated and removed through the same contracts; an incomplete
author inventory cannot prevent factual review.

Standing rulings can come from an earlier review/answer in the same snapshot.
Their original provenance is retained. Supported rulings require exact claim
identity; changed claims or snapshots cannot inherit approval. Binding narrow/remove
edits remain applicable while the exact ruled assertion survives in title/body,
even if its inventory entry changed or disappeared.
Narrow/remove revisions must record a `RulingApplication` even when the latest
review has no corresponding issue. Full review receives the supplied ruling
provenance and applied edits. Durable retrieval and applicability decisions remain
with the controller.

`review(context, review_id, answer, previous_review=...)` rechecks the full answer
against the same context and carries its issue responses. When responses exist,
the saved previous review is required and each response must match its exact
issue/claim. Any supplied prior review is scope-checked even when there are no
responses. No free-form explanation is silently promoted to checked evidence.

## Adjudication packet

`adjudicate(context, ruling_id, answer, review, issue_id, response, ...)` accepts
one factual/inference issue and its optional issue-specific author response.
It supplies both arguments, the targeted full claim (including inference premises
and reasoning, or the un-inventoried answer span), brief/snapshot identity and an independent packet
of exact saved passages, including passages neither party quoted.

By default all included passages are sent. `passage_ids` explicitly selects a
smaller packet; omitted IDs and a nonblank `omission_reason` are mandatory when
anything is omitted. There is no silent cap or truncation. Returned ruling
evidence must occur in that packet, not merely elsewhere in the snapshot.

`reviewer_checks` and `author_checks` must correspond exactly to the parties'
citations. Every check is `verified`, `not_found` or `unverified`, with explicit
reasons for unchecked/failed quotes. The client computes each quote status and
rejects conflicting caller-supplied statuses. Failures remain in the packet and
are denied evidentiary weight. Checks distinguish snapshot verification from
availability in the selected packet, including targeted claim and premise citations.

The controller may supply `response_review` for an earlier saved defense. Its
original review/issue IDs remain intact in the packet; its exact claim scope must
match the present dispute. The adjudicator prompt is versioned `query-adjudicator-v3`.

The adjudicator is a separate invocation with no shared conversation state. It
may use the reviewer's vendor/model; independence does not require a third vendor.
It cannot adjudicate a coverage gap into missing facts or certify sufficiency.

## Configuration, call bounds and failures

Construct `ReviewModels` from explicit author/reviewer `ModelSettings` objects,
and optionally separate adjudicator settings. Author and reviewer must use
different hosted vendors (`anthropic` and `openai`, in either order). Local
OpenAI-compatible endpoints do not establish vendor identity, so they cannot
satisfy this pair's separation rule. Existing local model smoke evals remain
available. With no adjudicator setting, it uses reviewer configuration.

`ReviewClients(models, limits=CallLimits(...))` builds fresh agents lazily. Only
`TestModel`/`FunctionModel` may be injected through the explicit offline overrides;
arbitrary prebuilt agents or real-model overrides cannot add tools or mislabel a
vendor. Agents have no function/native browsing or acquisition tools; structured
output is their only tool call. Prompts direct roles to use supplied evidence and
treat source text/party arguments as data rather than instructions.

Each invocation has one logical model request, zero output-validation retries,
a finite output-token bound and request timeout. `max_prompt_bytes` bounds the
rendered data plus instructions; oversized context fails explicitly without
truncation. This is a byte bound, not a tokenizer estimate, provider context-size
guarantee or investigation budget reservation. SDK transport retries may still
repeat HTTP attempts; no exactly-once billing guarantee is made.

`RoleCallError` records role, failure kind, retryability, optional status and known
usage. Rate limits, transient server/connection failures and timeouts are
retryable; account/quota/auth/configuration failures, invalid output, usage limits
and context limits are not automatically retried. Quota-specific structured codes
and anchored credit-limit messages override a generic HTTP 429. No sleep, retry
schedule or lifecycle transition is performed here. Unknown programming/run
errors propagate rather than being mislabeled transient provider failures.

Success returns `ModelCall[Answer|Review|Ruling]` with aggregate logical requests
and input/output token usage. Invalid returned scope or references preserve known
usage in the failure. Failed runs retain usage from captured model responses where
available; absence of a response is **unknown usage**, not a zero-cost call.

## Provenance and validation

Behavior is selectively adapted from aibyai
[`ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9`](https://github.com/grepory/aibyai/tree/ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9):
`src/clients/author.py`, `reviewer.py`, `adjudicator.py` and
`src/provider_errors.py`. Query owns the new schemas and prompts; it imports no
aibyai code/configuration/data. It replaces corpus-wide source labels with
snapshot/version/passage references and run-wide quote keys with exact scoped
claim identities. It narrows the source's broad retryable exception handling.

Offline tests use real Pydantic AI agents backed by TestModel/FunctionModel. They
exercise context equality, no external tools, explicit inference premises,
missing/limited evidence, issue defenses, standing rulings, packet omissions,
quote-check statuses, output validation, vendor separation and failure mapping.
Real model calls remain opt-in evals and never run in `mise run check` or CI.
