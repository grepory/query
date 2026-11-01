"""Fixture integrity and existing lifecycle guards, not a stopping-policy router."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from evals.stopping_cases import StoppingCorpus, load_corpus
from query.investigation import CompletionChecks, Lane, Outcome, Phase, transition

CORPUS = load_corpus()


@pytest.mark.parametrize("case", CORPUS.cases, ids=lambda case: case.id)
def test_prepared_transition_uses_real_lifecycle_guards(case):
    expected = case.expected
    checks = (
        CompletionChecks(
            brief_version=case.checkpoint.brief_version,
            answer_ref="prepared-reviewed-answer",
            answer_integrity=expected.answer_integrity,
            research_sufficiency=expected.research_sufficiency,
            rationale=expected.rationale,
        )
        if expected.target == Phase.COMPLETE
        else None
    )
    result = transition(
        case.checkpoint,
        expected.target,
        reason=expected.reason,
        completion=checks,
        outcome=expected.outcome,
    )
    assert result.phase == expected.target
    assert result.budget == case.checkpoint.budget
    assert result.brief_version == case.checkpoint.brief_version
    assert result.outcome == expected.outcome
    if expected.decision == "stop_at_limit":
        assert result.completion is None and result.outcome is None
        assert result.resume_phase == Phase.PLANNING
    if expected.decision == "park":
        assert result.completion is None and result.resume_phase == case.checkpoint.phase


@pytest.mark.parametrize("case", CORPUS.cases, ids=lambda case: case.id)
@pytest.mark.parametrize("integrity,sufficiency", [(False, True), (True, False), (False, False)])
def test_one_check_cannot_complete_prepared_history(case, integrity, sufficiency):
    current = case.checkpoint.model_copy(update={"phase": Phase.REVIEWING})
    checks = CompletionChecks(
        brief_version=current.brief_version,
        answer_ref="fixture-answer",
        answer_integrity=integrity,
        research_sufficiency=sufficiency,
        rationale="Explicit negative lifecycle test, not a model judgment",
    )
    with pytest.raises(ValidationError, match="Both completion checks"):
        transition(current, Phase.COMPLETE, completion=checks, outcome=Outcome.QUALIFIED)


def test_all_nine_families_have_decision_changing_boundaries():
    families = {
        "unread_primary",
        "private_fact",
        "circular_reporting",
        "scope_date_conflict",
        "impossible_certainty",
        "expensive_action",
        "exhausted_budget",
        "repeated_evidence",
        "removed_claim",
    }
    assert {c.family for c in CORPUS.cases if c.boundary_of is None} == families
    assert {c.family for c in CORPUS.cases if c.boundary_of is not None} == families
    assert len([c for c in CORPUS.cases if c.boundary_of is None]) == 9


def test_ten_outlets_are_explicitly_one_origin():
    case = next(c for c in CORPUS.cases if c.id == "circular-ten-reprints")
    assert len(case.snapshot.versions) == 10
    assert len(set(case.reporting_origins.values())) == 1
    assert len(case.attempts) == 10
    assert case.expected.decision == "research"


def test_required_claim_removal_retains_question_and_action():
    case = next(c for c in CORPUS.cases if c.id == "removed-hard-required-claim")
    assert not case.claims and case.removed_claims
    assert case.brief.questions[0].required
    assert case.expected.retained_question_ids == ("capacity",)
    assert case.expected.selected_action_ids == ("read-ledger",)


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("snapshot.snapshot.brief_version", 2, "current brief"),
        ("snapshot.snapshot.included_bytes", 1, "byte accounting"),
        ("snapshot.observations.0.content_hash", "wrong", "Observation scope"),
        ("actions.0.question_ids", ["missing"], "unknown question"),
        ("attempts.0.version_ids", [str(uuid4())], "unknown source"),
        ("questions.0.attempt_ids", ["missing"], "unknown attempt|attribution"),
        ("expected.selected_action_ids", ["missing"], "unknown action"),
        ("expected.retained_question_ids", [], "at least 1"),
        ("checkpoint.budget.research_spent", -1, "greater than or equal"),
        ("expected.outcome", "answered", "Only stop"),
        ("boundary_of", "missing", "parent and changed fact"),
    ],
)
def test_malformed_histories_are_rejected(field, value, message):
    data = json.loads(CORPUS.model_dump_json())
    target = data["cases"][0]
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    target[parts[-1]] = value
    with pytest.raises(ValidationError, match=message):
        StoppingCorpus.model_validate(data)


def test_boundary_must_change_the_decision():
    data = json.loads(CORPUS.model_dump_json())
    data["cases"][1]["expected"] = data["cases"][0]["expected"]
    with pytest.raises(ValidationError, match="Boundary must change"):
        StoppingCorpus.model_validate(data)


def test_model_inputs_exclude_gold_and_rubric():
    for case in CORPUS.cases:
        model_input = case.model_input()
        assert not {"expected", "rubric", "boundary_of", "boundary_change"} & model_input.keys()
        assert model_input["snapshot"]["snapshot"]["id"] == str(case.snapshot.snapshot.id)


def test_boundary_claims_and_ledger_attempts_match_their_histories():
    cases = {case.id: case for case in CORPUS.cases}
    settled = cases["primary-read-and-settled"]
    assert "incomplete" not in settled.claims[0].text
    ambiguous = cases["scope-definition-ambiguous"]
    assert "900 MW" in ambiguous.claims[0].text and "1200 MW" in ambiguous.claims[0].text
    assert "choose" in ambiguous.brief.questions[0].acceptance_criteria
    assert "1200 MW" in " ".join(v.text for v in ambiguous.snapshot.versions)
    for case_id in ("primary-read-and-settled", "removed-optional-claim"):
        case = cases[case_id]
        ledger = next(a for a in case.attempts if a.id == "read-ledger")
        version = next(v for v in case.snapshot.versions if v.id == ledger.version_ids[0])
        assert ledger.target == version.origin_url


def test_blocked_review_is_recorded_separately_from_blocked_research():
    case = next(c for c in CORPUS.cases if c.id == "budget-final-review-impossible")
    review = next(a for a in case.actions if a.kind == "review")
    assert review.lane == Lane.REVIEW
    assert review.estimated_tokens > case.checkpoint.budget.remaining(review.lane)
    assert review.id in case.expected.preserved_action_ids
    assert "read-ledger" in case.expected.preserved_action_ids
    assert case.answer_ref != case.last_reviewed_answer_ref


def test_research_gold_actions_fit_their_recorded_lane_allowance():
    for case in CORPUS.cases:
        if case.expected.decision == "research":
            selected = [a for a in case.actions if a.id in case.expected.selected_action_ids]
            assert all(a.lane == Lane.RESEARCH and a.availability == "available" for a in selected)
            assert sum(a.estimated_tokens for a in selected) <= case.checkpoint.budget.remaining(
                Lane.RESEARCH
            )


def test_wrong_question_attempt_attribution_is_rejected():
    data = json.loads(CORPUS.model_dump_json())
    optional = next(c for c in data["cases"] if c["id"] == "removed-optional-claim")
    optional["attempts"][0]["question_ids"] = ["capacity"]
    with pytest.raises(ValidationError, match="attributed to that question"):
        StoppingCorpus.model_validate(data)
