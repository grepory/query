from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from query.db import metadata
from query.investigation import (
    Brief,
    CompletionChecks,
    Lane,
    Limits,
    Outcome,
    Phase,
    Question,
    StepRequest,
    StepResult,
    StepStatus,
)
from query.store import InvestigationStore, StaleRevision


@pytest.fixture
def store(database):
    return InvestigationStore(database[0])


@pytest.fixture
def brief():
    return Brief(
        question="How much capacity is operational?",
        scope="Supplied public documents",
        cutoff=date(2026, 10, 20),
        questions=(
            Question(
                key="operational",
                text="What is operational?",
                acceptance_criteria="Dated operational watts",
            ),
        ),
    )


@pytest.fixture
def run(store, brief):
    return store.create(brief, Limits(research=100, review=100, total=150))


def request(key="extract-1", tokens=40):
    return StepRequest(
        key=key,
        kind="extract",
        lane=Lane.RESEARCH,
        reserved_tokens=tokens,
        inputs={"source": "document-1"},
    )


def result(tokens=20):
    return StepResult(status=StepStatus.SUCCEEDED, used_tokens=tokens, output={"capacity": 100})


def test_migration_roundtrip_and_schema_matches(database):
    engine, config = database
    with engine.begin() as conn:
        assert compare_metadata(MigrationContext.configure(conn), metadata) == []
        config.attributes["connection"] = conn
        command.downgrade(config, "base")
        assert inspect(conn).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        assert "investigations" in inspect(conn).get_table_names()


def test_roundtrip_and_versioned_brief(store, run, brief):
    updated = store.revise_brief(
        run.id, run.revision, brief.model_copy(update={"cutoff": date(2026, 10, 21)})
    )
    store.engine.dispose()
    restored = InvestigationStore(store.engine).load(run.id)
    assert restored == updated
    assert [b.brief.cutoff for b in restored.briefs] == [date(2026, 10, 20), date(2026, 10, 21)]
    assert restored.checkpoint.brief_version == 2
    assert [e.kind for e in restored.events] == ["created", "brief_revised"]


def test_stale_and_invalid_transitions_leave_state_unchanged(store, run):
    planned = store.move(run.id, run.revision, Phase.PLANNING)
    with pytest.raises(StaleRevision):
        store.move(run.id, run.revision, Phase.PLANNING)
    with pytest.raises(ValueError, match="Cannot move"):
        store.move(run.id, planned.revision, Phase.COMPLETE)
    assert store.load(run.id) == planned


def test_two_writers_cannot_apply_decisions_to_same_revision(store, run):
    def attempt():
        try:
            store.move(run.id, run.revision, Phase.PLANNING)
            return "committed"
        except StaleRevision:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == ["committed", "stale"]
    assert store.load(run.id).revision == run.revision + 1


def test_restart_reconciles_pending_step_without_double_charge(store, run):
    pending = store.plan_step(run.id, run.revision, request())
    restarted = InvestigationStore(store.engine)
    assert restarted.load(run.id).steps[0].result is None
    assert restarted.plan_step(run.id, pending.revision, request()) == pending
    finished = restarted.finish_step(run.id, pending.revision, "extract-1", result())
    assert finished.checkpoint.budget.research_spent == 20
    assert restarted.finish_step(run.id, finished.revision, "extract-1", result()) == finished
    with pytest.raises(ValueError, match="cannot be replaced"):
        restarted.finish_step(run.id, finished.revision, "extract-1", result(30))


def test_pending_step_prevents_different_work(store, run, brief):
    pending = store.plan_step(run.id, run.revision, request())
    with pytest.raises(ValueError, match="pending step"):
        store.plan_step(run.id, pending.revision, request("extract-2"))
    with pytest.raises(ValueError, match="pending step"):
        store.revise_brief(run.id, pending.revision, brief)
    with pytest.raises(ValueError, match="different work"):
        store.plan_step(run.id, pending.revision, request(tokens=50))
    assert store.load(run.id) == pending


def test_budget_admission_and_actual_overrun_are_durable(store, run):
    with pytest.raises(ValueError, match="allowance"):
        store.plan_step(run.id, run.revision, request(tokens=101))
    pending = store.plan_step(run.id, run.revision, request())
    stopped = store.finish_step(run.id, pending.revision, "extract-1", result(160))
    assert stopped.checkpoint.phase == Phase.STOPPED_AT_LIMIT
    assert stopped.checkpoint.budget.total_spent == 160
    assert stopped.checkpoint.outcome is None
    with pytest.raises(ValueError, match="allowance"):
        store.resume(run.id, stopped.revision)
    granted = store.grant(run.id, stopped.revision, Limits(research=200, review=100, total=300))
    resumed = store.resume(run.id, granted.revision)
    assert resumed.checkpoint.phase == Phase.PLANNING
    assert resumed.checkpoint.budget.total_spent == 160


def test_failed_steps_account_usage_and_keep_error(store, run):
    pending = store.plan_step(run.id, run.revision, request())
    failed = StepResult(status=StepStatus.FAILED, used_tokens=7, output={}, error="Invalid output")
    done = store.finish_step(run.id, pending.revision, "extract-1", failed)
    assert done.steps[0].result == failed
    assert done.checkpoint.budget.total_spent == 7


def test_pause_and_resume_preserve_pending_operation(store, run):
    pending = store.plan_step(run.id, run.revision, request())
    paused = store.move(run.id, pending.revision, Phase.PAUSED, reason="provider unavailable")
    restored = store.resume(run.id, paused.revision)
    assert restored.checkpoint.phase == Phase.SCOPE
    assert restored.steps == pending.steps


