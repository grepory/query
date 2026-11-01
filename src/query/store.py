"""Transactional investigation storage; no model calls or workflow execution."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from hashlib import blake2b
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from query import db
from query.answer_loop import (
    CALL,
    KIND,
    Execution,
    Operation,
    last_reviewed,
    reconstruct,
    validate_result,
)
from query.evidence import (
    EvidenceInput,
    EvidenceSnapshot,
    FreezeSnapshot,
    IngestText,
    LoadedSnapshot,
    Passage,
    SnapshotCandidate,
    SourceVersion,
    fingerprint,
    ingest_identity,
    passage_ranges,
    select_passages,
)
from query.export import InvestigationExport
from query.investigation import (
    ACTIVE,
    HOLDS,
    Brief,
    BriefVersion,
    Budget,
    Checkpoint,
    CompletionChecks,
    Event,
    Investigation,
    Lane,
    Limits,
    Outcome,
    Phase,
    Step,
    StepRequest,
    StepResult,
    transition,
)
from query.recovery import (
    ReviewAttempt,
    admission_reserve,
    in_closeout,
    review_attempts,
    saved_policy,
)
from query.review import ReviewContext


class StaleRevision(ValueError):
    """The caller must reload before applying a decision to newer state."""


class InvestigationStore:
    def __init__(self, engine: Engine, *, clock=None):
        self.engine = engine
        self.clock = clock
        self._owner = ContextVar("query_review_owner", default=None)

    def _now(self, conn):
        return self.clock() if self.clock else conn.scalar(select(func.clock_timestamp()))

    @contextmanager
    def answer_owner(self, identifier, expected_revision):
        key = int.from_bytes(
            blake2b(f"query:review:{identifier}".encode(), digest_size=8).digest(), signed=True
        )
        with self.engine.connect() as conn:
            if not conn.scalar(select(func.pg_try_advisory_lock(key))):
                raise ValueError("Another executor owns this investigation")
            conn.commit()
            pid = conn.scalar(select(func.pg_backend_pid()))
            conn.commit()
            token = self._owner.set((identifier, conn, pid, key))
            try:
                self.load_revision(identifier, expected_revision)
                yield
            finally:
                self._owner.reset(token)
                if not conn.closed and not conn.invalidated:
                    try:
                        conn.execute(select(func.pg_advisory_unlock(key)))
                        conn.commit()
                    except SQLAlchemyError:
                        conn.invalidate()

    def load_revision(self, identifier, expected_revision):
        with self.engine.begin() as conn:
            self._lock(conn, identifier, expected_revision, read=True)

    def _require_owner(self, identifier):
        owner = self._owner.get()
        if owner is None or owner[0] != identifier:
            raise ValueError("Answer attempts require execution ownership")
        _, conn, pid, key = owner
        if conn.closed or conn.invalidated:
            raise ValueError("Execution ownership connection was lost")
        try:
            held = conn.scalar(
                text(
                    "SELECT pg_backend_pid() = :pid AND EXISTS ("
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = :pid "
                    "AND classid::bigint = :high AND objid::bigint = :low "
                    "AND objsubid = 1 AND granted)"
                ),
                {"pid": pid, "high": (key & ((1 << 64) - 1)) >> 32, "low": key & ((1 << 32) - 1)},
            )
            conn.commit()
        except SQLAlchemyError:
            conn.invalidate()
            raise
        if not held:
            raise ValueError("Execution ownership was lost")

    def create(self, brief: Brief, limits: Limits, *, lifetime_tokens=None) -> Investigation:
        ceiling = limits.total if lifetime_tokens is None else lifetime_tokens
        if type(ceiling) is not int or ceiling < limits.total:
            raise ValueError("Lifetime token ceiling must cover the initial total allowance")
        identifier = uuid4()
        checkpoint = Checkpoint(budget=Budget(limits=limits))
        with self.engine.begin() as conn:
            conn.execute(
                insert(db.investigations).values(
                    id=identifier,
                    revision=1,
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
            )
            conn.execute(
                insert(db.briefs).values(
                    investigation_id=identifier,
                    version=1,
                    payload=brief.model_dump(mode="json"),
                )
            )
            conn.execute(
                insert(db.events).values(
                    investigation_id=identifier,
                    revision=1,
                    kind="created",
                    details={"lifetime_tokens": ceiling},
                )
            )
            return self._read(conn, identifier)

    def load(self, identifier: UUID) -> Investigation:
        with self.engine.begin() as conn:
            self._lock(conn, identifier, read=True)
            return self._read(conn, identifier)

    def export(self, identifier: UUID) -> InvestigationExport:
        """Read lifecycle, decisions and exact evidence under one shared row lock."""
        with self.engine.begin() as conn:
            self._lock(conn, identifier, read=True)
            observations = tuple(
                EvidenceInput.model_validate(payload)
                for payload in conn.execute(
                    select(db.evidence_inputs.c.payload)
                    .where(db.evidence_inputs.c.investigation_id == identifier)
                    .order_by(db.evidence_inputs.c.operation_key)
                ).scalars()
            )
            version_ids = sorted(
                {o.version_id for o in observations if o.version_id is not None}, key=str
            )
            snapshots = tuple(
                EvidenceSnapshot.model_validate(payload)
                for payload in conn.execute(
                    select(db.evidence_snapshots.c.manifest)
                    .where(db.evidence_snapshots.c.investigation_id == identifier)
                    .order_by(db.evidence_snapshots.c.manifest["revision"].as_integer())
                ).scalars()
            )
            investigation = self._read(conn, identifier)
            return InvestigationExport(
                investigation=investigation,
                last_reviewed_answer=last_reviewed(investigation.steps),
                review_attempts=review_attempts(investigation),
                evidence_inputs=observations,
                source_versions=tuple(self._read_source_version(conn, v) for v in version_ids),
                snapshots=snapshots,
                answer_loop=(
                    self._answer_state(conn, identifier)[2]
                    if any(
                        s.brief_version == investigation.checkpoint.brief_version for s in snapshots
                    )
                    else None
                ),
            )

    def answer_state(self, identifier: UUID):
        with self.engine.begin() as conn:
            self._lock(conn, identifier, read=True)
            return self._answer_state(conn, identifier)

    def _answer_state(self, conn, identifier):
        current = self._read(conn, identifier)
        snapshot_id = conn.execute(
            select(db.evidence_snapshots.c.id)
            .where(
                db.evidence_snapshots.c.investigation_id == identifier,
                db.evidence_snapshots.c.brief_version == current.checkpoint.brief_version,
            )
            .order_by(db.evidence_snapshots.c.manifest["revision"].as_integer().desc())
            .limit(1)
        ).scalar_one_or_none()
        if snapshot_id is None:
            raise ValueError("Freeze a snapshot for the current brief before answering")
        context = ReviewContext(
            brief=current.briefs[-1], evidence=self._read_snapshot(conn, snapshot_id)
        )
        state = reconstruct(context, current.steps, closeout=in_closeout(current, snapshot_id))
        return (
            current,
            context,
            state.model_copy(
                update={
                    "lifecycle_phase": current.checkpoint.phase,
                    "closeout": in_closeout(current, snapshot_id),
                }
            ),
        )

    def configure_review(self, identifier, expected_revision, snapshot_id, policy):
        self._require_owner(identifier)
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier, expected_revision)
            current, _, state = self._answer_state(conn, identifier)
            if state.snapshot_id != snapshot_id or saved_policy(current, snapshot_id) is not None:
                raise ValueError("Review policy must be saved once for the current snapshot")
            return self._save(
                conn,
                row,
                checkpoint,
                "review_policy",
                {"snapshot_id": str(snapshot_id), "policy": policy.model_dump(mode="json")},
            )

    def enter_closeout(self, identifier, expected_revision, snapshot_id):
        self._require_owner(identifier)
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier, expected_revision)
            self._require_no_pending(conn, identifier)
            current, _, state = self._answer_state(conn, identifier)
            if checkpoint.phase != Phase.REVIEWING or state.snapshot_id != snapshot_id:
                raise ValueError("Closeout requires the active review snapshot")
            if in_closeout(current, snapshot_id):
                return current
            return self._save(
                conn, row, checkpoint, "review_closeout", {"snapshot_id": str(snapshot_id)}
            )

    def park_review_limit(self, identifier, expected_revision, required):
        self._require_owner(identifier)
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier, expected_revision)
            held = transition(checkpoint, Phase.PARKED, reason="review_reserve")
            return self._save(conn, row, held, "review_limit", {"required_tokens": required})

    def begin_answer_attempt(self, identifier, expected_revision, key, policy):
        self._require_owner(identifier)
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier, expected_revision)
            current, _, state = self._answer_state(conn, identifier)
            step = self._step(conn, identifier, key)
            if (
                checkpoint.phase != Phase.REVIEWING
                or step is None
                or step.result is not None
                or key != state.next_operation.key
            ):
                raise ValueError("Attempt must use the active pending answer operation")
            if saved_policy(current, state.snapshot_id) != policy:
                raise ValueError("Attempt must use the saved review policy")
            execution = Execution.model_validate(step.request.inputs["execution"])
            bound = execution.limits.max_prompt_bytes + execution.limits.max_output_tokens
            last_paid = next(
                (
                    s.result.used_tokens
                    for s in reversed(current.steps)
                    if s.request.kind == KIND
                    and s.result is not None
                    and s.request.inputs["operation"]["action"] in {"write", "revise"}
                    and s.result.used_tokens > 0
                ),
                0,
            )
            if checkpoint.budget.remaining(Lane.REVIEW) < admission_reserve(
                state.next_operation, bound, last_paid
            ):
                raise ValueError("Attempt exceeds the protected final-review reserve")
            history = review_attempts(current)
            if any(a.state == "started" for a in history):
                raise ValueError("Reconcile the uncertain attempt before retrying")
            prior = [a for a in history if a.key == key]
            now = self._now(conn)
            if prior and prior[-1].retry_at is not None and prior[-1].retry_at > now:
                raise ValueError("Retry is not due yet")
            attempt = ReviewAttempt(
                id=uuid4(),
                key=key,
                number=len(prior) + 1,
                state="started",
                started_at=now,
                prices=policy.prices.get(step.request.inputs["execution"]["role"]),
            )
            self._save(
                conn,
                row,
                checkpoint,
                "review_attempt_started",
                {"review_attempt": attempt.model_dump(mode="json")},
            )
            return attempt

    def authorize_answer_attempt(self, identifier, attempt_id):
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier)
            attempt = self._pending_attempt(conn, identifier, attempt_id)
            if checkpoint.phase != Phase.REVIEWING:
                self._failed_attempt(
                    conn,
                    row,
                    checkpoint,
                    attempt,
                    kind="stale_execution",
                    retryable=False,
                    local=True,
                )
                return False
            return True

    def _pending_attempt(self, conn, identifier, attempt_id):
        self._require_owner(identifier)
        attempt = next(
            (a for a in review_attempts(self._read(conn, identifier)) if a.id == attempt_id), None
        )
        if attempt is None or attempt.state != "started":
            raise ValueError("Attempt is not pending")
        return attempt

    def _failed_attempt(
        self,
        conn,
        row,
        checkpoint,
        attempt,
        *,
        kind,
        retryable,
        usage=None,
        local=False,
        unknown=False,
    ):
        current = self._read(conn, row["id"])
        step = self._step(conn, row["id"], attempt.key)
        snapshot_id = UUID(step.request.inputs["operation"]["snapshot_id"])
        policy = saved_policy(current, snapshot_id)
        now = self._now(conn)
        charged = (
            usage.input_tokens + usage.output_tokens
            if usage is not None
            else 0
            if local
            else step.request.reserved_tokens
        )
        delay = min(
            policy.max_retry_delay_seconds,
            policy.retry_delay_seconds * 2 ** min(attempt.number - 1, 20),
        )
        ended = attempt.model_copy(
            update={
                "state": "unknown" if unknown else "failed",
                "finished_at": now,
                "usage": usage,
                "charged_tokens": charged,
                "charge_basis": "reported"
                if usage is not None
                else "not_called"
                if local
                else "reservation",
                "error_kind": kind,
                "retryable": retryable,
                "retry_at": now + timedelta(seconds=delay) if retryable else None,
                "estimated_usd": attempt.prices.cost(usage) if attempt.prices and usage else None,
            }
        )
        budget = checkpoint.budget.model_copy(
            update={"review_spent": checkpoint.budget.review_spent + charged}
        )
        updated = checkpoint.model_copy(update={"budget": budget})
        if checkpoint.phase in ACTIVE:
            updated = transition(updated, Phase.PAUSED if retryable else Phase.PARKED, reason=kind)
        return self._save(
            conn,
            row,
            updated,
            "review_attempt_unknown" if unknown else "review_attempt_failed",
            {"review_attempt": ended.model_dump(mode="json")},
        )

    def fail_answer_attempt(self, identifier, attempt_id, error):
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier)
            attempt = self._pending_attempt(conn, identifier, attempt_id)
            return self._failed_attempt(
                conn,
                row,
                checkpoint,
                attempt,
                kind=error.kind,
                retryable=error.retryable,
                usage=error.usage,
                local=error.kind == "context_limit",
            )

    def reconcile_answer_attempt(self, identifier):
        with self.engine.begin() as conn:
            row, checkpoint = self._lock(conn, identifier)
            attempt = next(
                a for a in review_attempts(self._read(conn, identifier)) if a.state == "started"
            )
            self._require_owner(identifier)
            return self._failed_attempt(
                conn,
                row,
                checkpoint,
                attempt,
                kind="remote_outcome_unknown",
                retryable=False,
                unknown=True,
            )

    def revise_brief(self, identifier: UUID, expected_revision: int, brief: Brief) -> Investigation:
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            if current.phase not in ACTIVE | HOLDS:
                raise ValueError("A terminal investigation cannot change its brief")
            self._require_no_pending(conn, identifier)
            version = current.brief_version + 1
            conn.execute(
                insert(db.briefs).values(
                    investigation_id=identifier,
                    version=version,
                    payload=brief.model_dump(mode="json"),
                )
            )
            held = current.phase in {Phase.PAUSED, Phase.PARKED, Phase.STOPPED_AT_LIMIT}
            checkpoint = Checkpoint(
                phase=current.phase if held else Phase.SCOPE,
                brief_version=version,
                budget=current.budget,
                resume_phase=Phase.SCOPE if held else None,
                stop_reason=current.stop_reason if held else None,
            )
            return self._save(conn, row, checkpoint, "brief_revised", {"version": version})

    def move(
        self,
        identifier: UUID,
        expected_revision: int,
        target: Phase,
        *,
        reason: str | None = None,
        completion: CompletionChecks | None = None,
        outcome: Outcome | None = None,
    ) -> Investigation:
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            if target not in HOLDS | {Phase.CANCELED}:
                self._require_no_pending(conn, identifier)
            checkpoint = transition(
                current, target, reason=reason, completion=completion, outcome=outcome
            )
            return self._save(
                conn,
                row,
                checkpoint,
                "transition",
                {
                    "from": current.phase.value,
                    "to": target.value,
                    "reason": reason,
                },
            )

    def resume(self, identifier: UUID, expected_revision: int) -> Investigation:
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            if current.phase not in {Phase.PAUSED, Phase.PARKED, Phase.STOPPED_AT_LIMIT}:
                raise ValueError("Only paused, parked, or limit-stopped work can resume")
            attempts = review_attempts(self._read(conn, identifier))
            if (
                attempts
                and attempts[-1].retry_at is not None
                and attempts[-1].retry_at > self._now(conn)
            ):
                raise ValueError("Retry is not due yet")
            target = current.resume_phase
            if target not in ACTIVE:
                raise ValueError("No resumable phase was recorded")
            if current.phase == Phase.STOPPED_AT_LIMIT:
                if not any(current.budget.remaining(lane) for lane in Lane):
                    raise ValueError("Increase the allowance before resuming")
            checkpoint = Checkpoint(
                phase=target, brief_version=current.brief_version, budget=current.budget
            )
            return self._save(conn, row, checkpoint, "resumed", {"from": current.phase.value})

    def grant(self, identifier: UUID, expected_revision: int, limits: Limits) -> Investigation:
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            if current.phase in {Phase.COMPLETE, Phase.CANCELED}:
                raise ValueError("Cannot grant a terminal investigation")
            history = self._read(conn, identifier)
            ceiling = history.events[0].details.get("lifetime_tokens", current.budget.limits.total)
            requested_limits = limits
            limits = limits.model_copy(update={"total": min(limits.total, ceiling)})
            old = current.budget.limits
            if any(
                getattr(limits, key) < getattr(old, key) for key in ("research", "review", "total")
            ):
                raise ValueError("A grant cannot reduce an allowance")
            if limits == old:
                raise ValueError("A grant must increase an allowance")
            budget = Budget(
                limits=limits,
                research_spent=current.budget.research_spent,
                review_spent=current.budget.review_spent,
            )
            checkpoint = current.model_copy(update={"budget": budget})
            return self._save(
                conn,
                row,
                checkpoint,
                "budget_granted",
                {
                    "before": old.model_dump(),
                    "after": limits.model_dump(),
                    "requested": requested_limits.model_dump(),
                    "lifetime_tokens": ceiling,
                },
            )

    def plan_step(
        self,
        identifier: UUID,
        expected_revision: int,
        request: StepRequest,
    ) -> Investigation:
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            existing = self._step(conn, identifier, request.key)
            if existing is not None:
                if existing.request != request or existing.brief_version != current.brief_version:
                    raise ValueError("Operation key already belongs to different work")
                return self._read(conn, identifier)
            if current.phase not in ACTIVE:
                raise ValueError("Steps can only start in an active phase")
            if (request.lane == Lane.REVIEW) != (current.phase == Phase.REVIEWING):
                raise ValueError(
                    "Review steps require reviewing; research steps require a research phase"
                )
            self._require_no_pending(conn, identifier)
            if request.reserved_tokens > current.budget.remaining(request.lane):
                raise ValueError("Step exceeds remaining token allowance")
            if request.kind == KIND:
                investigation, _, state = self._answer_state(conn, identifier)
                operation = Operation.model_validate(request.inputs.get("operation"))
                if (
                    operation != state.next_operation
                    or request.key != operation.key
                    or request.lane != Lane.REVIEW
                ):
                    raise ValueError("Answer-loop intent must match the next durable decision")
                if operation.action != "carry_forward":
                    execution = Execution.model_validate(request.inputs.get("execution"))
                    for saved in investigation.steps:
                        if (
                            saved.request.kind == KIND
                            and saved.request.inputs["operation"]["snapshot_id"]
                            == str(operation.snapshot_id)
                            and saved.request.inputs.get("execution") is not None
                        ):
                            pinned = Execution.model_validate(saved.request.inputs["execution"])
                            if pinned.models != execution.models:
                                raise ValueError(
                                    "Role models are pinned for the snapshot answer loop"
                                )
                            break
                    expected_role = {
                        "write": "author",
                        "revise": "author",
                        "review": "reviewer",
                        "adjudicate": "adjudicator",
                    }[operation.action]
                    if (
                        execution.role != expected_role
                        or request.reserved_tokens
                        < execution.limits.max_prompt_bytes + execution.limits.max_output_tokens
                    ):
                        raise ValueError("Role intent must reserve its bounded call allowance")
            step = Step(brief_version=current.brief_version, request=request)
            conn.execute(
                insert(db.steps).values(
                    investigation_id=identifier,
                    key=request.key,
                    brief_version=current.brief_version,
                    payload=step.model_dump(mode="json"),
                )
            )
            return self._save(conn, row, current, "step_planned", {"key": request.key})

    def finish_step(
        self,
        identifier: UUID,
        expected_revision: int,
        key: str,
        result: StepResult,
        *,
        attempt_id: UUID | None = None,
    ) -> Investigation:
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            step = self._step(conn, identifier, key)
            if step is None:
                raise ValueError("Unknown operation key")
            if step.result is not None:
                if step.result != result:
                    raise ValueError("A committed result cannot be replaced")
                return self._read(conn, identifier)
            ended_attempt = None
            if step.request.kind == KIND:
                history = review_attempts(self._read(conn, identifier))
                pending = next((a for a in history if a.state == "started" and a.key == key), None)
                if pending is not None and attempt_id != pending.id:
                    raise ValueError("Owned pending attempt is required to commit this result")
                if attempt_id is not None:
                    attempt = self._pending_attempt(conn, identifier, attempt_id)
                    if attempt.key != key:
                        raise ValueError("Attempt belongs to a different operation")
                    usage = CALL.validate_python(result.output["call"]).usage
                    ended_attempt = attempt.model_copy(
                        update={
                            "state": "succeeded",
                            "finished_at": self._now(conn),
                            "usage": usage,
                            "charged_tokens": result.used_tokens,
                            "charge_basis": "reported",
                            "estimated_usd": attempt.prices.cost(usage) if attempt.prices else None,
                        }
                    )
                _, context, state = self._answer_state(conn, identifier)
                operation = Operation.model_validate(step.request.inputs["operation"])
                if operation != state.next_operation:
                    raise ValueError("Answer-loop result does not match the pending decision")
                validate_result(
                    operation,
                    result,
                    state,
                    context,
                    Execution.model_validate(step.request.inputs["execution"])
                    if operation.action != "carry_forward"
                    else None,
                )
            # Record real usage even when the provider exceeds its reservation.
            budget = current.budget.model_dump()
            counter = "research_spent" if step.request.lane == Lane.RESEARCH else "review_spent"
            budget[counter] += result.used_tokens
            updated_budget = Budget.model_validate(budget)
            checkpoint = current.model_copy(update={"budget": updated_budget})
            if updated_budget.remaining(step.request.lane) == 0 and current.phase in ACTIVE:
                checkpoint = transition(checkpoint, Phase.STOPPED_AT_LIMIT, reason="budget")
                if step.request.kind == KIND:
                    checkpoint = checkpoint.model_copy(update={"resume_phase": Phase.REVIEWING})
            conn.execute(
                update(db.steps)
                .where(
                    db.steps.c.investigation_id == identifier,
                    db.steps.c.key == key,
                )
                .values(payload=step.model_copy(update={"result": result}).model_dump(mode="json"))
            )
            return self._save(
                conn,
                row,
                checkpoint,
                "step_finished",
                {
                    "key": key,
                    "status": result.status.value,
                    "used_tokens": result.used_tokens,
                    **(
                        {"review_attempt": ended_attempt.model_dump(mode="json")}
                        if ended_attempt
                        else {}
                    ),
                },
            )

    def ingest_text(
        self, identifier: UUID, expected_revision: int, request: IngestText
    ) -> EvidenceInput:
        request = IngestText.model_validate(request.model_dump())
        identity = ingest_identity(request)
        digest = fingerprint(identity)
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            existing = (
                conn.execute(
                    select(db.evidence_inputs).where(
                        db.evidence_inputs.c.investigation_id == identifier,
                        db.evidence_inputs.c.operation_key == request.operation_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["request"] != identity
                    or existing["fingerprint"] != digest
                    or existing["brief_version"] != current.brief_version
                ):
                    raise ValueError("Operation key already belongs to different input")
                observation = EvidenceInput.model_validate(existing["payload"])
                if observation.version_id is not None:
                    version = self._read_source_version(conn, observation.version_id)
                    if version.text != request.text:
                        raise ValueError("Content hash collision")
                return observation
            self._require_evidence_mutation(conn, identifier, current)
            reason = (
                "empty_document"
                if not request.text
                else "document_too_large"
                if identity["byte_count"] > request.max_bytes
                else None
            )
            version_id = None
            if reason is None:
                conn.execute(
                    pg_insert(db.sources)
                    .values(
                        id=uuid4(),
                        document_key=request.document_key,
                        title=request.title,
                        origin_url=request.origin_url,
                    )
                    .on_conflict_do_nothing(index_elements=[db.sources.c.document_key])
                )
                source = (
                    conn.execute(
                        select(db.sources)
                        .where(db.sources.c.document_key == request.document_key)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if source["title"] != request.title or source["origin_url"] != request.origin_url:
                    raise ValueError("Existing document key has different title or origin")
                version = (
                    conn.execute(
                        select(db.source_versions).where(
                            db.source_versions.c.source_id == source["id"],
                            db.source_versions.c.content_hash == identity["content_hash"],
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if version is not None:
                    if version["text"] != request.text:
                        raise ValueError("Content hash collision")
                    version_id = version["id"]
                else:
                    version_id = uuid4()
                    conn.execute(
                        insert(db.source_versions).values(
                            id=version_id,
                            source_id=source["id"],
                            text=request.text,
                            content_hash=identity["content_hash"],
                            byte_count=identity["byte_count"],
                            codepoint_count=identity["codepoint_count"],
                            partition_version=1,
                        )
                    )
                    conn.execute(
                        insert(db.passages),
                        [
                            {"id": uuid4(), "version_id": version_id, "start": start, "end": end}
                            for start, end in passage_ranges(request.text)
                        ],
                    )
            observation = EvidenceInput(
                investigation_id=identifier,
                operation_key=request.operation_key,
                brief_version=current.brief_version,
                revision=row["revision"] + 1,
                document_key=request.document_key,
                title=request.title,
                origin_url=request.origin_url,
                observed_at=request.observed_at,
                published_at=request.published_at,
                content_hash=identity["content_hash"],
                byte_count=identity["byte_count"],
                codepoint_count=identity["codepoint_count"],
                max_bytes=request.max_bytes,
                status="accepted" if reason is None else "rejected",
                version_id=version_id,
                reason=reason,
            )
            conn.execute(
                insert(db.evidence_inputs).values(
                    investigation_id=identifier,
                    operation_key=request.operation_key,
                    brief_version=current.brief_version,
                    fingerprint=digest,
                    request=identity,
                    payload=observation.model_dump(mode="json"),
                    version_id=version_id,
                    status=observation.status,
                )
            )
            self._save(
                conn,
                row,
                current,
                "evidence_ingested",
                {
                    "operation_key": request.operation_key,
                    "status": observation.status,
                    "version_id": str(version_id) if version_id else None,
                    "reason": reason,
                },
            )
            return observation

    def freeze_snapshot(
        self, identifier: UUID, expected_revision: int, request: FreezeSnapshot
    ) -> LoadedSnapshot:
        request = FreezeSnapshot.model_validate(request.model_dump())
        identity = request.model_dump(mode="json")
        digest = fingerprint(identity)
        with self.engine.begin() as conn:
            row, current = self._lock(conn, identifier, expected_revision)
            existing = (
                conn.execute(
                    select(db.evidence_snapshots).where(
                        db.evidence_snapshots.c.investigation_id == identifier,
                        db.evidence_snapshots.c.operation_key == request.operation_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if (
                    existing["request"] != identity
                    or existing["fingerprint"] != digest
                    or existing["brief_version"] != current.brief_version
                ):
                    raise ValueError("Operation key already belongs to a different snapshot")
                return self._read_snapshot(conn, existing["id"])
            self._require_evidence_mutation(conn, identifier, current)
            observations = tuple(
                EvidenceInput.model_validate(payload)
                for payload in conn.execute(
                    select(db.evidence_inputs.c.payload)
                    .where(
                        db.evidence_inputs.c.investigation_id == identifier,
                    )
                    .order_by(db.evidence_inputs.c.operation_key)
                ).scalars()
            )
            candidates = []
            requested_bytes = 0
            for selection in request.candidates:
                keys = tuple(
                    o.operation_key for o in observations if o.version_id == selection.version_id
                )
                if not keys:
                    raise ValueError("Candidate version was not acquired by this investigation")
                version = self._read_source_version(conn, selection.version_id)
                dispositions = select_passages(selection, version)
                requested_bytes += sum(
                    len(version.text[p.start : p.end].encode("utf-8"))
                    for p in dispositions
                    if p.included
                )
                candidates.append(
                    SnapshotCandidate(
                        version_id=version.id,
                        observation_keys=keys,
                        passages=dispositions,
                    )
                )
            reason = (
                "snapshot_too_large"
                if requested_bytes > request.max_bytes
                else "empty_selection"
                if requested_bytes == 0
                else None
            )
            if reason == "snapshot_too_large":
                candidates = [
                    c.model_copy(
                        update={
                            "passages": tuple(
                                p.model_copy(update={"included": False, "reason": reason})
                                if p.included
                                else p
                                for p in c.passages
                            )
                        }
                    )
                    for c in candidates
                ]
            snapshot = EvidenceSnapshot(
                id=uuid4(),
                investigation_id=identifier,
                operation_key=request.operation_key,
                brief_version=current.brief_version,
                revision=row["revision"] + 1,
                status="ready" if reason is None else "limited",
                reason=reason,
                candidates=tuple(candidates),
                rejected_inputs=tuple(
                    o
                    for o in observations
                    if o.status == "rejected" and o.brief_version == current.brief_version
                ),
                requested_bytes=requested_bytes,
                included_bytes=requested_bytes if reason is None else 0,
                max_bytes=request.max_bytes,
            )
            conn.execute(
                insert(db.evidence_snapshots).values(
                    id=snapshot.id,
                    investigation_id=identifier,
                    operation_key=request.operation_key,
                    brief_version=current.brief_version,
                    format_version=1,
                    status=snapshot.status,
                    fingerprint=digest,
                    request=identity,
                    manifest=snapshot.model_dump(mode="json"),
                )
            )
            if candidates:
                conn.execute(
                    insert(db.snapshot_versions),
                    [{"snapshot_id": snapshot.id, "version_id": c.version_id} for c in candidates],
                )
            self._save(
                conn,
                row,
                current,
                "snapshot_frozen",
                {
                    "snapshot_id": str(snapshot.id),
                    "status": snapshot.status,
                },
            )
            return self._read_snapshot(conn, snapshot.id)

    def load_source_version(self, version_id: UUID) -> SourceVersion:
        with self.engine.begin() as conn:
            return self._read_source_version(conn, version_id)

    def load_snapshot(self, snapshot_id: UUID) -> LoadedSnapshot:
        with self.engine.begin() as conn:
            return self._read_snapshot(conn, snapshot_id)

    def _require_evidence_mutation(self, conn, identifier, current):
        if current.phase not in {Phase.SCOPE, Phase.PLANNING, Phase.GATHERING, Phase.ASSESSING}:
            raise ValueError("Evidence changes require an active research phase")
        self._require_no_pending(conn, identifier)

    @staticmethod
    def _read_source_version(conn, version_id):
        version = (
            conn.execute(
                select(
                    db.source_versions,
                    db.sources.c.document_key,
                    db.sources.c.title,
                    db.sources.c.origin_url,
                )
                .join(db.sources, db.source_versions.c.source_id == db.sources.c.id)
                .where(db.source_versions.c.id == version_id)
            )
            .mappings()
            .one_or_none()
        )
        if version is None:
            raise KeyError(version_id)
        passages = tuple(
            Passage.model_validate(dict(p))
            for p in conn.execute(
                select(db.passages)
                .where(db.passages.c.version_id == version_id)
                .order_by(db.passages.c.start)
            ).mappings()
        )
        return SourceVersion(**version, passages=passages)

    def _read_snapshot(self, conn, snapshot_id):
        payload = conn.execute(
            select(db.evidence_snapshots.c.manifest).where(
                db.evidence_snapshots.c.id == snapshot_id,
            )
        ).scalar_one_or_none()
        if payload is None:
            raise KeyError(snapshot_id)
        snapshot = EvidenceSnapshot.model_validate(payload)
        observations = []
        versions = []
        for candidate in snapshot.candidates:
            versions.append(self._read_source_version(conn, candidate.version_id))
            for key in candidate.observation_keys:
                observation = conn.execute(
                    select(db.evidence_inputs.c.payload).where(
                        db.evidence_inputs.c.investigation_id == snapshot.investigation_id,
                        db.evidence_inputs.c.operation_key == key,
                        db.evidence_inputs.c.version_id == candidate.version_id,
                    )
                ).scalar_one()
                observations.append(EvidenceInput.model_validate(observation))
        return LoadedSnapshot(
            snapshot=snapshot, versions=tuple(versions), observations=tuple(observations)
        )

    @staticmethod
    def _lock(conn: Connection, identifier: UUID, expected: int | None = None, *, read=False):
        row = (
            conn.execute(
                select(db.investigations)
                .where(
                    db.investigations.c.id == identifier,
                )
                .with_for_update(read=read)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise KeyError(identifier)
        if expected is not None and row["revision"] != expected:
            raise StaleRevision(
                f"Expected revision {expected}; current revision is {row['revision']}"
            )
        return row, Checkpoint.model_validate(row["checkpoint"])

    @staticmethod
    def _step(conn: Connection, identifier: UUID, key: str) -> Step | None:
        payload = conn.execute(
            select(db.steps.c.payload).where(
                db.steps.c.investigation_id == identifier,
                db.steps.c.key == key,
            )
        ).scalar_one_or_none()
        return Step.model_validate(payload) if payload is not None else None

    @staticmethod
    def _require_no_pending(conn: Connection, identifier: UUID) -> None:
        payloads = conn.execute(
            select(db.steps.c.payload).where(
                db.steps.c.investigation_id == identifier,
            )
        ).scalars()
        if any(Step.model_validate(payload).result is None for payload in payloads):
            raise ValueError("Reconcile the pending step before starting different work")

    def _save(self, conn, row, checkpoint, kind, details) -> Investigation:
        revision = row["revision"] + 1
        conn.execute(
            update(db.investigations)
            .where(db.investigations.c.id == row["id"])
            .values(
                revision=revision,
                checkpoint=checkpoint.model_dump(mode="json"),
            )
        )
        conn.execute(
            insert(db.events).values(
                investigation_id=row["id"],
                revision=revision,
                kind=kind,
                details=details,
            )
        )
        return self._read(conn, row["id"])

    @staticmethod
    def _read(conn: Connection, identifier: UUID) -> Investigation:
        row = (
            conn.execute(
                select(db.investigations).where(
                    db.investigations.c.id == identifier,
                )
            )
            .mappings()
            .one()
        )
        brief_rows = conn.execute(
            select(db.briefs)
            .where(
                db.briefs.c.investigation_id == identifier,
            )
            .order_by(db.briefs.c.version)
        ).mappings()
        step_rows = conn.execute(
            select(db.steps.c.payload)
            .where(
                db.steps.c.investigation_id == identifier,
            )
            .order_by(db.steps.c.created_at, db.steps.c.key)
        ).scalars()
        event_rows = conn.execute(
            select(db.events)
            .where(
                db.events.c.investigation_id == identifier,
            )
            .order_by(db.events.c.revision)
        ).mappings()
        return Investigation(
            id=identifier,
            revision=row["revision"],
            checkpoint=row["checkpoint"],
            briefs=tuple(
                BriefVersion(version=r["version"], brief=r["payload"]) for r in brief_rows
            ),
            steps=tuple(Step.model_validate(p) for p in step_rows),
            events=tuple(
                Event(revision=r["revision"], kind=r["kind"], details=r["details"])
                for r in event_rows
            ),
        )
