"""Transactional investigation storage; no model calls or workflow execution."""

from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from query import db
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


class StaleRevision(ValueError):
    """The caller must reload before applying a decision to newer state."""


class InvestigationStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def create(self, brief: Brief, limits: Limits) -> Investigation:
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
                    details={},
                )
            )
            return self._read(conn, identifier)

    def load(self, identifier: UUID) -> Investigation:
        with self.engine.begin() as conn:
            self._lock(conn, identifier, read=True)
            return self._read(conn, identifier)

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
            # Record real usage even when the provider exceeds its reservation.
            budget = current.budget.model_dump()
            counter = "research_spent" if step.request.lane == Lane.RESEARCH else "review_spent"
            budget[counter] += result.used_tokens
            updated_budget = Budget.model_validate(budget)
            checkpoint = current.model_copy(update={"budget": updated_budget})
            if updated_budget.remaining(step.request.lane) == 0 and current.phase in ACTIVE:
                checkpoint = transition(checkpoint, Phase.STOPPED_AT_LIMIT, reason="budget")
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
                },
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
