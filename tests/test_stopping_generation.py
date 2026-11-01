"""Regressions for authored histories, lossless generation, and blind case identities."""

import copy
import json

import pytest
import yaml

from evals.generate_stopping import (
    GENERATED,
    UniqueKeyLoader,
    compile_corpus,
    load_source,
    main,
    render_json,
    resolve_cases,
    review_case,
)
from evals.stopping_cases import load_corpus
from query.evidence import text_identity


def test_generated_contracts_match_authored_yaml_and_are_reproducible():
    source = load_source()
    corpus = compile_corpus(source)
    assert render_json(corpus) == GENERATED.read_text()
    assert corpus == load_corpus() == compile_corpus(source)
    assert len(corpus.cases) == 20


def test_source_edit_rebuilds_exact_text_identity_and_snapshot_membership():
    source = load_source()
    old = compile_corpus(source).cases[0]
    data = source.model_dump(mode="json")
    text = "  Revised café source.\n\n" + "界" * 2100 + "\n"
    data["sources"]["announcement"]["text"] = text
    new = compile_corpus(type(source).model_validate(data)).cases[0]
    version = new.snapshot.versions[0]
    assert version.text == text
    assert version.content_hash == text_identity(text).content_hash
    assert version.byte_count == len(text.encode())
    assert "".join(version.passage_text(p) for p in version.passages) == text
    assert version.id != old.snapshot.versions[0].id
    assert new.snapshot.snapshot.id != old.snapshot.snapshot.id
    assert new.evaluation_id == old.evaluation_id
    assert new.questions[0].version_ids == (version.id,)
    assert new.attempts[0].version_ids == (version.id,)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_source",
        "unknown_field",
        "variant_chain",
        "no_gold",
        "duplicate_source",
        "generated_field",
        "empty_gold",
        "invalid_parent",
    ],
)
def test_bad_authored_cases_are_rejected(mutation):
    source = load_source()
    data = source.model_dump(mode="json")
    base = data["cases"]["unread-primary"]
    if mutation == "unknown_source":
        base["sources"] = ["not-a-source"]
    elif mutation == "unknown_field":
        base["expected"]["answr_integrity"] = True
    elif mutation == "variant_chain":
        data["cases"]["primary-read-and-settled"]["extends"] = "budget-explicit-grant"
    elif mutation == "duplicate_source":
        base["sources"] = ["announcement", "announcement"]
    elif mutation == "generated_field":
        base["snapshot"] = {}
    elif mutation == "empty_gold":
        base["expected"] = None
    elif mutation == "invalid_parent":
        base["extends"] = []
    else:
        del base["expected"]
    with pytest.raises(ValueError):
        compile_corpus(type(source).model_validate(data))


def test_duplicate_yaml_keys_cannot_silently_replace_a_case():
    with pytest.raises(ValueError, match="unique strings"):
        yaml.load("cases:\n  example: {}\n  example: {}", Loader=UniqueKeyLoader)


def test_variants_replace_lists_without_mutating_the_base():
    source = load_source()
    original = copy.deepcopy(source)
    cases = resolve_cases(source)
    assert cases["unread-primary"]["actions"] == ["read-ledger"]
    assert cases["primary-read-and-settled"]["actions"] != cases["unread-primary"]["actions"]
    assert source == original


def test_check_reports_stale_json_and_generate_repairs_it(tmp_path, monkeypatch, capsys):
    from evals import generate_stopping

    target = tmp_path / "corpus.json"
    target.write_text("{}")
    monkeypatch.setattr(generate_stopping, "GENERATED", target)
    assert main(["check"]) == 2
    assert "stale" in capsys.readouterr().err
    assert main(["generate"]) == main(["check"]) == 0
    assert target.read_text() == GENERATED.read_text()


def test_resolved_review_view_includes_actual_evidence_and_boundary_context(capsys):
    source = load_source()
    case = review_case(source, "expensive-action-does-not-fit")
    assert case["boundary_of"] == "expensive-action-fits"
    assert case["actions"][0]["estimated_tokens"] == 60
    assert case["checkpoint"]["budget"]["limits"]["research"] == 80
    assert case["sources"]["announcement"]["text"] == source.sources["announcement"].text
    assert "snapshot" not in case
    assert main(["review", "--case", "private-fact-exhausted"]) == 0
    assert yaml.safe_load(capsys.readouterr().out)[0]["id"] == "private-fact-exhausted"


@pytest.mark.parametrize(
    "base,variant",
    [
        ("private-fact-exhausted", "private-fact-user-source"),
        ("circular-ten-reprints", "circular-no-independent-avenue"),
        ("impossible-certainty", "certainty-new-material-lead"),
        ("repeated-evidence-exhausted", "repeated-evidence-new-method"),
        ("removed-hard-required-claim", "removed-claim-unavailable-evidence"),
    ],
)
def test_research_changes_do_not_relabel_the_unchanged_answer_integrity(base, variant):
    cases = {c.id: c for c in load_corpus().cases}
    before, after = cases[base], cases[variant]
    assert before.claims == after.claims
    assert before.snapshot.versions == after.snapshot.versions
    assert before.answer_ref == after.answer_ref
    assert before.last_reviewed_answer_ref == after.last_reviewed_answer_ref
    assert before.expected.answer_integrity is after.expected.answer_integrity is True


def test_exhaustion_has_named_attempts_and_explicit_scope():
    cases = {c.id: c for c in load_corpus().cases}
    for key in (
        "repeated-evidence-exhausted",
        "removed-claim-unavailable-evidence",
        "impossible-certainty",
    ):
        case = cases[key]
        unavailable = [a for a in case.attempts if a.result == "unavailable"]
        assert len({a.target for a in unavailable}) >= 2
        assert "operator-contact" in " ".join(a.target for a in unavailable)
        assert all("all named" not in a.observed_progress for a in unavailable)
        assert any("declared" in a or "prepared reasonable" in a for a in case.scope_assumptions)
    repeated = cases["repeated-evidence-exhausted"]
    assert sum(a.method == "search" for a in repeated.attempts) == 2
    assert len(repeated.attempts) > 3


def test_quantities_and_evidence_attempt_targets_are_unambiguous():
    for case in load_corpus().cases:
        versions = {v.id: v for v in case.snapshot.versions}
        for version in versions.values():
            assert "1200 MW planned" not in version.text
            if version.document_key == "announcement":
                assert "1200 MW total announced" in version.text
                assert "300 MW planned" in version.text
        for attempt in case.attempts:
            if attempt.result == "evidence":
                assert all(attempt.target == versions[v].origin_url for v in attempt.version_ids)


def test_model_inputs_have_opaque_unique_ids_and_no_case_names():
    corpus = load_corpus()
    inputs = [c.model_input() for c in corpus.cases]
    assert len({c["id"] for c in inputs}) == len(corpus.cases)
    serialized = json.dumps(inputs)
    for case, data in zip(corpus.cases, inputs, strict=True):
        assert data["id"] == str(case.evaluation_id)
        assert case.id not in serialized
        assert not {"expected", "rubric", "boundary_of", "boundary_change", "family"} & data.keys()


def test_pending_answer_cannot_be_marked_as_having_passed_integrity():
    corpus = load_corpus()
    data = corpus.model_dump(mode="json")
    case = next(c for c in data["cases"] if c["id"] == "budget-final-review-impossible")
    case["expected"]["answer_integrity"] = True
    with pytest.raises(ValueError, match="pending draft"):
        type(corpus).model_validate(data)
