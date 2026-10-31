"""Prepared stopping histories and gold expectations; no decision enforcement."""

import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from query.evidence import LoadedSnapshot
from query.investigation import Brief, Checkpoint, Contract, Lane, Outcome, Phase

Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0, strict=True)]
Decision = Literal["research", "revise", "clarify", "qualify", "stop", "stop_at_limit", "park"]
Fatal = Literal[
    "invented_evidence_or_attempt",
    "false_independence",
    "unsupported_sufficiency",
    "lost_required_question",
    "unreviewed_final_answer",
    "unbounded_or_unaffordable_action",
]
Family = Literal[
    "unread_primary",
    "private_fact",
    "circular_reporting",
    "scope_date_conflict",
    "impossible_certainty",
    "expensive_action",
    "exhausted_budget",
    "repeated_evidence",
    "removed_claim",
]


class Attempt(Contract):
    id: Text
    question_ids: tuple[Text, ...] = Field(min_length=1)
    method: Text
    target: Text
    result: Literal["evidence", "no_change", "unavailable", "operational_failure"]
    version_ids: tuple[UUID, ...]
    used_tokens: Count
    observed_progress: Text


class Action(Contract):
    id: Text
    question_ids: tuple[Text, ...] = Field(min_length=1)
    kind: Literal["fetch", "search", "reconcile", "revise", "ask_user", "review"]
    lane: Lane
    missing_evidence: Text
    consequence: Text
    next_action: Text
    stop_condition: Text
    estimated_tokens: Count
    availability: Literal["available", "unavailable", "exhausted"]


class Claim(Contract):
    id: Text
    question_id: Text
    text: Text


class QuestionHistory(Contract):
    question_id: Text
    state: Literal["answered", "open", "unresolved"]
    version_ids: tuple[UUID, ...]
    attempt_ids: tuple[Text, ...]
    unresolved_reason: Text | None = None


class Expected(Contract):
    decision: Decision
    selected_action_ids: tuple[Text, ...]
    preserved_action_ids: tuple[Text, ...] = ()
    retained_question_ids: tuple[Text, ...] = Field(min_length=1)
    target: Phase
    reason: Text | None = None
    outcome: Outcome | None = None
    answer_integrity: bool
    research_sufficiency: bool
    rationale: Text
    required_records: tuple[Text, ...] = Field(min_length=1)
    forbidden: tuple[Text, ...] = Field(min_length=1)


class Rubric(Contract):
    must_explain: tuple[Text, ...] = Field(min_length=1)
    fatal_examples: tuple[Text, ...] = Field(min_length=1)


class ScoreAnchors(Contract):
    zero: Text
    one: Text
    two: Text


class RubricDefinition(Contract):
    version: Literal[1] = 1
    evidence_and_scope: ScoreAnchors
    question_coverage: ScoreAnchors
    action_and_stop: ScoreAnchors
    fatal_errors: dict[Fatal, Text]

    @model_validator(mode="after")
    def complete_fatal_definitions(self):
        from typing import get_args

        if set(self.fatal_errors) != set(get_args(Fatal)):
            raise ValueError("Rubric must define every fatal error")
        return self


