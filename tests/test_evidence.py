from datetime import UTC, datetime
from uuid import uuid4

import pytest

from query.evidence import (
    CandidateSelection,
    FreezeSnapshot,
    IngestText,
    Passage,
    SourceVersion,
    fingerprint,
    ingest_identity,
    passage_ranges,
    select_passages,
    text_identity,
)


def supplied(text="  é\r\n🦊e\u0301  ", **kwargs):
    return IngestText(
        operation_key="import-1",
        document_key="document-1",
        title="Document 1",
        observed_at=datetime(2026, 10, 21, tzinfo=UTC),
        text=text,
        **kwargs,
    )


def version(text):
    identifier = uuid4()
    return SourceVersion(
        id=identifier,
        source_id=uuid4(),
        document_key="doc",
        title="Doc",
        origin_url=None,
        text=text,
        **text_identity(text).model_dump(),
        passages=tuple(
            Passage(id=uuid4(), version_id=identifier, start=a, end=b)
            for a, b in passage_ranges(text)
        ),
    )


def test_exact_text_identity_and_unicode_offsets():
    request = supplied()
    assert request.text == "  é\r\n🦊e\u0301  "
    assert request.text.encode("utf-8") != request.text.strip().encode("utf-8")
    assert text_identity("é").content_hash != text_identity("e\u0301").content_hash
    saved = version("🦊" * 2001)
    assert [(p.start, p.end) for p in saved.passages] == [(0, 2000), (2000, 2001)]
    assert [len(saved.passage_text(p)) for p in saved.passages] == [2000, 1]
    assert saved.byte_count == 8004
    assert passage_ranges("") == ()
    assert passage_ranges("a" * 2000) == ((0, 2000),)
    with pytest.raises(ValueError):
        saved.passage_text(version("other").passages[0])


@pytest.mark.parametrize("text", ["a\x00b", "\ud800", "\udfff"])
def test_unsupported_text_rejected(text):
    with pytest.raises(ValueError):
        supplied(text)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_bytes", 0),
        ("max_bytes", True),
        ("max_bytes", -1),
        ("title", "  "),
        ("document_key", "\x00"),
        ("origin_url", "\ud800"),
        ("observed_at", datetime(2026, 10, 21)),
    ],
)
def test_invalid_metadata(field, value):
    fields = supplied().model_dump()
    fields[field] = value
    with pytest.raises(ValueError):
        IngestText.model_validate(fields)


def test_fingerprint_is_deterministic_and_includes_limits_and_exact_identity():
    request = supplied()
    fields = ingest_identity(request)
    assert "text" not in fields
    assert fingerprint(fields) == fingerprint(dict(reversed(list(fields.items()))))
    assert fingerprint(fields) != fingerprint(ingest_identity(supplied(max_bytes=1)))
    assert fingerprint(fields) != fingerprint(ingest_identity(supplied(request.text.strip())))
    assert ingest_identity(supplied(""))["byte_count"] == 0


def test_selections_validate_identity_omissions_and_preserve_partition_order():
    saved = version("a" * 4001)
    selection = CandidateSelection(version_id=saved.id)
    assert all(p.included and p.reason is None for p in select_passages(selection, saved))
    partial = CandidateSelection(
        version_id=saved.id,
        passage_ids=(saved.passages[2].id, saved.passages[0].id),
        exclusion_reason="outside scope",
    )
    dispositions = select_passages(partial, saved)
    assert [p.included for p in dispositions] == [True, False, True]
    assert dispositions[1].reason == "outside scope"
    with pytest.raises(ValueError, match="requires an exclusion"):
        select_passages(CandidateSelection(version_id=saved.id, passage_ids=()), saved)
    with pytest.raises(ValueError, match="foreign"):
        select_passages(CandidateSelection(version_id=saved.id, passage_ids=(uuid4(),)), saved)
    with pytest.raises(ValueError, match="different version"):
        select_passages(CandidateSelection(version_id=uuid4()), saved)
    with pytest.raises(ValueError, match="Duplicate passage"):
        CandidateSelection(version_id=saved.id, passage_ids=(saved.passages[0].id,) * 2)
    with pytest.raises(ValueError, match="Duplicate candidate"):
        FreezeSnapshot(operation_key="freeze", candidates=(selection, selection))


def test_corrupted_saved_identity_and_ranges_fail_validation():
    saved = version("abc")
    data = saved.model_dump()
    data["text"] = "xyz"
    with pytest.raises(ValueError, match="identity"):
        SourceVersion.model_validate(data)
    data = saved.model_dump()
    data["passages"][0]["end"] = 4
    with pytest.raises(ValueError, match="partition"):
        SourceVersion.model_validate(data)


@pytest.mark.parametrize("text", [b"abc", bytearray(b"abc"), "é".encode(), b"\xff"])
def test_supplied_text_never_decodes_byte_input(text):
    with pytest.raises(ValueError):
        supplied(text)
