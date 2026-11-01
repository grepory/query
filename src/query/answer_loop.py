"""Durable answer-loop decisions reconstructed from immutable step results."""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import TypeAdapter, model_validator

from query.evidence import fingerprint
from query.investigation import (
    Contract,
    Lane,
    Phase,
    StepRequest,
    StepResult,
    StepStatus,
)
from query.review import (
    Answer,
    Issue,
    Review,
    ReviewContext,
    Ruling,
    check_answer,
    check_review,
    check_revision,
    check_ruling,
)
from query.review_clients import PROMPT_VERSIONS, CallLimits, ModelCall, ReviewClients

KIND = "answer_loop"
Action = Literal["write", "review", "revise", "adjudicate", "carry_forward"]
SavedCall = ModelCall[Answer] | ModelCall[Review] | ModelCall[Ruling]
CALL = TypeAdapter(SavedCall)


class Operation(Contract):
    snapshot_id: UUID
    context_digest: str
    sequence: int
    action: Action
    output_id: UUID
    answer_id: UUID | None = None
    review_id: UUID | None = None
    issue_id: str | None = None
    ruling_ids: tuple[UUID, ...] = ()

    @property
    def key(self):
        return f"answer-loop/{self.snapshot_id}/{self.sequence}"


class ModelIdentity(Contract):
    provider: Literal["anthropic", "openai"]
    model_name: str


class CycleModels(Contract):
    author: ModelIdentity
    reviewer: ModelIdentity
    adjudicator: ModelIdentity

    @model_validator(mode="after")
    def separate_vendors(self):
        if self.author.provider == self.reviewer.provider:
            raise ValueError("Author and reviewer require different hosted vendors")
        return self


class Execution(Contract):
    role: Literal["author", "reviewer", "adjudicator"]
    provider: Literal["anthropic", "openai"]
    model_name: str
    prompt_version: str
    limits: CallLimits
    models: CycleModels

    @model_validator(mode="after")
    def role_identity(self):
        selected = getattr(self.models, self.role)
        if (self.provider, self.model_name) != (selected.provider, selected.model_name):
            raise ValueError("Execution must use the pinned role model")
        return self


def execution_for(clients, action):
    role = {
        "write": "author",
        "revise": "author",
        "review": "reviewer",
        "adjudicate": "adjudicator",
    }[action]
    settings = getattr(clients.models, role) or clients.models.reviewer
    return Execution(
        role=role,
        provider=settings.model_backend,
        model_name=settings.model_name,
        prompt_version=PROMPT_VERSIONS[role],
        limits=clients.limits,
        models=CycleModels(
            **{
                name: ModelIdentity(
                    provider=(
                        getattr(clients.models, name) or clients.models.reviewer
                    ).model_backend,
                    model_name=(
                        getattr(clients.models, name) or clients.models.reviewer
                    ).model_name,
                )
                for name in ("author", "reviewer", "adjudicator")
            }
        ),
    )


class ReviewedAnswer(Contract):
    answer: Answer
    review: Review

    @model_validator(mode="after")
    def exact_review(self):
        if (self.review.answer_id, self.review.snapshot_id, self.review.brief_version) != (
            self.answer.id,
            self.answer.snapshot_id,
            self.answer.brief_version,
        ):
            raise ValueError("Visible answer requires its exact scoped review")
        return self


class LoopState(Contract):
    snapshot_id: UUID
    status: Literal["working", "settled", "needs_research", "limited"]
    reason: str
    next_operation: Operation | None
    current_answer: ReviewedAnswer | None
    answers: tuple[Answer, ...]
    reviews: tuple[Review, ...]
    rulings: tuple[Ruling, ...]


def identity(answer: Answer, issue: Issue):
    """Exact identity, including inference premises; no semantic rewording guesses."""
    claim = next((c for c in answer.claims if c.id == issue.claim_id), None)
    return (
        answer.brief_version,
        answer.snapshot_id,
        issue.claim_id,
        issue.claim_text,
        issue.question_id if issue.kind == "coverage" else None,
        fingerprint(claim.model_dump(mode="json")) if claim else None,
    )


