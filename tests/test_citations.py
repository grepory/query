from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import ValidationError

from query.citations import CitationIndex, normalize
from query.evidence import (
    EvidenceInput,
    EvidenceSnapshot,
    LoadedSnapshot,
    Passage,
    PassageDisposition,
    SnapshotCandidate,
    SourceVersion,
    passage_ranges,
    text_identity,
)
from query.investigation import Brief, BriefVersion, Question
from query.review import Citation, ReviewContext


def uid(key):
    return uuid5(NAMESPACE_URL, "query-citation-tests/" + key)


def make_context(*texts, excluded=(), limited=False):
    versions, observations, candidates = [], [], []
    size = 0
    for number, text in enumerate(texts):
        identity = text_identity(text)
        version_id = uid(f"version/{number}/{identity.content_hash}")
        passages = tuple(
            Passage(id=uid(f"{version_id}/{start}"), version_id=version_id, start=start, end=end)
            for start, end in passage_ranges(text)
        )
        version = SourceVersion(
            id=version_id,
            source_id=uid(f"source/{number}"),
            document_key=f"doc-{number}",
            title=f"Saved text {number}",
            origin_url=None,
            text=text,
            passages=passages,
            **identity.model_dump(),
        )
        versions.append(version)
        observation = EvidenceInput(
            investigation_id=uid("investigation"),
            operation_key=f"supply-{number}",
            brief_version=1,
            revision=number + 2,
            document_key=version.document_key,
            title=version.title,
            origin_url=None,
            observed_at=datetime(2026, 11, 1, tzinfo=UTC),
            published_at=None,
            max_bytes=1024 * 1024,
            status="accepted",
            version_id=version.id,
            reason=None,
            content_hash=identity.content_hash,
            byte_count=identity.byte_count,
            codepoint_count=identity.codepoint_count,
        )
        observations.append(observation)
        dispositions = []
        for position, passage in enumerate(passages):
            included = not limited and (number, position) not in excluded
            if included:
                size += len(version.passage_text(passage).encode())
            dispositions.append(
                PassageDisposition(
                    passage_id=passage.id,
                    start=passage.start,
                    end=passage.end,
                    included=included,
                    reason=None if included else "Explicit exclusion",
                )
            )
        candidates.append(
            SnapshotCandidate(
                version_id=version.id,
                observation_keys=(observation.operation_key,),
                passages=tuple(dispositions),
            )
        )
    snapshot = EvidenceSnapshot(
        id=uid("snapshot"),
        investigation_id=uid("investigation"),
        operation_key="freeze-1",
        brief_version=1,
        revision=100,
        status="limited" if limited else "ready",
        reason="empty_selection" if limited else None,
        candidates=tuple(candidates),
        rejected_inputs=(),
        requested_bytes=size,
        included_bytes=size,
        max_bytes=256 * 1024,
    )
    return ReviewContext(
        brief=BriefVersion(
            version=1,
            brief=Brief(
                question="What does the supplied text establish?",
                scope="Supplied text only",
                cutoff=date(2026, 11, 1),
                questions=(
                    Question(
                        key="fact",
                        text="What is supported?",
                        acceptance_criteria="Exact saved evidence",
                    ),
                ),
            ),
        ),
        evidence=LoadedSnapshot(
            snapshot=snapshot, versions=tuple(versions), observations=tuple(observations)
        ),
    )


def citation(context, quote, version=0, passage=0):
    source = context.evidence.versions[version]
    return Citation(version_id=source.id, passage_id=source.passages[passage].id, excerpt=quote)


def test_normalization_resolves_original_saved_text_despite_length_changes():
    original = "The “ﬂow” is ５ ㎿.\r\nReady.  "
    context = make_context(original)
    resolved = CitationIndex(context).resolve(citation(context, 'THE "flow" IS 5 MW. Ready.'))
    assert resolved.check.status == "verified" and resolved.match_kind == "contiguous"
    assert resolved.passage.text == original
    assert (resolved.passage.start, resolved.passage.end) == (0, len(original))
    assert resolved.snapshot_id == context.evidence.snapshot.id
    assert normalize(original) == 'the "flow" is 5 mw. ready.'


