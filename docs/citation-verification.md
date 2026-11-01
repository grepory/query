# Source-bound citation verification (NOT-38)

`query.citations.CitationIndex` checks quote occurrence against a frozen
`ReviewContext`. It reads only immutable saved source text: answer prose,
generated summaries, model premises and party arguments never become index
sources. No database or provider call is needed.

```python
from query.citations import CitationIndex

resolution = CitationIndex(context).resolve(citation)
if resolution.check.status == "verified":
    print(resolution.passage.text)  # Original saved text, not normalized text.
else:
    print(resolution.check.reason)
```

## Identity and resolution

Every lookup uses the exact `(version_id, passage_id)` pair. A quote elsewhere
in the corpus cannot rescue a wrong source or passage. Unknown versions,
foreign/missing passage IDs, exclusions and limited snapshots yield `not_found`
with an explicit reason. Altered text/hash or passage ranges fail context
construction. Duplicate passage UUIDs are rejected globally within a context,
preventing ambiguous packet selection or overwritten index entries.

`CitationResolution` records snapshot ID, brief version, checked citation, match
kind and original `SavedPassage`. Resolution returns the **entire exact saved
passage**, its IDs and end-exclusive Unicode code-point offsets. It never claims
normalized character offsets are original offsets: NFKC expansion and whitespace
collapse can change lengths. Failed matches have no resolved passage. `check()`
and `check_all()` return the existing `EvidenceCheck` status/reason contracts.

## Normalization and elision

Comparison normalizes NFKC compatibility forms, typographic single/double quotes,
lowercase and whitespace runs. Other punctuation remains. Quotes are not stripped
and words are not concatenated. Matches respect word/number boundaries: `approved`
does not occur in `unapproved`, `5` does not occur in `15` or `1.5`, and deleting
`not` does not create a contiguous quote. Both ends of a fragment must preserve
the whole adjacent numeric token, including separators, signs, exponents and
tightly attached currency/percent markers. Partition edges do not make the remainder
of a word or decimal into a fresh token.

Contiguous normalized occurrence is tried first, including literal source ellipses.
Otherwise `...`, `…`, `[...]` and `[…]` explicitly mark elision. Every nonempty
fragment must occur in order, without overlap, in the **same named included
passage**. Leading/trailing/empty elisions, reversed/reused occurrences, unmarked
deletions, cross-source joins and cross-passage joins fail. Even adjacent selected
passages of one version are not concatenated; use separate citations instead.

An elided match is marked `ordered_elision` and resolves to the whole saved passage
so omitted material remains inspectable. Occurrence does not establish that an
omission preserves meaning, that a claim follows, that a source is true, or that
research is sufficient. Semantic support and inference validity remain review
judgments; this verifier never creates an answer-integrity attestation.

## Role calls and disputed exhibits

Ordinary answer, revision, review and ruling validation now require mechanically
verified citations, including individual inference premises and issue responses.
Invalid returned citations produce the existing nonretryable `invalid_output`
failure with known usage retained. Structural-only NOT-37 placeholder excerpts
are not grandfathered into evidence.

Adjudication has an explicit separate structural-validation path that preserves
failed party exhibits for discussion. They do not become accepted citations.
Answer/review scope, exact claims, question coverage and response identity still
apply. Each submitted quote is independently checked: unknown source IDs and
fabricated excerpts are marked `not_found` and denied evidentiary weight. Returned
ruling citations must pass normal verification and occur in the source packet.

The changed adjudicator instructions use prompt version `query-adjudicator-v2`.
Packets include checks for targeted claim citations, each inference premise,
reviewer evidence and author response evidence. Optional caller checks must match
the exact citations and computed statuses; forged `verified` flags cannot override
failure. The verifier's actual reasons replace caller-written reasons.

Every checked exhibit has `passage_in_packet` and `quote_available` flags. A quote
can be verified against the snapshot yet unavailable to this invocation because
its passage was omitted. A failed quote is never available even when its passage
is present. The prompt limits source support to the supplied source packet.

All included passages are sent by default. Smaller explicit `passage_ids`
selections retain omitted IDs and require a nonblank `omission_reason`. There is
no silent truncation or corpus-wide fallback. Oversized complete requests still
fail the prompt-byte bound without calling a model; callers can prepare disclosed
smaller packets. Durable research decisions and orchestration remain in later tickets.

## Provenance and validation

Normalization is selectively adapted from aibyai
[`ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9:src/services/evidence.py`](https://github.com/grepory/aibyai/blob/ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9/src/services/evidence.py).
Query replaces source-label-independent, corpus-wide and unordered-fragment
matching with exact immutable references, ordered matching and saved-text
resolution. It does not port summary/company blocks or silent four-block caps.

Offline tests cover wrong source/passage pairs, changed/excluded data, Unicode
length changes, saved text, token/partition boundaries, elisions, fabricated joins,
duplicate IDs, strict role outputs, failed party exhibits, forged status flags,
checked premises and verified-but-omitted evidence. No live model/search calls run.
