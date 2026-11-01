"""NOT-40 fault, ownership, closeout and cumulative-accounting regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event

import pytest
from sqlalchemy import func, select

from query.answer_loop import AnswerController
from query.investigation import Limits, Phase
from query.recovery import Prices, ReviewPolicy
from query.review_clients import CallUsage, RoleCallError
from query.store import InvestigationStore, StaleRevision
from tests.integration.test_answer_loop import ScriptedClients, advance
from tests.integration.test_answer_loop import loop as loop


def drive(store, identifier, clients, policy=None):
    return AnswerController(store, clients, policy=policy).advance(
        identifier, store.load(identifier).revision
    )


def at_due(store, identifier):
    due = store.export(identifier).review_attempts[-1].retry_at
    if due:
        store.clock = lambda: due
    store.resume(identifier, store.load(identifier).revision)


def test_closeout_is_committed_before_fresh_adjudication_and_survives_failure_and_grant(loop):
    store, identifier, clients, _ = loop("closeout-survives-failure-grant")
    policy = ReviewPolicy(closeout_remaining_tokens=2000000)
    drive(store, identifier, clients, policy)
    advance(store, identifier, clients)
    observed = []
    clients.before_call = lambda: observed.append(store.load(identifier))
    clients.fail = True
    state = drive(store, identifier, clients)
    assert state.closeout and state.lifecycle_phase == Phase.PAUSED
    assert state.next_operation.action == "adjudicate"
    assert clients.calls[-1][4] is None  # Fresh issue bypasses author response in closeout.
    events = observed[0].events
    closeout = next(e for e in events if e.kind == "review_closeout")
    plan = [e for e in events if e.kind == "step_planned"][-1]
    started = [e for e in events if e.kind == "review_attempt_started"][-1]
    assert closeout.revision < plan.revision < started.revision
    prior_spend = store.load(identifier).checkpoint.budget.total_spent
    current = store.load(identifier)
    granted = store.grant(
        identifier, current.revision, Limits(research=200, review=3000000, total=4000000)
    )
    assert granted.checkpoint.budget.limits.total == 2000100  # Immutable initial ceiling.
    assert granted.checkpoint.budget.total_spent == prior_spend
    at_due(store, identifier)
    clients.fail = False
    state = drive(store, identifier, clients)
    assert state.closeout and len(state.rulings) == 1
    assert store.export(identifier).review_attempts[-1].number == 2
    assert len([e for e in store.load(identifier).events if e.kind == "review_closeout"]) == 1


@pytest.mark.parametrize("case_id", ["hard-limit-preserves-reviewed", "final-review-unaffordable"])
def test_reserve_parks_before_paying_and_preserves_last_reviewed_answer(loop, case_id):
    store, identifier, clients, _ = loop(case_id)
    state = advance(store, identifier, clients, 2)
    before = store.export(identifier).last_reviewed_answer
    if case_id == "final-review-unaffordable":
        state = drive(store, identifier, clients)  # Save paid revision, still unreviewed.
        assert state.next_operation.action == "review"
    calls = len(clients.calls)
    clients.limits = clients.limits.model_copy(update={"max_prompt_bytes": 3000000})
    held = drive(store, identifier, clients)
    assert held.lifecycle_phase == Phase.PARKED
    assert store.load(identifier).checkpoint.stop_reason == "review_reserve"
    assert len(clients.calls) == calls
    assert store.export(identifier).last_reviewed_answer == before
    assert held.current_answer == before
    assert store.load(identifier).checkpoint.completion is None


@pytest.mark.parametrize(
    "kind,retryable,usage,basis,charged",
    [
        (
            "provider_unavailable",
            True,
            CallUsage(requests=1, input_tokens=7, output_tokens=3),
            "reported",
            10,
        ),
        ("provider_unavailable", True, None, "reservation", 33024),
        ("provider_rejected", False, None, "reservation", 33024),
        ("context_limit", False, None, "not_called", 0),
        (
            "invalid_output",
            False,
            CallUsage(requests=1, input_tokens=9, output_tokens=2),
            "reported",
            11,
        ),
    ],
)
def test_failures_hold_and_account_known_or_unknown_usage_once(
    loop, monkeypatch, kind, retryable, usage, basis, charged
):
    store, identifier, clients, _ = loop("permanent-failure-parks")
    advance(store, identifier, clients, 2)

    def fail(*args, **kwargs):
        raise RoleCallError("author", kind, retryable=retryable, usage=usage)

    monkeypatch.setattr(clients, "revise", fail)
    prior = store.load(identifier).checkpoint.budget.total_spent
    reviewed = store.export(identifier).last_reviewed_answer
    state = drive(store, identifier, clients)
    expected_phase = Phase.PAUSED if retryable else Phase.PARKED
    assert state.lifecycle_phase == expected_phase
    attempt = store.export(identifier).review_attempts[-1]
    assert attempt.state == "failed" and attempt.usage == usage
    assert attempt.charged_tokens == charged and attempt.charge_basis == basis
    assert store.load(identifier).checkpoint.budget.total_spent == prior + charged
    assert store.export(identifier).last_reviewed_answer == reviewed
    with pytest.raises(ValueError, match="resume"):
        drive(store, identifier, clients)
    assert store.load(identifier).checkpoint.budget.total_spent == prior + charged
    assert store.load(identifier).steps[-1].result is None


def test_due_time_exponential_backoff_and_stale_resume_prevent_calls(loop):
    store, identifier, clients, _ = loop("not-due-retry")
    clients.fail = True
    drive(store, identifier, clients)
    attempt = store.export(identifier).review_attempts[-1]
    assert attempt.retry_at - attempt.finished_at == timedelta(seconds=60)
    with pytest.raises(ValueError, match="not due"):
        store.resume(identifier, store.load(identifier).revision)
    calls = len(clients.calls)
    old_revision = store.load(identifier).revision
    at_due(store, identifier)
    with pytest.raises(StaleRevision):
        AnswerController(store, clients).advance(identifier, old_revision)
    assert len(clients.calls) == calls
    drive(store, identifier, clients)
    second = store.export(identifier).review_attempts[-1]
    assert second.number == 2
    assert second.retry_at - second.finished_at == timedelta(seconds=120)
    assert second.key == attempt.key


@pytest.mark.parametrize("boundary", ["before_attempt", "before_commit", "after_commit"])
def test_crash_boundaries_resume_without_replaying_committed_work(loop, monkeypatch, boundary):
    store, identifier, clients, _ = loop(
        "review-pending-revision" if boundary == "after_commit" else "resume-remote-outcome-unknown"
    )
    if boundary == "before_attempt":
        original = store.begin_answer_attempt
        monkeypatch.setattr(
            store,
            "begin_answer_attempt",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")),
        )
    else:
        original = store.finish_step

        def finish(*args, **kwargs):
            if boundary == "after_commit":
                original(*args, **kwargs)
            raise RuntimeError("crash")

        monkeypatch.setattr(store, "finish_step", finish)
    with pytest.raises(RuntimeError, match="crash"):
        drive(store, identifier, clients)
    monkeypatch.setattr(
        store, "begin_answer_attempt" if boundary == "before_attempt" else "finish_step", original
    )
    if boundary == "before_attempt":
        assert store.export(identifier).review_attempts == ()
        assert clients.calls == []
        state = drive(store, identifier, clients)
        assert state.next_operation.action == "review"
    elif boundary == "after_commit":
        assert len(store.answer_state(identifier)[2].answers) == 1
        assert store.export(identifier).review_attempts[-1].state == "succeeded"
        # Stop before reviewer: demonstrate that reconstruction has no author replay.
        assert store.answer_state(identifier)[2].next_operation.action == "review"
        assert len(clients.calls) == 1
        drive(store, identifier, clients)
        assert [c[0] for c in clients.calls] == ["write", "review"]
        assert store.load(identifier).checkpoint.budget.review_spent == 30
    else:
        assert store.export(identifier).review_attempts[-1].state == "started"
        calls = len(clients.calls)
        state = drive(store, identifier, clients)
        assert state.lifecycle_phase == Phase.PARKED
        attempt = store.export(identifier).review_attempts[-1]
        assert attempt.state == "unknown" and attempt.usage is None
        assert attempt.charged_tokens == 33024
        assert len(clients.calls) == calls  # Reconciliation itself cannot retry remotely.
        store.resume(identifier, store.load(identifier).revision)
        drive(store, identifier, clients)
        assert len(clients.calls) == calls + 1  # Explicit retry can repeat remote billing.
        assert len(store.load(identifier).steps) == 1


def test_unrelated_programming_error_propagates_then_reconciles_as_uncertain(loop, monkeypatch):
    store, identifier, clients, _ = loop("resume-remote-outcome-unknown")
    monkeypatch.setattr(clients, "write", lambda *a, **k: (_ for _ in ()).throw(TypeError("bug")))
    with pytest.raises(TypeError, match="bug"):
        drive(store, identifier, clients)
    state = drive(store, identifier, clients)
    assert state.lifecycle_phase == Phase.PARKED
    assert store.export(identifier).review_attempts[-1].error_kind == "remote_outcome_unknown"


def test_two_independent_executors_cannot_call_concurrently(loop):
    store, identifier, clients, _ = loop("stale-retry")
    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(5)

    clients.before_call = block
    competing = ScriptedClients(clients.corpus, clients.case)
    other = InvestigationStore(store.engine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(drive, store, identifier, clients)
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match="Another executor"):
                drive(other, identifier, competing)
            assert competing.calls == []
        finally:
            release.set()
        assert running.result(timeout=5).next_operation.action == "review"
    assert len(store.export(identifier).review_attempts) == 1


@pytest.mark.parametrize("phase", [Phase.PAUSED, Phase.CANCELED])
def test_inflight_success_keeps_concurrent_hold_or_cancel_and_records_paid_result(loop, phase):
    store, identifier, clients, _ = loop("stale-retry")

    def hold():
        current = store.load(identifier)
        store.move(identifier, current.revision, phase, reason="Concurrent user decision")

    clients.before_call = hold
    state = drive(store, identifier, clients)
    current = store.load(identifier)
    assert state.lifecycle_phase == phase == current.checkpoint.phase
    assert len(state.answers) == 1
    assert current.checkpoint.budget.review_spent == 15
    assert store.export(identifier).review_attempts[-1].state == "succeeded"


def test_crashed_canceled_attempt_can_reconcile_without_resuming_or_paid_io(loop, monkeypatch):
    store, identifier, clients, _ = loop("resume-remote-outcome-unknown")

    def crash(*args, **kwargs):
        current = store.load(identifier)
        store.move(identifier, current.revision, Phase.CANCELED, reason="Canceled during call")
        raise RuntimeError("crash")

    monkeypatch.setattr(clients, "write", crash)
    with pytest.raises(RuntimeError):
        drive(store, identifier, clients)
    state = drive(store, identifier, clients)
    assert state.lifecycle_phase == Phase.CANCELED
    assert store.export(identifier).review_attempts[-1].state == "unknown"
    assert store.load(identifier).checkpoint.budget.review_spent == 33024


def test_explicit_prices_record_assumptions_and_success_and_failed_costs(loop):
    store, identifier, clients, _ = loop("stale-retry")
    price = Prices(
        input_usd_per_million=Decimal("2"),
        output_usd_per_million=Decimal("5"),
        assumption="Synthetic test rates, not provider prices",
    )
    drive(store, identifier, clients, ReviewPolicy(prices={"author": price}))
    first = store.export(identifier).review_attempts[-1]
    assert first.prices == price
    assert first.estimated_usd == Decimal("0.000045")
    assert first.usage == CallUsage(requests=1, input_tokens=10, output_tokens=5)
    assert first.charged_tokens == 15


def test_persisted_carry_forward_does_not_hide_last_paid_revision_reserve(loop, monkeypatch):
    store, identifier, clients, _ = loop("upheld-same-scope")
    original_revise, original_adjudicate = clients.revise, clients.adjudicate

    def revise(*args, **kwargs):
        return original_revise(*args, **kwargs).model_copy(
            update={"usage": CallUsage(requests=1, input_tokens=600000, output_tokens=5)}
        )

    def adjudicate(*args, **kwargs):
        return original_adjudicate(*args, **kwargs).model_copy(
            update={"usage": CallUsage(requests=1, input_tokens=1200000, output_tokens=5)}
        )

    monkeypatch.setattr(clients, "revise", revise)
    monkeypatch.setattr(clients, "adjudicate", adjudicate)
    drive(store, identifier, clients, ReviewPolicy(closeout_remaining_tokens=300000))
    advance(store, identifier, clients, 5)  # Through paid ruling and free unchanged draft.

    def fresh(data, answer):
        issue = {
            **data["issues"][0],
            "id": "fresh-title",
            "claim_id": None,
            "claim_text": "Capacity",
            "problem": "New title assertion",
        }
        return {**data, "issues": [issue]}

    clients.review_override = fresh
    state = drive(store, identifier, clients)
    assert state.closeout and state.next_operation.action == "adjudicate"
    before = len(clients.calls)
    state = drive(store, identifier, clients)
    assert state.lifecycle_phase == Phase.PARKED
    assert len(clients.calls) == before
    steps = store.load(identifier).steps
    assert steps[-2].request.inputs["operation"]["action"] == "carry_forward"
    assert steps[-2].result.used_tokens == 0
    required = store.load(identifier).events[-1].details["required_tokens"]
    assert required == 2 * 33024 + 600005


@pytest.mark.parametrize("loss", ["unlock", "terminate"])
def test_lost_lock_session_prevents_new_paid_io_and_old_owner_commit(loop, monkeypatch, loss):
    from sqlalchemy.exc import SQLAlchemyError

    store, identifier, clients, _ = loop("resume-remote-outcome-unknown")
    original = store.begin_answer_attempt

    def begin(*args, **kwargs):
        attempt = original(*args, **kwargs)
        _, owner_conn, pid, key = store._owner.get()
        if loss == "unlock":
            owner_conn.execute(select(func.pg_advisory_unlock(key)))
            owner_conn.commit()
        else:
            with store.engine.begin() as conn:
                assert conn.scalar(select(func.pg_terminate_backend(pid)))
        return attempt

    monkeypatch.setattr(store, "begin_answer_attempt", begin)
    with pytest.raises((ValueError, SQLAlchemyError)):
        drive(store, identifier, clients)
    assert clients.calls == []
    assert store.export(identifier).review_attempts[-1].state == "started"
    # A new lock owner reconciles; it does not mistake the old claim for a free retry.
    monkeypatch.setattr(store, "begin_answer_attempt", original)
    state = drive(store, identifier, clients)
    assert state.lifecycle_phase == Phase.PARKED
    assert store.export(identifier).review_attempts[-1].state == "unknown"


def test_cancel_between_attempt_intent_and_authorization_costs_no_call(loop, monkeypatch):
    store, identifier, clients, _ = loop("stale-retry")
    original = store.begin_answer_attempt

    def begin(*args, **kwargs):
        attempt = original(*args, **kwargs)
        current = store.load(identifier)
        store.move(identifier, current.revision, Phase.CANCELED, reason="Cancel before paid call")
        return attempt

    monkeypatch.setattr(store, "begin_answer_attempt", begin)
    state = drive(store, identifier, clients)
    assert state.lifecycle_phase == Phase.CANCELED
    assert clients.calls == []
    attempt = store.export(identifier).review_attempts[-1]
    assert attempt.error_kind == "stale_execution"
    assert attempt.charge_basis == "not_called" and attempt.charged_tokens == 0


def test_each_adjudication_rechecks_reserve_and_grant_does_not_replay_first(loop, monkeypatch):
    store, identifier, clients, _ = loop("resume-between-adjudications", lifetime_tokens=3000000)
    clients.limits = clients.limits.model_copy(update={"max_prompt_bytes": 600000})
    advance(store, identifier, clients, 4)
    original = clients.adjudicate

    def expensive(*args, **kwargs):
        call = original(*args, **kwargs)
        return call.model_copy(
            update={"usage": CallUsage(requests=1, input_tokens=300000, output_tokens=5)}
        )

    monkeypatch.setattr(clients, "adjudicate", expensive)
    state = drive(store, identifier, clients)
    assert len(state.rulings) == 1
    spent = store.load(identifier).checkpoint.budget.review_spent
    state = drive(store, identifier, clients)
    assert state.lifecycle_phase == Phase.PARKED
    assert clients.ruling_count == 1
    current = store.load(identifier)
    store.grant(identifier, current.revision, Limits(research=100, review=3000000, total=9000000))
    assert store.load(identifier).checkpoint.budget.limits.total == 3000000
    store.resume(identifier, store.load(identifier).revision)
    state = drive(store, identifier, clients)
    assert len(state.rulings) == clients.ruling_count == 2
    assert clients.calls[-1][3] == "i4"
    assert store.load(identifier).checkpoint.budget.review_spent == spent + 300005


def test_closeout_passes_binding_only_flag_to_author(loop, monkeypatch):
    store, identifier, clients, _ = loop("binding-narrow-reapplied")
    drive(store, identifier, clients, ReviewPolicy(closeout_remaining_tokens=2000000))
    advance(store, identifier, clients, 2)  # Full review, then fresh-issue ruling.
    assert store.answer_state(identifier)[2].next_operation.action == "revise"
    original = clients.revise
    observed = []

    def revise(*args, **kwargs):
        observed.append(kwargs["closeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(clients, "revise", revise)
    state = drive(store, identifier, clients)
    assert observed == [True]
    assert state.next_operation.action == "review"
    assert state.answers[-1].ruling_applications


def test_default_ceiling_cannot_be_increased_and_explicit_ceiling_is_immutable(loop):
    store, identifier, clients, _ = loop("stale-retry")
    current = store.load(identifier)
    granted = store.grant(
        identifier, current.revision, Limits(research=200, review=3000000, total=9999999)
    )
    assert granted.checkpoint.budget.limits.total == 2000100
    assert granted.events[-1].details["requested"]["total"] == 9999999
    assert granted.events[-1].details["lifetime_tokens"] == 2000100
    assert granted.checkpoint.budget.total_spent == 0


def test_ownership_excludes_another_python_process(loop):
    import os
    import subprocess
    import sys

    store, identifier, clients, _ = loop("stale-retry")
    code = """
import os
from uuid import UUID
from query.db import database_engine
from query.store import InvestigationStore
store = InvestigationStore(database_engine(os.environ['QUERY_OWNER_TEST_URL']))
try:
    with store.answer_owner(UUID(os.environ['QUERY_OWNER_TEST_ID']), 0):
        raise AssertionError('Competing process acquired the lock')
except ValueError as error:
    assert 'Another executor' in str(error)
print('blocked')
"""
    env = {
        **os.environ,
        "QUERY_OWNER_TEST_URL": store.engine.url.render_as_string(hide_password=False),
        "QUERY_OWNER_TEST_ID": str(identifier),
    }
    with store.answer_owner(identifier, store.load(identifier).revision):
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "blocked"
    assert clients.calls == []