def test_completion_requires_current_brief_and_both_checks(store, run):
    planning = store.move(run.id, run.revision, Phase.PLANNING)
    reviewing = store.move(run.id, planning.revision, Phase.REVIEWING)
    checks = CompletionChecks(
        brief_version=1,
        answer_ref="draft-1",
        answer_integrity=True,
        research_sufficiency=False,
        rationale="Primary source unread",
    )
    with pytest.raises(ValueError, match="Both completion"):
        store.move(
            run.id, reviewing.revision, Phase.COMPLETE, outcome=Outcome.QUALIFIED, completion=checks
        )
    checks = checks.model_copy(
        update={"research_sufficiency": True, "rationale": "All required questions assessed"}
    )
    done = store.move(
        run.id, reviewing.revision, Phase.COMPLETE, outcome=Outcome.UNRESOLVED, completion=checks
    )
    assert done.checkpoint.outcome == Outcome.UNRESOLVED
    with pytest.raises(ValueError):
        store.resume(run.id, done.revision)
    assert store.load(run.id) == done


def test_cancellation_is_terminal_but_inflight_usage_can_be_reconciled(store, run):
    pending = store.plan_step(run.id, run.revision, request())
    canceled = store.move(run.id, pending.revision, Phase.CANCELED, reason="user canceled")
    reconciled = store.finish_step(run.id, canceled.revision, "extract-1", result())
    assert reconciled.checkpoint.phase == Phase.CANCELED
    assert reconciled.checkpoint.budget.total_spent == 20
    with pytest.raises(ValueError, match="active phase"):
        store.plan_step(run.id, reconciled.revision, request("new-step"))


def test_step_result_and_usage_rollback_together(store, run, monkeypatch):
    pending = store.plan_step(run.id, run.revision, request())

    def fail_commit(*args, **kwargs):
        raise RuntimeError("simulated crash before checkpoint commit")

    monkeypatch.setattr(store, "_save", fail_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.finish_step(run.id, pending.revision, "extract-1", result())
    assert store.load(run.id) == pending


def test_review_steps_use_separate_allowance(store, run):
    review_request = StepRequest(
        key="review-1",
        kind="review",
        lane=Lane.REVIEW,
        reserved_tokens=30,
        inputs={"draft": "draft-1"},
    )
    with pytest.raises(ValueError, match="require reviewing"):
        store.plan_step(run.id, run.revision, review_request)
    planning = store.move(run.id, run.revision, Phase.PLANNING)
    reviewing = store.move(run.id, planning.revision, Phase.REVIEWING)
    with pytest.raises(ValueError, match="research phase"):
        store.plan_step(run.id, reviewing.revision, request())
    pending = store.plan_step(run.id, reviewing.revision, review_request)
    done = store.finish_step(run.id, pending.revision, "review-1", result())
    assert done.checkpoint.budget.review_spent == 20
    assert done.checkpoint.budget.research_spent == 0


def test_brief_revision_resets_work_but_retains_spend(store, run, brief):
    pending = store.plan_step(run.id, run.revision, request())
    done = store.finish_step(run.id, pending.revision, "extract-1", result())
    waiting = store.move(run.id, done.revision, Phase.AWAITING_INPUT, reason="Which cutoff?")
    revised = store.revise_brief(run.id, waiting.revision, brief)
    assert revised.checkpoint.phase == Phase.SCOPE
    assert revised.checkpoint.budget.research_spent == 20
    assert revised.steps[0].brief_version == 1
    with pytest.raises(ValueError, match="different work"):
        store.plan_step(run.id, revised.revision, request())


def test_grant_cannot_erase_usage_or_shrink_limits(store, run):
    with pytest.raises(ValueError, match="reduce"):
        store.grant(run.id, run.revision, Limits(research=50, review=100, total=150))
    assert store.load(run.id) == run


@pytest.mark.parametrize("phase", [Phase.PAUSED, Phase.PARKED, Phase.STOPPED_AT_LIMIT])
def test_brief_revision_preserves_hold_until_explicit_resume(store, run, brief, phase):
    reason = "budget" if phase == Phase.STOPPED_AT_LIMIT else "user decision"
    held = store.move(run.id, run.revision, phase, reason=reason)
    revised = store.revise_brief(run.id, held.revision, brief)
    assert revised.checkpoint.phase == phase
    assert revised.checkpoint.stop_reason == reason
    assert revised.checkpoint.resume_phase == Phase.SCOPE
    assert store.load(run.id) == revised
    with pytest.raises(ValueError, match="active phase"):
        store.plan_step(run.id, revised.revision, request())
    resumed = store.resume(run.id, revised.revision)
    assert resumed.checkpoint.phase == Phase.SCOPE
    assert resumed.checkpoint.brief_version == 2


def test_brief_revision_cannot_bypass_exhausted_budget_stop(store, run, brief):
    pending = store.plan_step(run.id, run.revision, request())
    stopped = store.finish_step(run.id, pending.revision, "extract-1", result(160))
    revised = store.revise_brief(run.id, stopped.revision, brief)
    assert revised.checkpoint.phase == Phase.STOPPED_AT_LIMIT
    assert revised.checkpoint.budget.total_spent == 160
    with pytest.raises(ValueError, match="allowance"):
        store.resume(run.id, revised.revision)
    assert store.load(run.id) == revised
