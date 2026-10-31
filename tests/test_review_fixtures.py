"""Validate the prepared histories; controller assertions belong to NOT-39/40.

These checks prove the corpus can be consumed without aibyai or a provider.
They do not claim that Query already implements the expected review actions.
"""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from tests.review_cases import Corpus, load_corpus

CORPUS = load_corpus()


def test_prepared_history_roundtrips_without_reference_repository(review_case):
    restored = type(review_case).model_validate_json(review_case.model_dump_json())
    assert restored == review_case
    assert all(owner.startswith("NOT-") for owner in review_case.owners)
    assert all(origin.path.startswith(("src/", "tests/")) for origin in review_case.origins)


def test_inventory_covers_each_inherited_behavior_and_query_boundary():
    assert {case.area for case in CORPUS.cases} == {
        "final_review",
        "responses",
        "disputes",
        "rulings",
        "editorial",
        "evidence",
        "closeout",
        "resume",
        "coverage",
    }
    by_id = {case.id: case for case in CORPUS.cases}
    assert {
        "upheld-still-final-review",
        "fresh-and-repeated-issues",
        "binding-narrow-reapplied",
        "binding-remove-reapplied",
        "new-snapshot-reopens-ruling",
        "changed-claim-reopens-ruling",
        "editorial-only-rejection",
        "signoff-with-factual-blocker",
        "wrong-source-real-quote",
        "excluded-source-real-quote",
        "closeout-survives-failure-grant",
        "resume-paid-draft",
        "resume-partial-round",
        "resume-between-adjudications",
        "resume-remote-outcome-unknown",
        "missing-required-question",
    } <= by_id.keys()


@pytest.mark.parametrize(
    ("case_id", "field", "value", "message"),
    [
        ("review-pending-revision", "snapshot", "missing", "Unknown current snapshot"),
        ("review-pending-revision", "expected.visible_draft", "d2", "saved review"),
        ("review-pending-revision", "reviews.0.draft", "missing", "saved draft"),
        ("review-pending-revision", "drafts.0.snapshot", "missing", "Unknown draft snapshot"),
        ("review-pending-revision", "drafts.0.claims.0.question", "missing", "required question"),
        ("review-pending-revision", "reviews.0.issues.0.claim", "missing", "reviewed draft"),
        ("issue-specific-defense", "responses.0.issue", "missing", "saved draft and issue"),
        ("issue-specific-defense", "responses.0.draft", "missing", "saved draft and issue"),
        ("issue-specific-defense", "responses.0.disposition", "unstructured", "Free-form"),
        ("upheld-same-scope", "rulings.0.claim", "missing", "disputed claim"),
        ("upheld-same-scope", "rulings.0.issue", "missing", "issue and snapshot"),
        ("upheld-same-scope", "rulings.0.decision", "narrow", "replacement text"),
        ("upheld-same-scope", "rulings.0.snapshot", "s2", "Ruling scope"),
        ("upheld-same-scope", "rulings.0.claim_text", "Aster operates 1700 MW.", "Ruling scope"),
        ("upheld-same-scope", "drafts.2.after_rulings", ["missing"], "existing prior ruling"),
        ("upheld-same-scope", "drafts.0.after_rulings", ["a1"], "follow the disputed draft"),
        ("resume-paid-draft", "operations.0.result", None, "saved result"),
        ("resume-paid-draft", "operations.1.result", "d1", "saved result"),
        ("resume-paid-draft", "operations.0.result", "missing", "saved record for its role"),
        ("resume-paid-draft", "operations.0.role", "reviewer", "saved record for its role"),
        ("resume-paid-draft", "operations.1.used_tokens", 10, "usage is unknown"),
        ("resume-between-adjudications", "operations.0.result", "r2", "saved record for its role"),
        (
            "wrong-source-real-quote",
            "reviews.0.issues.0.evidence.0.version",
            "missing",
            "known version",
        ),
        ("review-pending-revision", "budget.review_spent", -1, "greater than or equal"),
        ("review-pending-revision", "expected.actions", [], "at least 1"),
    ],
)
def test_malformed_histories_fail_at_load(case_id, field, value, message):
    data = json.loads(CORPUS.model_dump_json())
    case = next(case for case in data["cases"] if case["id"] == case_id)
    target = case
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = value
    with pytest.raises(ValidationError, match=message):
        Corpus.model_validate(data)


@pytest.mark.parametrize("collection", ["cases", "drafts", "reviews", "operations"])
def test_duplicate_identity_is_rejected(collection):
    data = json.loads(CORPUS.model_dump_json())
    if collection == "cases":
        rows = data["cases"]
    else:
        case = next(case for case in data["cases"] if case["id"] == "resume-paid-draft")
        if collection == "reviews":
            case = data["cases"][0]
        rows = case[collection]
    rows.append(deepcopy(rows[0]))
    with pytest.raises(ValidationError, match="Duplicate"):
        Corpus.model_validate(data)


def test_unknown_snapshot_membership_is_rejected():
    data = json.loads(CORPUS.model_dump_json())
    data["snapshots"]["s1"].append("missing")
    with pytest.raises(ValidationError, match="Unknown snapshot version"):
        Corpus.model_validate(data)


def test_contradictory_expectations_are_rejected():
    data = json.loads(CORPUS.model_dump_json())
    case = data["cases"][0]
    case["expected"]["forbidden"].append(case["expected"]["actions"][0])
    with pytest.raises(ValidationError, match="overlap"):
        Corpus.model_validate(data)


@pytest.mark.parametrize(
    "case_id", ["binding-narrow-reapplied", "binding-remove-reapplied", "upheld-same-scope"]
)
def test_binding_regressions_include_a_review_after_the_ruling(case_id):
    case = next(case for case in CORPUS.cases if case.id == case_id)
    ruling = case.rulings[0]
    later = [draft for draft in case.drafts if ruling.id in draft.after_rulings]
    assert later
    assert any(review.draft == later[-1].id and review.issues for review in case.reviews)
    assert case.expected.visible_draft == later[-1].id


def test_partial_adjudication_history_preserves_the_paid_result():
    case = next(case for case in CORPUS.cases if case.id == "resume-between-adjudications")
    committed, pending = case.operations
    assert committed.role == pending.role == "adjudicator"
    assert committed.state == "committed"
    assert committed.result == case.rulings[0].id
    assert pending.state == "planned" and pending.result is None
    assert "adjudicate:i2" in case.expected.forbidden
    assert case.expected.actions == ("adjudicate:i4",)