def ruling_matches(ruling: Ruling, answer: Answer, answers: tuple[Answer, ...]):
    original = next(a for a in answers if a.id == ruling.answer_id)
    old = next((c for c in original.claims if c.id == ruling.claim_id), None)
    new = next((c for c in answer.claims if c.id == ruling.claim_id), None)
    if ruling.claim_id is None or ruling.decision in {"narrow", "remove"}:
        return ruling.claim_text in answer.title or ruling.claim_text in answer.body_markdown
    return old == new and old is not None


def binding_violation(ruling: Ruling, answer: Answer):
    if ruling.decision not in {"narrow", "remove"}:
        return False
    # Replacement may contain the original span; do not mistake it for an ignored edit.
    text = answer.title + "\n" + answer.body_markdown
    if ruling.replacement:
        if ruling.replacement not in text:
            return True
        text = text.replace(ruling.replacement, "")
    return ruling.claim_text in text or any(
        c.id == ruling.claim_id and c.text == ruling.claim_text for c in answer.claims
    )


def prior_response(state: LoopState, issue: Issue):
    """Return the saved defense and its review without rewriting their provenance."""
    current = state.answers[-1]
    by_id = {r.id: r for r in state.reviews}
    answers = {a.id: a for a in state.answers}
    for answer in reversed(state.answers):
        for response in answer.issue_responses:
            review = by_id[response.review_id]
            old_issue = next(i for i in review.issues if i.id == response.issue_id)
            if identity(answers[review.answer_id], old_issue) == identity(current, issue):
                return response, review
    return None, None


def saved_output(step):
    if step.request.inputs["operation"]["action"] == "carry_forward":
        return Answer.model_validate(step.result.output["answer"])
    return CALL.validate_python(step.result.output["call"]).output


def last_reviewed(steps):
    """Historical visibility survives new snapshots without carrying current approval."""
    answers = {}
    visible = None
    for step in steps:
        if (
            step.request.kind != KIND
            or step.result is None
            or step.result.status != StepStatus.SUCCEEDED
        ):
            continue
        output = saved_output(step)
        if isinstance(output, Answer):
            answers[output.id] = output
        elif isinstance(output, Review):
            visible = ReviewedAnswer(answer=answers[output.answer_id], review=output)
    return visible