def test_quote_must_occur_in_the_named_version_and_passage():
    context = make_context("Aster operates 900 MW.", "Boreal operates 200 MW.")
    index = CitationIndex(context)
    assert index.check(citation(context, "Aster operates 900 MW.")).status == "verified"
    assert index.check(citation(context, "Aster operates 900 MW.", version=1)).status == "not_found"
    wrong_pair = citation(context, "Aster operates 900 MW.").model_copy(
        update={
            "version_id": context.evidence.versions[1].id,
        }
    )
    assert index.check(wrong_pair).reason == "passage_not_in_named_version"
    assert (
        index.check(wrong_pair.model_copy(update={"version_id": UUID(int=999)})).reason
        == "unknown_source_version"
    )
    assert (
        index.check(wrong_pair.model_copy(update={"passage_id": UUID(int=998)})).status
        == "not_found"
    )


@pytest.mark.parametrize(
    "text,quote",
    [
        ("The plan is unapproved.", "approved"),
        ("The value is 15 MW.", "5 MW"),
        ("The value is 1.5 MW.", "5 MW"),
        ("The value is 1.5 MW.", ".5 MW"),
        ("The value is 900.5 MW.", "900."),
        ("The value is 1,500 MW.", ",500 MW"),
        ("The change is −12 MW.", "12 MW"),
        ("The value is 1/2 MW.", "2 MW"),
        ("The fraction is 5%.", "5"),
        ("The amount is $15.", "15"),
        ("Change -$15 million.", "$15 million"),
        ("Change −€15 million.", "€15 million"),
        ("Change $-15 million.", "-15 million"),
        ("Change €−15 million.", "−15 million"),
        ("The value is 900.5 MW.", "900"),
        ("The value is 1,500 MW.", "1"),
        ("The change was -12 MW.", "12 MW"),
        ("The value is 5.0 MW.", "5"),
        ("The project is co-owned.", "coowned"),
        ("The project was not approved.", "The project was approved."),
        ("The project was approved.", '"The project was approved."'),
    ],
)
def test_normalization_does_not_invent_words_numbers_punctuation_or_joins(text, quote):
    context = make_context(text)
    assert CitationIndex(context).resolve(citation(context, quote)).passage is None


@pytest.mark.parametrize(
    "quote",
    [
        "Alpha approved ... Beta operates 5 MW.",
        "Alpha approved […] Beta operates 5 MW.",
        "Alpha approved [...] Beta operates 5 MW.",
    ],
)
def test_elision_is_explicit_ordered_and_resolves_the_whole_saved_passage(quote):
    text = "Alpha approved the plan in 2026. Beta operates 5 MW."
    context = make_context(text)
    resolved = CitationIndex(context).resolve(citation(context, quote))
    assert resolved.check.status == "verified" and resolved.match_kind == "ordered_elision"
    assert resolved.passage.text == text  # The intervening material remains inspectable.


@pytest.mark.parametrize(
    "quote",
    [
        "Beta operates 5 MW. ... Alpha approved",  # Reordered source fragments.
        "Alpha approved ... Alpha approved",  # One occurrence cannot support two fragments.
        "Alpha approved Beta operates 5 MW.",  # No marked omission.
        "Alpha approved ...",
        "... Beta operates 5 MW.",
        "...",  # Empty fragments.
        "pha approved ... Beta operates 5 MW.",  # Partial word at a fragment boundary.
    ],
)
def test_fabricated_or_ambiguous_elisions_fail(quote):
    context = make_context("Alpha approved the plan in 2026. Beta operates 5 MW.")
    assert CitationIndex(context).check(citation(context, quote)).status == "not_found"


def test_never_joins_versions_or_adjacent_passages_even_when_both_are_included():
    context = make_context("Alpha approved.", "Beta operates.")
    assert (
        CitationIndex(context).check(citation(context, "Alpha approved. ... Beta operates.")).status
        == "not_found"
    )
    context = make_context("Alpha" + " " * 1995 + "Beta")
    for passage in (0, 1):
        assert (
            CitationIndex(context)
            .check(citation(context, "Alpha ... Beta", passage=passage))
            .status
            == "not_found"
        )


