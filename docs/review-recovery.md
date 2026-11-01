# Review closeout, usage and recovery (NOT-40)

The answer controller executes one action per explicit advance. PostgreSQL session
ownership spans intent, remote I/O and result commit. Append-only investigation
events record review policy, closeout, and each attempt's started and terminal
states. Typed attempts are also exposed in `export.review_attempts`; successful
role outputs remain in their original immutable step records. No new database
tables or migration are needed.

## Ownership and fault boundaries

A dedicated database connection holds a per-investigation advisory lock. Another
thread or process cannot advance that investigation while the lock is held.
Before calling a provider, code rechecks the original connection/backend PID,
held advisory lock, pending attempt and reviewing phase. A lost connection cannot
silently reconnect as the old owner. Due-time and revision checks also precede
work. Holds or cancellation before authorization prevent paid I/O.

A live provider call cannot be undone by a later cancellation or lost database
connection. A valid owner may commit its result and usage while preserving a
concurrent hold or cancellation. A lost owner cannot commit as though it still
owned the run. This fences further local work, not remote execution already in
flight. PostgreSQL session loss or remote success followed by local failure can
leave billing and outcome uncertain.

Each boundary has a different recovery path:

| Last durable record | Next explicit advance |
| --- | --- |
| Planned intent, no started attempt | Execute that same operation and output ID after admission checks. |
| Started attempt without terminal record | Record `unknown`, conservatively charge its reserved token bound and park. Perform no provider call. This reconciliation is permitted on held/canceled runs and preserves their phase. |
| Failed transient attempt | Remain paused until an explicit due-time resume, then retry the same intent. |
| Failed permanent attempt | Remain parked until an explicit operator resume. |
| Committed successful result | Continue at the next unsaved action; never replay that result. |

After inspecting an uncertain outcome, explicit resume permits another attempt
on the same intent. That may repeat a remotely successful call. Exactly-once
remote execution or billing is not promised. Unknown accounting is conservative,
not an assertion that the provider actually consumed its full reservation.

Programming exceptions propagate and retain the started attempt for later
reconciliation. Provider errors use the role client's classification. A local
context rejection occurs before remote I/O and records `not_called` with zero
usage. SDK transport retries are disabled; there are no hidden retries inside a
durable attempt.

## Pauses and retry time

Transient errors pause immediately. Their persisted `retry_at` uses the database
clock and exponential delay (default 60 seconds, capped at 3600). Each retry
requires explicit `resume`, then `advance`; no scheduler or background wake-up is
installed. Resume before the due time fails, and stale revisions cannot call a
provider. Permanent failures park without an automatic retry.

Successful and failed attempts with reported usage charge actual input plus
output tokens exactly once. Unknown remote usage charges the planned call bound
and retains `usage=null`, `charge_basis=reservation`. Pending attempts are not
reported as zero-cost successful calls. The checkpoint budget is the cumulative
charge; step and attempt metadata describe the same successful call and must not
be added together a second time.

## Admission, reserve and closeout

`max_prompt_bytes` checks instructions, rendered payload, twice the output JSON
schema bytes, and an 8192-byte provider framing allowance. This is an explicit
conservative admission estimate, not a promise about proprietary tokenization or
billed provider envelopes. The reserved token bound is this byte cap plus maximum
output tokens. Reported overruns are always charged, even beyond admission or
lifetime allowances; they never authorize further overspend.

Before paying an author, admission protects the call plus a full reviewer call.
Before each adjudication it protects the adjudicator, a binding revision and a
full final review. The binding revision estimate never falls below the latest
paid author usage; free carry-forward drafts do not hide it. Admission is checked
again when starting an attempt. Each adjudication commits independently and the
next one rechecks remaining allowance.

Closeout is a separate committed event for the snapshot, saved before the next
paid intent. It starts when remaining tokens reach the configured threshold
(default three bounded calls). Failure, restart, grants and resume cannot undo
it. Fresh factual/inference disputes then reach adjudication without requiring
another author defense. Revisions receive `closeout=true` and may apply binding
edits or qualify the answer; full review still assesses semantic compliance.
Editorial feedback never creates closeout work. Insufficient evidence and
coverage decisions remain explicit; closeout cannot manufacture sufficiency.

If a call and its protected reserve cannot fit, park with reason
`review_reserve` before calling. Preserve the last reviewed draft and any paid,
unreviewed revision. A grant/resume can continue from that revision's review.
Actual usage exhaustion produces a limit stop; review operations retain a
reviewing resume target. Neither hold is a research-sufficiency result.

## Lifetime ceiling and configuration

`create --lifetime-tokens N` sets an immutable lifetime token ceiling. Without
it, the initial `limits.total` is the ceiling. Initial total must fit within the
ceiling. Existing investigations without a creation ceiling are grandfathered at
their current total allowance. Grants cannot reduce lane allowances or reset
spend; requested total increases are clamped to the ceiling and recorded with
both requested and effective values. Increasing a lane cannot bypass the total
ceiling. NOT-48 owns full cross-provider dollar/deadline budgets.

Use `advance --answer-models models.json --review-policy policy.json` to supply
an optional policy on the first action for a snapshot. Later actions use the
saved policy; conflicting policy changes are rejected. Model identities remain
pinned as described in [answer-loop.md](answer-loop.md). For example:

```json
{
  "closeout_remaining_tokens": 1600000,
  "retry_delay_seconds": 60,
  "max_retry_delay_seconds": 3600,
  "prices": {}
}
```

Optional `prices` maps `author`, `reviewer` and/or `adjudicator` to explicit
`input_usd_per_million`, `output_usd_per_million` decimal strings and an
`assumption` describing the caller-supplied rates. Each attempt retains its rate
assumptions. Reported usage yields an estimated USD cost; missing rates or unknown
usage yield `estimated_usd=null`. Rates are not fetched or assumed current, and
this estimate is not a dollar ceiling or a provider invoice.

## Regression provenance

`tests/integration/test_review_recovery.py` materializes NOT-36 histories for
closeout, hard limits, unaffordable final review, retries and partial adjudication.
It adds independent-thread/process ownership races, actual lock-backend loss,
cancel/hold during I/O, and faults before attempt start, before result commit and
after commit. A persisted paid-revision/free-draft history checks reserve handling.
All clients are fake or offline Pydantic AI test models. See the pinned aibyai
provenance and deliberate changes in [review-invariants.md](review-invariants.md).
