"""Synthetic reviewer records exercise scoring mechanics, not real human approval."""

import json

import pytest
from pydantic import ValidationError

from evals.score_stopping import (
    HumanJudgment,
    Proposal,
    ReviewBundle,
    RubricApproval,
    digest,
    main,
    score,
)
from evals.stopping_cases import load_corpus

CORPUS = load_corpus()


def bundle(cases=CORPUS.cases):
    proposals = []
    judgments = []
    for case in cases:
        expected = case.expected
        proposal = Proposal(
            case_id=case.id,
            model="fake-test-model",
            prompt_version="test-v1",
            brief_version=case.checkpoint.brief_version,
            snapshot_id=case.snapshot.snapshot.id,
            decision=expected.decision,
            target=expected.target,
            outcome=expected.outcome,
            limit_reason=expected.reason if expected.decision == "stop_at_limit" else None,
            selected_action_ids=expected.selected_action_ids,
            preserved_action_ids=expected.preserved_action_ids,
            retained_question_ids=expected.retained_question_ids,
            answer_integrity=expected.answer_integrity,
            research_sufficiency=expected.research_sufficiency,
            rationale="Synthetic test proposal; not a real quality evaluation.",
        )
        proposals.append(proposal)
        judgments.append(
            HumanJudgment(
                case_id=case.id,
                reviewer="Synthetic test reviewer (not human approval)",
                proposal_digest=digest(proposal),
                case_digest=digest(case),
                rubric_digest=digest(CORPUS.rubric_definition),
                evidence_and_scope=2,
                question_coverage=2,
                action_and_stop=2,
                evidence_notes="Synthetic rating for the aggregator test.",
                coverage_notes="Synthetic rating for the aggregator test.",
                action_notes="Synthetic rating for the aggregator test.",
                fatal_errors=(),
            )
        )
    return ReviewBundle(
        rubric_approval=RubricApproval(
            reviewer="Synthetic rubric reviewer (test only)",
            corpus_digest=digest(CORPUS),
            approved=True,
            notes="This test does not confer human approval on the rubric.",
        ),
        proposals=tuple(proposals),
        judgments=tuple(judgments),
    )


def test_complete_synthetic_review_passes_and_retains_audit_notes():
    result = score(bundle(), CORPUS)
    assert result["passed"] and not result["missing_cases"]
    assert len(result["results"]) == len(CORPUS.cases)
    assert result["results"][0]["human_notes"]["evidence"]


def test_right_label_cannot_replace_rationale_quality_or_erase_fatal_errors():
    original = bundle()
    for changes in (
        {"evidence_and_scope": 0},
        {"action_and_stop": 1},
        {"fatal_errors": ("invented_evidence_or_attempt",)},
    ):
        first = original.judgments[0].model_copy(update=changes)
        reviewed = original.model_copy(update={"judgments": (first, *original.judgments[1:])})
        result = score(reviewed, CORPUS)
        assert not result["passed"]
        assert all(result["results"][0]["structural_checks"].values())


def test_wrong_decision_fails_despite_perfect_declared_human_scores():
    original = bundle()
    changed = original.proposals[0].model_copy(update={"decision": "stop"})
    reviewed = original.judgments[0].model_copy(update={"proposal_digest": digest(changed)})
    result = score(
        original.model_copy(
            update={
                "proposals": (changed, *original.proposals[1:]),
                "judgments": (reviewed, *original.judgments[1:]),
            }
        ),
        CORPUS,
    )
    assert not result["results"][0]["structural_checks"]["decision"]
    assert not result["passed"]


def test_partial_suite_cannot_claim_full_pass():
    result = score(bundle(CORPUS.cases[:1]), CORPUS)
    assert not result["passed"] and len(result["missing_cases"]) == len(CORPUS.cases) - 1


@pytest.mark.parametrize("field", ["proposal_digest", "case_digest", "rubric_digest"])
def test_stale_human_judgment_is_rejected(field):
    original = bundle()
    changed = original.judgments[0].model_copy(update={field: "stale"})
    with pytest.raises(ValueError, match="stale"):
        score(original.model_copy(update={"judgments": (changed, *original.judgments[1:])}), CORPUS)


@pytest.mark.parametrize("changes", [{"approved": False}, {"corpus_digest": "stale"}])
def test_unapproved_or_changed_rubric_cannot_be_scored(changes):
    original = bundle()
    with pytest.raises(ValueError, match="rubric approval"):
        score(
            original.model_copy(
                update={
                    "rubric_approval": original.rubric_approval.model_copy(update=changes),
                }
            ),
            CORPUS,
        )


def test_changed_scoring_anchor_invalidates_prior_human_approval():
    data = json.loads(CORPUS.model_dump_json())
    data["rubric_definition"]["evidence_and_scope"]["two"] = "A materially changed anchor"
    changed = type(CORPUS).model_validate(data)
    with pytest.raises(ValueError, match="rubric approval"):
        score(bundle(), changed)


def test_renewed_corpus_approval_cannot_reuse_ratings_under_old_anchors():
    data = json.loads(CORPUS.model_dump_json())
    data["rubric_definition"]["action_and_stop"]["two"] = "A different human scoring anchor"
    changed = type(CORPUS).model_validate(data)
    original = bundle()
    reapproved = original.model_copy(
        update={
            "rubric_approval": original.rubric_approval.model_copy(
                update={
                    "corpus_digest": digest(changed),
                }
            ),
        }
    )
    with pytest.raises(ValueError, match="stale.*scoring rubric"):
        score(reapproved, changed)


def test_missing_fatal_definition_invalidates_the_rubric():
    data = json.loads(CORPUS.model_dump_json())
    data["rubric_definition"]["fatal_errors"].pop("unsupported_sufficiency")
    with pytest.raises(ValidationError, match="every fatal error"):
        type(CORPUS).model_validate(data)


def test_duplicate_or_unmatched_judgments_are_rejected():
    original = bundle()
    for rows in ((original.judgments[0], *original.judgments), original.judgments[1:]):
        with pytest.raises(ValueError, match="Duplicate|matching"):
            score(original.model_copy(update={"judgments": rows}), CORPUS)


@pytest.mark.parametrize("value", [-1, 3, 1.5, True])
def test_ratings_must_use_integer_anchors(value):
    data = bundle().judgments[0].model_dump()
    data["evidence_and_scope"] = value
    with pytest.raises(ValidationError):
        HumanJudgment.model_validate(data)


def test_offline_cli_exports_blind_inputs_and_scores_files(tmp_path, capsys):
    assert main(["inputs"]) == 0
    inputs = json.loads(capsys.readouterr().out)
    assert len(inputs["cases"]) == len(CORPUS.cases)
    assert all("expected" not in case and "rubric" not in case for case in inputs["cases"])
    path = tmp_path / "reviewed.json"
    path.write_text(bundle().model_dump_json())
    assert main(["score", "--input", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]
    path.write_text(bundle(CORPUS.cases[:1]).model_dump_json())
    assert main(["score", "--input", str(path)]) == 1
    capsys.readouterr()
    path.write_text("{bad-json")
    assert main(["score", "--input", str(path)]) == 2
    output = capsys.readouterr()
    assert not output.out and "Invalid JSON" in output.err