class StoppingCase(Contract):
    id: Text
    family: Family
    boundary_of: Text | None = None
    boundary_change: Text | None = None
    brief: Brief
    checkpoint: Checkpoint
    snapshot: LoadedSnapshot
    reporting_origins: dict[UUID, Text]
    questions: tuple[QuestionHistory, ...] = Field(min_length=1)
    claims: tuple[Claim, ...]
    removed_claims: tuple[Claim, ...]
    answer_ref: Text
    last_reviewed_answer_ref: Text | None
    attempts: tuple[Attempt, ...]
    actions: tuple[Action, ...]
    reviewer_challenge: Text | None = None
    expected: Expected
    rubric: Rubric

    @model_validator(mode="after")
    def references(self):
        def unique(values, label):
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label}")

        manifest = self.snapshot.snapshot
        if manifest.brief_version != self.checkpoint.brief_version:
            raise ValueError("Snapshot must use current brief version")
        versions = {v.id: v for v in self.snapshot.versions}
        unique([v.id for v in self.snapshot.versions], "version")
        if set(self.reporting_origins) != set(versions):
            raise ValueError("Every saved version must have a reporting origin")
        candidates = manifest.candidates
        unique([c.version_id for c in candidates], "snapshot candidate")
        if {c.version_id for c in candidates} != set(versions):
            raise ValueError("Snapshot candidates must match saved versions")
        observations = {o.operation_key: o for o in self.snapshot.observations}
        unique([o.operation_key for o in self.snapshot.observations], "observation")
        included_bytes = 0
        for candidate in candidates:
            version = versions[candidate.version_id]
            if not candidate.observation_keys:
                raise ValueError("Snapshot candidate needs frozen provenance")
            for key in candidate.observation_keys:
                observation = observations.get(key)
                if observation is None or observation.version_id != version.id:
                    raise ValueError("Frozen provenance must name the candidate version")
                if (
                    observation.investigation_id != manifest.investigation_id
                    or observation.brief_version != manifest.brief_version
                    or observation.revision > manifest.revision
                    or observation.content_hash != version.content_hash
                ):
                    raise ValueError("Observation scope or content does not match snapshot")
            if len(candidate.passages) != len(version.passages):
                raise ValueError("Snapshot must account for every saved passage")
            for disposition, passage in zip(candidate.passages, version.passages, strict=True):
                if (disposition.passage_id, disposition.start, disposition.end) != (
                    passage.id,
                    passage.start,
                    passage.end,
                ):
                    raise ValueError("Snapshot passage must resolve to exact saved text")
                if disposition.included:
                    if disposition.reason is not None:
                        raise ValueError("Included passage cannot have an exclusion reason")
                    included_bytes += len(version.passage_text(passage).encode())
                elif not disposition.reason:
                    raise ValueError("Excluded passage needs a reason")
        if manifest.status != "ready" or manifest.reason is not None or included_bytes == 0:
            raise ValueError("Stopping histories require a ready nonempty snapshot")
        if included_bytes != manifest.included_bytes or included_bytes != manifest.requested_bytes:
            raise ValueError("Snapshot byte accounting must match selected text")
        question_ids = {q.key for q in self.brief.questions}
        unique([q.question_id for q in self.questions], "question history")
        if {q.question_id for q in self.questions} != question_ids:
            raise ValueError("Question history must retain every brief question")
        for rows, label in (
            (self.actions, "action"),
            (self.attempts, "attempt"),
            (self.claims + self.removed_claims, "claim"),
        ):
            unique([r.id for r in rows], label)
        actions = {a.id for a in self.actions}
        attempts = {a.id for a in self.attempts}
        attempts_by_id = {a.id: a for a in self.attempts}
        histories = {q.question_id: q for q in self.questions}
        for row in (*self.actions, *self.attempts):
            if not set(row.question_ids) <= question_ids:
                raise ValueError("Action or attempt names an unknown question")
        for attempt in self.attempts:
            if not set(attempt.version_ids) <= versions.keys():
                raise ValueError("Attempt names an unknown source version")
            if attempt.result == "evidence" and not attempt.version_ids:
                raise ValueError("Evidence attempt must identify saved evidence")
            for question_id in attempt.question_ids:
                if attempt.id not in histories[question_id].attempt_ids:
                    raise ValueError("Attempt attribution must appear in its question history")
        for question in self.questions:
            if not set(question.version_ids) <= versions.keys():
                raise ValueError("Question names an unknown source version")
            if not set(question.attempt_ids) <= attempts:
                raise ValueError("Question names an unknown attempt")
            if any(
                question.question_id not in attempts_by_id[key].question_ids
                for key in question.attempt_ids
            ):
                raise ValueError("Question history must name attempts attributed to that question")
            if question.state == "unresolved" and not question.unresolved_reason:
                raise ValueError("Unresolved question needs an explicit reason")
        if any(c.question_id not in question_ids for c in (*self.claims, *self.removed_claims)):
            raise ValueError("Claim names an unknown question")
        expected = self.expected
        if not set(expected.selected_action_ids + expected.preserved_action_ids) <= actions:
            raise ValueError("Expected decision names an unknown action")
        if not {q.key for q in self.brief.questions if q.required} <= set(
            expected.retained_question_ids
        ):
            raise ValueError("Expected decision must retain every required question")
        if not set(expected.retained_question_ids) <= question_ids:
            raise ValueError("Expected decision names an unknown question")
        if expected.decision == "stop":
            if (
                expected.target != Phase.COMPLETE
                or expected.outcome is None
                or not expected.answer_integrity
                or not expected.research_sufficiency
            ):
                raise ValueError("Stop requires both completion checks and an outcome")
        elif expected.target == Phase.COMPLETE or expected.outcome is not None:
            raise ValueError("Only stop may complete an investigation")
        target_by_decision = {
            "research": Phase.PLANNING,
            "revise": Phase.REVIEWING,
            "clarify": Phase.AWAITING_INPUT,
            "qualify": Phase.REVIEWING,
            "park": Phase.PARKED,
        }
        if (
            expected.decision in target_by_decision
            and expected.target != target_by_decision[expected.decision]
        ):
            raise ValueError("Expected decision has an inconsistent lifecycle target")
        if (
            expected.decision in {"research", "revise", "clarify"}
            and not expected.selected_action_ids
        ):
            raise ValueError("An actionable decision must select a candidate action")
        if expected.decision in {"stop_at_limit", "park"} and not expected.preserved_action_ids:
            raise ValueError("Operational stop must preserve the worthwhile next action")
        if expected.decision == "park" and self.last_reviewed_answer_ref is None:
            raise ValueError("Prepared review park must identify the last reviewed answer")
        if expected.decision == "stop_at_limit" and (
            expected.target != Phase.STOPPED_AT_LIMIT
            or expected.reason not in {"budget", "deadline"}
            or expected.research_sufficiency
        ):
            raise ValueError(
                "Limit stop must preserve insufficient research and identify the limit"
            )
        return self

    def model_input(self) -> dict:
        return self.model_dump(
            mode="json",
            exclude={
                "expected",
                "rubric",
                "boundary_of",
                "boundary_change",
                "family",
            },
        )


class StoppingCorpus(Contract):
    format_version: Literal[1] = 1
    design_url: Literal[
        "https://linear.app/nothanks-io/document/query-investigation-design-99eeebe4bfb0"
    ]
    rubric_version: Literal[1] = 1
    rubric_definition: RubricDefinition
    cases: tuple[StoppingCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def boundaries(self):
        cases = {c.id: c for c in self.cases}
        if len(cases) != len(self.cases):
            raise ValueError("Duplicate stopping case")
        for case in self.cases:
            if (case.boundary_of is None) != (case.boundary_change is None):
                raise ValueError("Boundary must identify its parent and changed fact")
            if case.boundary_of is not None:
                base = cases.get(case.boundary_of)
                if base is None or base.boundary_of is not None or base.family != case.family:
                    raise ValueError("Boundary must name a base case in the same family")
                if base.expected.decision == case.expected.decision:
                    raise ValueError("Boundary must change the expected decision")
        return self


def load_corpus() -> StoppingCorpus:
    path = Path(__file__).parent / "fixtures" / "stopping_decisions.json"
    return StoppingCorpus.model_validate(json.loads(path.read_text()))
