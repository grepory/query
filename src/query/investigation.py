"""Investigation contracts and lifecycle rules, independent of persistence and providers."""

from datetime import date
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0, strict=True)]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Question(Contract):
    key: Text
    text: Text
    acceptance_criteria: Text
    required: bool = True


class Brief(Contract):
    question: Text
    scope: Text
    cutoff: date
    questions: tuple[Question, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_questions(self) -> Brief:
        if len({q.key for q in self.questions}) != len(self.questions):
            raise ValueError("Question keys must be unique within a brief")
        if not any(q.required for q in self.questions):
            raise ValueError("A brief must have at least one required question")
        return self


class Phase(StrEnum):
    SCOPE = "scope"
    PLANNING = "planning"
    GATHERING = "gathering"
    ASSESSING = "assessing"
    REVIEWING = "reviewing"
    AWAITING_INPUT = "awaiting_input"
    PAUSED = "paused"
    PARKED = "parked"
    STOPPED_AT_LIMIT = "stopped_at_limit"
    COMPLETE = "complete"
    CANCELED = "canceled"


class Outcome(StrEnum):
    ANSWERED = "answered"
    QUALIFIED = "qualified"
    UNRESOLVED = "unresolved"


class Lane(StrEnum):
    RESEARCH = "research"
    REVIEW = "review"


class Limits(Contract):
    research: Count
    review: Count
    total: Annotated[int, Field(gt=0, strict=True)]


class Budget(Contract):
    limits: Limits
    research_spent: Count = 0
    review_spent: Count = 0

    def remaining(self, lane: Lane) -> int:
        spent = self.research_spent if lane == Lane.RESEARCH else self.review_spent
        allowance = self.limits.research if lane == Lane.RESEARCH else self.limits.review
        return max(0, min(allowance - spent, self.limits.total - self.total_spent))

    @property
    def total_spent(self) -> int:
        return self.research_spent + self.review_spent


class CompletionChecks(Contract):
    brief_version: Annotated[int, Field(gt=0)]
    answer_ref: Text
    answer_integrity: bool
    research_sufficiency: bool
    rationale: Text


class Checkpoint(Contract):
    phase: Phase = Phase.SCOPE
    brief_version: Annotated[int, Field(gt=0)] = 1
    budget: Budget
    resume_phase: Phase | None = None
    stop_reason: Text | None = None
    completion: CompletionChecks | None = None
    outcome: Outcome | None = None

    @model_validator(mode="after")
    def check_completion(self) -> Checkpoint:
        if self.phase == Phase.COMPLETE:
            if not self.completion or not self.outcome:
                raise ValueError("Completion requires checks and an answer outcome")
            if not (self.completion.answer_integrity and self.completion.research_sufficiency):
                raise ValueError("Both completion checks must pass")
            if self.completion.brief_version != self.brief_version:
                raise ValueError("Completion checks must use the current brief")
        elif self.completion is not None or self.outcome is not None:
            raise ValueError("Only a complete investigation has a final outcome")
        return self


class StepStatus(StrEnum):
    PLANNED = "planned"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StepRequest(Contract):
    key: Text
    kind: Text
    lane: Lane
    reserved_tokens: Annotated[int, Field(gt=0, strict=True)]
    inputs: dict[str, JsonValue]


class StepResult(Contract):
    status: StepStatus
    used_tokens: Count
    output: dict[str, JsonValue]
    error: Text | None = None

    @model_validator(mode="after")
    def check_terminal(self) -> StepResult:
        if self.status == StepStatus.PLANNED:
            raise ValueError("A result must be succeeded or failed")
        if (self.status == StepStatus.FAILED) != (self.error is not None):
            raise ValueError("Only a failed step must have an error")
        return self


class Step(Contract):
    brief_version: int
    request: StepRequest
    result: StepResult | None = None


class BriefVersion(Contract):
    version: int
    brief: Brief


class Event(Contract):
    revision: int
    kind: str
    details: dict[str, JsonValue]


class Investigation(Contract):
    id: UUID
    revision: int
    checkpoint: Checkpoint
    briefs: tuple[BriefVersion, ...]
    steps: tuple[Step, ...]
    events: tuple[Event, ...]


ACTIVE = {Phase.SCOPE, Phase.PLANNING, Phase.GATHERING, Phase.ASSESSING, Phase.REVIEWING}
EDGES = {
    Phase.SCOPE: {Phase.PLANNING},
    Phase.PLANNING: {Phase.GATHERING, Phase.REVIEWING},
    Phase.GATHERING: {Phase.ASSESSING},
    Phase.ASSESSING: {Phase.PLANNING, Phase.REVIEWING},
    Phase.REVIEWING: {Phase.PLANNING, Phase.COMPLETE},
}
HOLDS = {Phase.PAUSED, Phase.PARKED, Phase.AWAITING_INPUT, Phase.STOPPED_AT_LIMIT}


def transition(
    current: Checkpoint,
    target: Phase,
    *,
    reason: str | None = None,
    completion: CompletionChecks | None = None,
    outcome: Outcome | None = None,
) -> Checkpoint:
    if target in HOLDS or target == Phase.CANCELED:
        if current.phase not in ACTIVE and not (
            target == Phase.CANCELED and current.phase in HOLDS
        ):
            raise ValueError(f"Cannot move from {current.phase} to {target}")
        if not reason or not reason.strip():
            raise ValueError("A stopping or waiting decision requires a reason")
        if target == Phase.STOPPED_AT_LIMIT and reason not in {"budget", "deadline"}:
            raise ValueError("A limit stop must identify budget or deadline")
    elif target not in EDGES.get(current.phase, set()):
        raise ValueError(f"Cannot move from {current.phase} to {target}")
    return Checkpoint(
        phase=target,
        brief_version=current.brief_version,
        budget=current.budget,
        resume_phase=(
            Phase.PLANNING
            if target == Phase.STOPPED_AT_LIMIT
            else current.phase
            if target in HOLDS
            else None
        ),
        stop_reason=reason,
        completion=completion,
        outcome=outcome,
    )
