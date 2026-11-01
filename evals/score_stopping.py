"""Offline aggregation of human/model judgments under a human-approved rubric."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from evals.stopping_cases import Decision, Fatal, StoppingCorpus, Text, load_corpus
from query.investigation import Contract, Outcome, Phase

Score = Annotated[int, Field(ge=0, le=2, strict=True)]


def digest(value) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Proposal(Contract):
    case_id: Text
    model: Text
    prompt_version: Text
    brief_version: int = Field(gt=0, strict=True)
    snapshot_id: UUID
    decision: Decision
    target: Phase
    outcome: Outcome | None
    limit_reason: Literal["budget", "deadline"] | None
    selected_action_ids: tuple[Text, ...]
    preserved_action_ids: tuple[Text, ...]
    retained_question_ids: tuple[Text, ...]
    answer_integrity: bool
    research_sufficiency: bool
    rationale: Text


class HumanReviewer(Contract):
    kind: Literal["human"]
    name: Text


class ModelReviewer(Contract):
    kind: Literal["model"]
    name: Text
    model: Text
    prompt_version: Text


Reviewer = Annotated[HumanReviewer | ModelReviewer, Field(discriminator="kind")]


class Judgment(Contract):
    case_id: Text
    reviewer: Reviewer
    status: Literal["assessed", "needs_human_review"] = "assessed"
    proposal_digest: Text
    case_digest: Text
    rubric_digest: Text
    evidence_and_scope: Score
    question_coverage: Score
    action_and_stop: Score
    evidence_notes: Text
    coverage_notes: Text
    action_notes: Text
    fatal_errors: tuple[Fatal, ...]


class RubricApproval(Contract):
    reviewer: HumanReviewer
    corpus_digest: Text
    approved: bool
    notes: Text


class ReviewBundle(Contract):
    rubric_approval: RubricApproval
    proposals: tuple[Proposal, ...] = Field(min_length=1)
    judgments: tuple[Judgment, ...] = Field(min_length=1)


def score(bundle: ReviewBundle, corpus: StoppingCorpus) -> dict:
    approval = bundle.rubric_approval
    if not approval.approved or approval.corpus_digest != digest(corpus):
        raise ValueError("Human rubric approval must cover the current corpus and rubric")
    cases = {str(case.evaluation_id): case for case in corpus.cases}
    proposals = {proposal.case_id: proposal for proposal in bundle.proposals}
    judgments = {judgment.case_id: judgment for judgment in bundle.judgments}
    if len(proposals) != len(bundle.proposals) or len(judgments) != len(bundle.judgments):
        raise ValueError("Duplicate proposal or judgment for a case")
    if proposals.keys() != judgments.keys() or not proposals.keys() <= cases.keys():
        raise ValueError("Every known proposal needs exactly one matching judgment")
    rows = []
    for case_id, proposal in proposals.items():
        case, judgment = cases[case_id], judgments[case_id]
        if (
            judgment.proposal_digest != digest(proposal)
            or judgment.case_digest != digest(case)
            or judgment.rubric_digest != digest(corpus.rubric_definition)
        ):
            raise ValueError("Judgment is stale for the proposal, case, or scoring rubric")
        expected = case.expected
        structural = {
            "scope": (
                proposal.brief_version == case.checkpoint.brief_version
                and proposal.snapshot_id == case.snapshot.snapshot.id
            ),
            "decision": proposal.decision == expected.decision,
            "transition": proposal.target == expected.target,
            "outcome": proposal.outcome == expected.outcome,
            "limit_reason": proposal.limit_reason
            == (expected.reason if expected.decision == "stop_at_limit" else None),
            "actions": (
                len(proposal.selected_action_ids) == len(set(proposal.selected_action_ids))
                and set(proposal.selected_action_ids) == set(expected.selected_action_ids)
            ),
            "preserved_actions": (
                len(proposal.preserved_action_ids) == len(set(proposal.preserved_action_ids))
                and set(proposal.preserved_action_ids) == set(expected.preserved_action_ids)
            ),
            "questions": (
                len(proposal.retained_question_ids) == len(set(proposal.retained_question_ids))
                and set(proposal.retained_question_ids) == set(expected.retained_question_ids)
            ),
            "checks": (
                proposal.answer_integrity == expected.answer_integrity
                and proposal.research_sufficiency == expected.research_sufficiency
            ),
        }
        rationale_scores = {
            name: getattr(judgment, name)
            for name in (
                "evidence_and_scope",
                "question_coverage",
                "action_and_stop",
            )
        }
        rows.append(
            {
                "case_id": case_id,
                "case_name": case.id,
                "model": proposal.model,
                "prompt_version": proposal.prompt_version,
                "structural_checks": structural,
                "rationale_scores": rationale_scores,
                "fatal_errors": judgment.fatal_errors,
                "reviewer": judgment.reviewer.model_dump(),
                "review_status": judgment.status,
                "proposal_digest": judgment.proposal_digest,
                "case_digest": judgment.case_digest,
                "rubric_digest": judgment.rubric_digest,
                "review_notes": {
                    "evidence": judgment.evidence_notes,
                    "coverage": judgment.coverage_notes,
                    "action": judgment.action_notes,
                },
                "passed": (
                    all(structural.values())
                    and judgment.status == "assessed"
                    and all(v == 2 for v in rationale_scores.values())
                    and not judgment.fatal_errors
                ),
            }
        )
    missing = sorted(cases.keys() - proposals.keys())
    return {
        "rubric_version": corpus.rubric_version,
        "corpus_digest": digest(corpus),
        "rubric_reviewer": approval.reviewer.model_dump(),
        "reviewer_kinds": sorted({j.reviewer.kind for j in bundle.judgments}),
        "results": rows,
        "missing_cases": missing,
        "passed": not missing and all(row["passed"] for row in rows),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inputs", help="Print prepared model inputs with gold/rubric removed")
    commands.add_parser("manifest", help="Print operator-only case mapping and review digests")
    score_parser = commands.add_parser("score", help="Aggregate already reviewed proposals")
    score_parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        corpus = load_corpus()
        if args.command == "inputs":
            print(
                json.dumps(
                    {
                        "corpus_digest": digest(corpus),
                        "cases": [
                            c.model_input()
                            for c in sorted(corpus.cases, key=lambda c: str(c.evaluation_id))
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "manifest":
            print(
                json.dumps(
                    {
                        "corpus_digest": digest(corpus),
                        "rubric_digest": digest(corpus.rubric_definition),
                        "cases": [
                            {
                                "case_id": str(c.evaluation_id),
                                "case_name": c.id,
                                "case_digest": digest(c),
                            }
                            for c in corpus.cases
                        ],
                    },
                    indent=2,
                )
            )
            return 0
        bundle = ReviewBundle.model_validate_json(args.input.read_bytes())
        report = score(bundle, corpus)
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except (ValueError, OSError) as exc:
        print(f"score_stopping: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