def reconstruct(context: ReviewContext, steps) -> LoopState:
    snapshot = context.evidence.snapshot
    answers, reviews, rulings = [], [], []
    count = 0
    for step in steps:
        if step.request.kind != KIND:
            continue
        op = Operation.model_validate(step.request.inputs["operation"])
        if op.snapshot_id != snapshot.id:
            continue
        if op.context_digest != context.digest() or op.sequence != count:
            raise ValueError("Answer-loop history does not match its frozen context/order")
        if step.result is None:
            break
        if step.result.status != StepStatus.SUCCEEDED:
            raise ValueError("Failed answer-loop operations require explicit reconciliation")
        output = saved_output(step)
        if isinstance(output, Answer):
            answers.append(output)
        elif isinstance(output, Review):
            reviews.append(output)
        else:
            rulings.append(output)
        count += 1
    answers, reviews, rulings = tuple(answers), tuple(reviews), tuple(rulings)
    by_answer = {r.answer_id: r for r in reviews}
    visible = next(
        (
            ReviewedAnswer(answer=a, review=by_answer[a.id])
            for a in reversed(answers)
            if a.id in by_answer
        ),
        None,
    )
    base = dict(
        snapshot_id=snapshot.id,
        current_answer=visible,
        answers=answers,
        reviews=reviews,
        rulings=rulings,
    )

    def result(status, reason, action=None, **refs):
        op = None
        if action:
            op = Operation(
                snapshot_id=snapshot.id,
                context_digest=context.digest(),
                sequence=count,
                action=action,
                output_id=uuid5(snapshot.id, f"{count}/{action}"),
                **refs,
            )
        return LoopState(**base, status=status, reason=reason, next_operation=op)

    if snapshot.status != "ready":
        return result("limited", "Snapshot contains no usable evidence")
    if not answers:
        return result("working", "Write initial answer", "write")
    answer = answers[-1]
    review = by_answer.get(answer.id)
    if review is None:
        return result(
            "working",
            "Full review required",
            "review",
            answer_id=answer.id,
            review_id=reviews[-1].id if reviews else None,
            ruling_ids=tuple(r.id for r in rulings),
        )
    active = tuple(r for r in rulings if ruling_matches(r, answer, answers))
    unresolved = tuple(r for r in active if r.decision == "insufficient_evidence")
    if unresolved:
        return result(
            "needs_research", "Adjudication requires missing context; no research admitted"
        )
    violations = tuple(r for r in active if binding_violation(r, answer))
    blockers = [
        i
        for i in review.issues
        if i.kind != "editorial"
        and not any(
            i.kind in {"factual", "inference"}
            and r.decision == "supported"
            and (r.claim_id, r.claim_text) == (i.claim_id, i.claim_text)
            for r in active
        )
    ]
    state = LoopState(**base, status="working", reason="Route reviewed issues", next_operation=None)
    for issue in blockers:
        if issue.kind not in {"factual", "inference"}:
            continue
        response, _ = prior_response(state, issue)
        already = any(
            (r.claim_id, r.claim_text) == (issue.claim_id, issue.claim_text) for r in active
        )
        if response is not None and not already:
            return result(
                "working",
                "Repeated exact claim dispute",
                "adjudicate",
                answer_id=answer.id,
                review_id=review.id,
                issue_id=issue.id,
            )
    if any(i.kind == "coverage" and prior_response(state, i)[0] is not None for i in blockers):
        return result(
            "needs_research", "Repeated coverage gap requires a recorded research decision"
        )
    if blockers or violations:
        return result(
            "working",
            "Respond to fresh issues and apply binding edits",
            "revise",
            answer_id=answer.id,
            review_id=review.id,
            ruling_ids=tuple(r.id for r in active),
        )
    if any(r.answer_id == answer.id for r in active):
        return result(
            "working",
            "Ruling requires a new final full review",
            "carry_forward",
            answer_id=answer.id,
            review_id=review.id,
        )
    required = {q.key for q in context.brief.brief.questions if q.required}
    if any(
        c.question_id in required and (c.status == "unresolved" or not c.claim_ids)
        for c in (*answer.coverage, *review.coverage)
    ):
        return result(
            "needs_research",
            "Required question remains unresolved; disposition needs a research decision",
        )
    return result("settled", "No unresolved integrity objections; sufficiency remains separate")