@pytest.mark.parametrize(
    "text,quote,passage,verified",
    [
        (" " * 1999 + "approved", "pproved", 1, False),
        (" " * 1999 + "approved", "a", 0, False),
        (" " * 1998 + "1.5 MW", "5 MW", 1, False),
        ("x" * 2000 + " approved", "approved", 1, True),
        (" " * 2000 + "approved", "approved", 1, True),
    ],
)
def test_token_boundaries_survive_source_partition_edges(text, quote, passage, verified):
    context = make_context(text)
    assert (
        CitationIndex(context).check(citation(context, quote, passage=passage)).status == "verified"
    ) is verified


def test_excluded_and_limited_sources_cannot_supply_evidence():
    context = make_context("Included evidence.", "Excluded evidence.", excluded={(1, 0)})
    check = CitationIndex(context).check(citation(context, "Excluded evidence.", version=1))
    assert check.status == "not_found" and check.reason == "passage_excluded"
    context = make_context("Saved but not selected.", limited=True)
    assert (
        CitationIndex(context).check(citation(context, "Saved but not selected.")).reason
        == "snapshot_limited"
    )


def test_duplicate_passage_ids_are_rejected_instead_of_overwriting_the_index():
    context = make_context("Source one.", "Source two.")
    data = context.model_dump(mode="json")
    duplicate = data["evidence"]["versions"][0]["passages"][0]["id"]
    data["evidence"]["versions"][1]["passages"][0]["id"] = duplicate
    data["evidence"]["snapshot"]["candidates"][1]["passages"][0]["passage_id"] = duplicate
    with pytest.raises(ValidationError, match="Duplicate saved passage ID"):
        ReviewContext.model_validate(data)


def test_generated_text_is_not_an_index_source():
    context = make_context("Only this saved sentence is evidence.")
    generated_summary = "The facility secretly has 500 MW."
    assert CitationIndex(context).check(citation(context, generated_summary)).status == "not_found"


@pytest.mark.parametrize("mutate", ["text", "range"])
def test_altered_saved_text_or_passage_ranges_fail_before_verification(mutate):
    context = make_context("Exact immutable text.")
    data = context.model_dump(mode="json")
    if mutate == "text":
        data["evidence"]["versions"][0]["text"] = "Altered immutable text."
    else:
        data["evidence"]["snapshot"]["candidates"][0]["passages"][0]["end"] -= 1
    with pytest.raises(ValidationError, match="identity|saved version"):
        ReviewContext.model_validate(data)


@pytest.mark.parametrize(
    "number,fragment", [("1.5", ".5"), ("900.5", "900."), ("1,500", ",500"), ("−12", "12")]
)
def test_elision_fragments_cannot_bisect_numbers(number, fragment):
    context = make_context(f"Value {number} MW. A later independent sentence.")
    quote = f"{fragment} ... A later independent sentence."
    assert CitationIndex(context).check(citation(context, quote)).status == "not_found"


@pytest.mark.parametrize(
    "prefix,value,quote,passage",
    [
        (" " * 1999, "1.5 MW.", ".5 MW.", 1),
        (" " * 1996, "900.5 MW.", "900.", 0),
        (" " * 1999, "1,500 MW.", ",500 MW.", 1),
        (" " * 1999, "−12 MW.", "12 MW.", 1),
        (" " * 1999, "1e+5 MW.", "1", 0),
        (" " * 1999, "-$15 million.", "$15 million.", 1),
        (" " * 1999, "−€15 million.", "€15 million.", 1),
    ],
)
def test_numeric_punctuation_and_signs_survive_partition_boundaries(prefix, value, quote, passage):
    context = make_context(prefix + value)
    assert (
        CitationIndex(context).check(citation(context, quote, passage=passage)).status
        == "not_found"
    )


@pytest.mark.parametrize(
    "number",
    ["1.5", ".5", "900.5", "1,500", "−12", "1/2", "5%", "$15", "-$15", "−€15", "$-15", "€−15"],
)
def test_whole_numeric_tokens_still_match(number):
    context = make_context(f"Value {number} MW.")
    assert CitationIndex(context).check(citation(context, number)).status == "verified"