def validate_result(
    operation: Operation,
    result: StepResult,
    state: LoopState,
    context: ReviewContext,
    execution: Execution | None = None,
):
    if result.status != StepStatus.SUCCEEDED:
        raise ValueError("Answer-loop failures must retain the pending intent for reconciliation")
    answer = state.answers[-1] if state.answers else None
    review = state.reviews[-1] if state.reviews else None
    if operation.action == "carry_forward":
        expected = carry_forward(operation, answer)
        if result.used_tokens != 0 or result.output != {"answer": expected.model_dump(mode="json")}:
            raise ValueError("Carry-forward must preserve the exact reviewed answer at zero cost")
        return
    call = CALL.validate_python(result.output["call"])
    output = call.output
    if execution is None or (call.role, call.provider, call.model_name, call.prompt_version) != (
        execution.role,
        execution.provider,
        execution.model_name,
        execution.prompt_version,
    ):
        raise ValueError("Role result must use the persisted model and prompt version")
    role = {
        "write": "author",
        "revise": "author",
        "review": "reviewer",
        "adjudicate": "adjudicator",
    }[operation.action]
    if (
        call.role != role
        or call.context_digest != context.digest()
        or output.id != operation.output_id
        or result.used_tokens != call.usage.input_tokens + call.usage.output_tokens
    ):
        raise ValueError("Committed role result must match its planned operation and usage")
    if operation.action in {"write", "revise"}:
        if not isinstance(output, Answer):
            raise ValueError("Author must return an answer")
        check_answer(output, context)
        if operation.action == "write":
            if output.issue_responses or output.ruling_applications:
                raise ValueError("Initial answer cannot invent responses or rulings")
        else:
            supplied = tuple(r for r in state.rulings if r.id in operation.ruling_ids)
            check_revision(output, review, supplied, context)
    elif operation.action == "review":
        if not isinstance(output, Review):
            raise ValueError("Reviewer must return a review")
        check_review(output, answer, context)
    else:
        if not isinstance(output, Ruling) or output.issue_id != operation.issue_id:
            raise ValueError("Adjudicator must return the planned dispute ruling")
        check_ruling(output, review, context)


def carry_forward(operation: Operation, answer: Answer):
    return answer.model_copy(update={"id": operation.output_id, "issue_responses": ()})


class AnswerController:
    """One committed action per advance; no autonomous research or retry loop."""

    def __init__(self, store, clients: ReviewClients):
        self.store, self.clients = store, clients

    def advance(self, identifier: UUID, expected_revision: int):
        current, context, state = self.store.answer_state(identifier)
        from query.store import StaleRevision

        if current.revision != expected_revision:
            raise StaleRevision(
                f"Expected revision {expected_revision}; current revision is {current.revision}"
            )
        if current.checkpoint.phase != Phase.REVIEWING:
            raise ValueError(
                "Answer-loop execution requires reviewing; explicitly resume held work"
            )
        op = state.next_operation
        if op is None:
            return state
        # Existing step admission provides the token ceiling. Detailed reserve/pricing is NOT-40.
        reserve = (
            self.clients.limits.max_prompt_bytes + self.clients.limits.max_output_tokens
            if op.action != "carry_forward"
            else 1
        )
        execution = execution_for(self.clients, op.action) if op.action != "carry_forward" else None
        request = StepRequest(
            key=op.key,
            kind=KIND,
            lane=Lane.REVIEW,
            reserved_tokens=reserve,
            inputs={
                "operation": op.model_dump(mode="json"),
                "execution": execution.model_dump(mode="json") if execution else None,
            },
        )
        planned = self.store.plan_step(identifier, current.revision, request)
        answer = state.answers[-1] if state.answers else None
        review = state.reviews[-1] if state.reviews else None
        supplied = tuple(r for r in state.rulings if r.id in op.ruling_ids)
        if op.action == "carry_forward":
            result = StepResult(
                status=StepStatus.SUCCEEDED,
                used_tokens=0,
                output={"answer": carry_forward(op, answer).model_dump(mode="json")},
            )
        else:
            if op.action == "write":
                call = self.clients.write(context, op.output_id)
            elif op.action == "review":
                call = self.clients.review(
                    context, op.output_id, answer, previous_review=review, rulings=supplied
                )
            elif op.action == "revise":
                call = self.clients.revise(context, op.output_id, answer, review, rulings=supplied)
            else:
                issue = next(i for i in review.issues if i.id == op.issue_id)
                response, response_review = prior_response(state, issue)
                call = self.clients.adjudicate(
                    context,
                    op.output_id,
                    answer,
                    review,
                    issue.id,
                    response,
                    response_review=response_review,
                )
            result = StepResult(
                status=StepStatus.SUCCEEDED,
                used_tokens=call.usage.input_tokens + call.usage.output_tokens,
                output={"call": call.model_dump(mode="json")},
            )
        self.store.finish_step(identifier, planned.revision, op.key, result)
        return self.store.answer_state(identifier)[2]
