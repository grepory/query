"""Exercise the real durable controller with offline, scripted role clients."""

from datetime import UTC, datetime

import pytest

from query.answer_loop import AnswerController
from query.evidence import CandidateSelection, FreezeSnapshot, IngestText
from query.investigation import (
    Limits,
    Phase,
    StepResult,
    StepStatus,
)
from query.review import Answer, IssueResponse, Review, Ruling
from query.review_clients import PROMPT_VERSIONS, CallLimits, CallUsage, ModelCall, RoleCallError
from query.store import InvestigationStore, StaleRevision
from tests.test_review_clients import config


class ScriptedClients:
    def __init__(self, corpus, case):
        self.corpus, self.case = corpus, case
        self.models, self.limits = (
            config(),
            CallLimits(max_prompt_bytes=32768, max_output_tokens=256),
        )
        self.calls = []
        self.answer_count = self.review_count = self.ruling_count = 0
        self.before_call = None
        self.fail = False
        self.review_override = None
        self.revision_override = None

    def call(self, role, context, output):
        if self.before_call:
            self.before_call()
        if self.fail:
            raise RoleCallError(role, "provider_unavailable", retryable=True)
        settings = getattr(self.models, role) or self.models.reviewer
        return ModelCall(
            output=output,
            role=role,
            provider=settings.model_backend,
            model_name=settings.model_name,
            prompt_version=PROMPT_VERSIONS[role],
            context_digest=context.digest(),
            simulated=True,
            usage=CallUsage(requests=1, input_tokens=10, output_tokens=5),
        )

    def answer(self, context, identifier, previous=None, review=None, rulings=()):
        draft = self.case.drafts[min(self.answer_count, len(self.case.drafts) - 1)]
        self.answer_count += 1
        passage = context.passages()[0]
        citation = {k: passage[k] for k in ("version_id", "passage_id")}
        citation["excerpt"] = passage["text"]
        claims = [
            dict(
                id=c.id,
                text=c.text,
                question_ids=[c.question],
                kind="source_assertion",
                citations=[citation],
                premises=[],
                reasoning=None,
            )
            for c in draft.claims
        ]
        responses = []
        if review:
            for issue in review.issues:
                if issue.kind == "editorial":
                    continue
                prepared = next((r for r in self.case.responses if r.issue == issue.id), None)
                disposition = prepared.disposition if prepared else "defend"
                responses.append(
                    IssueResponse(
                        review_id=review.id,
                        issue_id=issue.id,
                        claim_id=issue.claim_id,
                        claim_text=issue.claim_text,
                        disposition=disposition,
                        explanation=prepared.explanation
                        if prepared
                        else "Saved issue-specific defense",
                        evidence=(citation,) if disposition == "defend" else (),
                        applied_edit=(prepared.applied_edit if prepared else None),
                    )
                )
        applications = [
            dict(
                ruling_id=r.id,
                claim_id=r.claim_id,
                claim_text=r.claim_text,
                applied_edit="Apply binding edit",
            )
            for r in rulings
            if r.decision in {"narrow", "remove"}
        ]
        data = dict(
            id=identifier,
            snapshot_id=context.evidence.snapshot.id,
            brief_version=1,
            title="Capacity",
            body_markdown="\n".join(c["text"] for c in claims) or "Unknown.",
            claims=claims,
            coverage=[
                dict(
                    question_id=q.key,
                    status="answered" if claims else "unresolved",
                    claim_ids=[c["id"] for c in claims],
                    rationale="Supplied-source assessment",
                )
                for q in context.brief.brief.questions
            ],
            issue_responses=responses,
            ruling_applications=applications,
        )
        if self.revision_override and previous:
            data = self.revision_override(data, previous, review, rulings)
        return Answer.model_validate(data)

    def write(self, context, identifier):
        self.calls.append(("write", identifier))
        return self.call("author", context, self.answer(context, identifier))

    def revise(self, context, identifier, previous, review, *, rulings=(), closeout=False):
        self.calls.append(("revise", previous, review, rulings))
        return self.call(
            "author", context, self.answer(context, identifier, previous, review, rulings)
        )

    def review(self, context, identifier, answer, *, previous_review=None, rulings=()):
        self.calls.append(("review", answer, previous_review, rulings))
        prepared = self.case.reviews[min(self.review_count, len(self.case.reviews) - 1)]
        self.review_count += 1
        issues = []
        for i in prepared.issues:
            claim = next((c for c in answer.claims if c.id == i.claim), None)
            if claim is None:
                continue
            issues.append(
                dict(
                    id=i.id,
                    kind=i.kind,
                    claim_id=claim.id,
                    claim_text=claim.text,
                    question_id=claim.question_ids[0],
                    problem=i.problem,
                    evidence_examined=(),
                    evidence_gap="Check against saved evidence",
                    suggestion=None,
                )
            )
        data = dict(
            id=identifier,
            answer_id=answer.id,
            brief_version=1,
            snapshot_id=context.evidence.snapshot.id,
            publishable=prepared.publishable,
            assessment="Full answer review",
            issues=issues,
            coverage=answer.coverage,
        )
        if self.review_override:
            data = self.review_override(data, answer)
        return self.call("reviewer", context, Review.model_validate(data))

    def adjudicate(
        self, context, identifier, answer, review, issue_id, response, *, response_review=None
    ):
        self.calls.append(("adjudicate", answer, review, issue_id, response, response_review))
        prepared = (
            self.case.rulings[min(self.ruling_count, len(self.case.rulings) - 1)]
            if self.case.rulings
            else None
        )
        self.ruling_count += 1
        issue = next(i for i in review.issues if i.id == issue_id)
        ruling = Ruling(
            id=identifier,
            answer_id=answer.id,
            review_id=review.id,
            issue_id=issue_id,
            brief_version=1,
            snapshot_id=context.evidence.snapshot.id,
            claim_id=issue.claim_id,
            claim_text=issue.claim_text,
            decision=prepared.decision if prepared else "supported",
            rationale="Checked saved source",
            evidence=answer.claims[0].citations,
            replacement=prepared.replacement if prepared else None,
            missing_context=None,
        )
        return self.call("adjudicator", context, ruling)


@pytest.fixture
def loop(database, review_corpus):
    def make(case_id, *, lifetime_tokens=None):
        case = next(c for c in review_corpus.cases if c.id == case_id)
        store = InvestigationStore(database[0])
        run = store.create(
            review_corpus.brief,
            Limits(research=100, review=2000000, total=2000100),
            lifetime_tokens=lifetime_tokens,
        )
        observation = store.ingest_text(
            run.id,
            run.revision,
            IngestText(
                operation_key="document",
                document_key="v1",
                title="Operational record",
                text=review_corpus.versions["v1"],
                observed_at=datetime(2026, 10, 1, tzinfo=UTC),
            ),
        )
        current = store.load(run.id)
        snapshot = store.freeze_snapshot(
            run.id,
            current.revision,
            FreezeSnapshot(
                operation_key="snapshot",
                candidates=(CandidateSelection(version_id=observation.version_id),),
            ),
        )
        current = store.move(run.id, store.load(run.id).revision, Phase.PLANNING)
        store.move(run.id, current.revision, Phase.REVIEWING)
        clients = ScriptedClients(review_corpus, case)
        return store, run.id, clients, snapshot

    return make


def advance(store, identifier, clients, count=1):
    state = None
    for _ in range(count):
        # A new controller/store instance on every operation exercises reconstruction.
        fresh = InvestigationStore(store.engine, clock=store.clock)
        state = AnswerController(fresh, clients).advance(
            identifier, fresh.load(identifier).revision
        )
    return state


@pytest.mark.parametrize(
    "case_id", ["review-pending-revision", "issue-specific-defense", "concession-saved"]
)
def test_pending_revision_exports_only_reviewed_answer_then_resumes_at_review(loop, case_id):
    store, identifier, clients, _ = loop(case_id)
    state = advance(store, identifier, clients, 3)
    assert state.next_operation.action == "review"
    assert state.current_answer.answer.id == state.answers[0].id
    exported = store.export(identifier)
    assert exported.answer_loop == state
    saved = store.load(identifier).steps
    assert len(saved) == 3
    assert saved[-1].result.output["call"]["output"]["issue_responses"]
    if case_id == "concession-saved":
        assert state.answers[-1].issue_responses[0].disposition == "concede"
        assert state.answers[-1].issue_responses[0].applied_edit
    clients.review_override = lambda data, answer: {**data, "issues": []}
    final = advance(store, identifier, clients)
    assert final.status == "settled"
    assert final.current_answer.answer.id == state.answers[-1].id
    assert store.load(identifier).steps[:3] == saved
    assert [c[0] for c in clients.calls] == ["write", "review", "revise", "review"]
    assert store.load(identifier).checkpoint.budget.review_spent == 60


@pytest.mark.parametrize(
    "case_id,status,action",
    [
        ("editorial-only-rejection", "settled", None),
        ("signoff-with-factual-blocker", "working", "revise"),
        ("fresh-claim-not-a-stall", "working", "revise"),
    ],
)
def test_issue_consequence_overrides_signoff_and_round_count(loop, case_id, status, action):
    store, identifier, clients, _ = loop(case_id)
    state = advance(store, identifier, clients, 4 if case_id == "fresh-claim-not-a-stall" else 2)
    assert state.status == status
    assert (state.next_operation.action if state.next_operation else None) == action
    if status == "settled":
        before = store.load(identifier)
        assert advance(store, identifier, clients) == state
        assert store.load(identifier) == before
        assert state.current_answer.review.issues[0].kind == "editorial"
        assert before.checkpoint.phase == Phase.REVIEWING
        assert before.checkpoint.completion is None


@pytest.mark.parametrize(
    "case_id", ["reworded-objection-escalates", "upheld-still-final-review", "upheld-same-scope"]
)
def test_repeated_dispute_uses_saved_defense_and_gets_final_full_review(loop, case_id):
    store, identifier, clients, _ = loop(case_id)
    state = advance(store, identifier, clients, 4)
    assert state.next_operation.action == "adjudicate"
    state = advance(store, identifier, clients)
    call = clients.calls[-1]
    assert call[0] == "adjudicate"
    assert call[4] == state.answers[-1].issue_responses[0]
    assert call[5].id == call[4].review_id
    assert call[5].id != call[2].id
    assert state.next_operation.action == "carry_forward"
    before = store.load(identifier).checkpoint.budget.review_spent
    carried = advance(store, identifier, clients)
    assert carried.next_operation.action == "review"
    assert carried.current_answer.answer.id == state.answers[-1].id
    assert store.load(identifier).checkpoint.budget.review_spent == before
    settled = advance(store, identifier, clients)
    assert settled.status == "settled"
    assert settled.current_answer.answer.id != state.answers[-1].id
    assert clients.ruling_count == 1
    assert [c[0] for c in clients.calls] == [
        "write",
        "review",
        "revise",
        "review",
        "adjudicate",
        "review",
    ]


def test_fresh_objection_still_reaches_author_in_adjudication_round(loop):
    store, identifier, clients, _ = loop("fresh-and-repeated-issues")
    state = advance(store, identifier, clients, 5)
    assert state.next_operation.action == "revise"
    assert clients.ruling_count == 1
    state = advance(store, identifier, clients)
    assert {r.issue_id for r in state.answers[-1].issue_responses} == {"i2", "i3"}
    assert state.next_operation.action == "review"
    assert clients.calls[-1][3][0].decision == "supported"


@pytest.mark.parametrize("case_id", ["binding-narrow-reapplied", "binding-remove-reapplied"])
@pytest.mark.parametrize("review_omits_issue", [False, True])
def test_ignored_binding_edits_reapply_without_readjudication_even_if_reviewer_misses_it(
    loop, case_id, review_omits_issue
):
    store, identifier, clients, _ = loop(case_id)
    state = advance(store, identifier, clients, 6)
    assert state.answers[-1].ruling_applications
    assert state.next_operation.action == "review"
    if review_omits_issue:
        clients.review_override = lambda data, answer: {**data, "issues": []}
    state = advance(store, identifier, clients)
    assert state.next_operation.action == "revise"
    assert clients.ruling_count == 1

    def apply(data, previous, review, rulings):
        ruling = rulings[0]
        if ruling.decision == "narrow":
            data["claims"][0]["text"] = ruling.replacement
            data["body_markdown"] = ruling.replacement
        else:
            data["claims"] = []
            data["body_markdown"] = "The operational figure remains unresolved."
            data["coverage"] = [
                dict(
                    question_id="operational",
                    status="unresolved",
                    claim_ids=[],
                    rationale="Removed unsupported figure",
                )
            ]
        return data

    clients.revision_override = apply
    state = advance(store, identifier, clients)
    assert state.next_operation.action == "review"
    clients.review_override = lambda data, answer: {**data, "issues": []}
    final = advance(store, identifier, clients)
    assert final.status == (
        "settled" if case_id == "binding-narrow-reapplied" else "needs_research"
    )
    assert clients.ruling_count == 1
    assert clients.calls[-1][3]  # Review receives the removed/narrowed historical ruling.


def test_plan_is_saved_before_io_and_pending_intent_reused_after_failure(loop):
    store, identifier, clients, _ = loop("review-pending-revision")
    observed = []
    clients.before_call = lambda: observed.append(store.load(identifier).steps[-1])
    clients.fail = True
    paused = advance(store, identifier, clients)
    assert paused.lifecycle_phase == Phase.PAUSED
    assert observed[0].result is None
    pending = store.load(identifier).steps[-1]
    assert pending.request.inputs["execution"]["prompt_version"] == PROMPT_VERSIONS["author"]
    assert "api_key" not in str(pending.request.inputs)
    clients.fail = False
    retry_at = store.export(identifier).review_attempts[-1].retry_at
    store.clock = lambda: retry_at
    store.resume(identifier, store.load(identifier).revision)
    state = advance(store, identifier, clients)
    assert len(store.load(identifier).steps) == 1
    assert state.next_operation.action == "review"
    assert observed[1].request == pending.request
    assert (
        store.load(identifier).checkpoint.budget.review_spent
        == pending.request.reserved_tokens + 15
    )
    with pytest.raises(ValueError, match="committed result"):
        store.finish_step(
            identifier,
            store.load(identifier).revision,
            pending.request.key,
            StepResult(status=StepStatus.SUCCEEDED, used_tokens=1, output={}),
        )


def test_stale_held_and_unaffordable_runs_do_no_paid_work(loop):
    store, identifier, clients, _ = loop("review-pending-revision")
    current = store.load(identifier)
    with pytest.raises(StaleRevision):
        AnswerController(store, clients).advance(identifier, current.revision - 1)
    held = store.move(identifier, current.revision, Phase.PARKED, reason="Explicit hold")
    with pytest.raises(ValueError, match="resume"):
        advance(store, identifier, clients)
    store.resume(identifier, held.revision)
    clients.limits = clients.limits.model_copy(update={"max_prompt_bytes": 3000000})
    held = advance(store, identifier, clients)
    assert held.lifecycle_phase == Phase.PARKED
    assert clients.calls == []
    assert store.load(identifier).steps == ()


def test_new_snapshot_and_brief_do_not_inherit_rulings_or_current_approval(loop):
    store, identifier, clients, snapshot = loop("upheld-same-scope")
    original = advance(store, identifier, clients, 7)
    assert original.status == "settled"
    current = store.move(identifier, store.load(identifier).revision, Phase.PLANNING)
    new_snapshot = store.freeze_snapshot(
        identifier,
        current.revision,
        FreezeSnapshot(
            operation_key="new-snapshot",
            candidates=(CandidateSelection(version_id=snapshot.versions[0].id),),
        ),
    )
    assert new_snapshot.snapshot.id != original.snapshot_id
    fresh = store.export(identifier).answer_loop
    assert fresh.current_answer is None and fresh.rulings == ()
    assert fresh.next_operation.action == "write"
    historical = store.export(identifier).last_reviewed_answer
    assert historical == original.current_answer
    assert historical.answer.snapshot_id == snapshot.snapshot.id
    assert historical.review.answer_id == historical.answer.id
    assert len(store.export(identifier).investigation.steps) == 7
    current = store.load(identifier)
    store.revise_brief(identifier, current.revision, current.briefs[-1].brief)
    assert store.export(identifier).answer_loop is None
    assert store.export(identifier).last_reviewed_answer == historical
    with pytest.raises(ValueError, match="Freeze a snapshot"):
        store.answer_state(identifier)


def test_changed_claim_reused_id_gets_fresh_response_instead_of_old_approval(loop):
    store, identifier, clients, _ = loop("fresh-and-repeated-issues")
    advance(
        store, identifier, clients, 5
    )  # Uphold capacity; fresh expansion issue requires revision.

    def changed(data, previous, review, rulings):
        data["claims"][0]["text"] = "Aster operates 1200 MW."
        data["body_markdown"] = "\n".join(c["text"] for c in data["claims"])
        return data

    clients.revision_override = changed
    advance(store, identifier, clients)
    state = advance(store, identifier, clients)
    assert (
        state.next_operation.action == "adjudicate"
    )  # Expansion now repeated, capacity still fresh.
    state = advance(store, identifier, clients)
    assert state.next_operation.action == "revise"
    assert len(state.rulings) == 2


def test_missing_required_question_does_not_close_by_deleting_claim(loop):
    store, identifier, clients, _ = loop("missing-required-question")
    state = advance(store, identifier, clients, 4)
    assert state.status == "needs_research"
    assert state.current_answer.answer.coverage[0].question_id == "operational"
    assert state.current_answer.answer.coverage[0].status == "unresolved"
    assert state.current_answer.answer.claims == ()
    assert store.load(identifier).checkpoint.completion is None


@pytest.mark.parametrize("mutation", ["omitted", "renamed", "metadata"])
@pytest.mark.parametrize("case_id", ["binding-narrow-reapplied", "binding-remove-reapplied"])
def test_binding_body_span_cannot_escape_via_claim_inventory(loop, mutation, case_id):
    store, identifier, clients, _ = loop(case_id)
    advance(store, identifier, clients, 5)

    def evade(data, previous, review, rulings):
        if mutation == "omitted":
            data["claims"] = []
            data["coverage"] = [
                dict(
                    question_id="operational",
                    status="qualified",
                    claim_ids=[],
                    rationale="Inventory omitted",
                )
            ]
        elif mutation == "renamed":
            data["claims"][0]["id"] = "renamed"
            data["coverage"][0]["claim_ids"] = ["renamed"]
        else:
            data["claims"][0]["citations"][0]["excerpt"] = "Aster operates 900 MW."
        return data

    clients.revision_override = evade
    advance(store, identifier, clients)
    clients.review_override = lambda data, answer: {**data, "issues": []}
    state = advance(store, identifier, clients)
    assert state.next_operation.action == "revise"
    assert len(state.next_operation.ruling_ids) == 1
    assert clients.ruling_count == 1
    # Reapplication is admitted even though the exact original inventory entry changed.
    advance(store, identifier, clients)
    assert clients.ruling_count == 1


def test_supported_claim_cannot_suppress_required_question_coverage_issue(loop):
    store, identifier, clients, _ = loop("upheld-same-scope")
    advance(store, identifier, clients, 6)

    def coverage(data, answer):
        data["issues"][0]["kind"] = "coverage"
        data["issues"][0]["problem"] = "The required scope is still unanswered."
        return data

    clients.review_override = coverage
    state = advance(store, identifier, clients)
    assert state.status == "working"
    assert state.next_operation.action == "revise"
    assert clients.ruling_count == 1


def test_cli_executes_one_action_with_explicit_models_and_limits(
    loop, tmp_path, monkeypatch, capsys
):
    from query import cli

    store, identifier, clients, _ = loop("editorial-only-rejection")
    models_path, limits_path = tmp_path / "models.json", tmp_path / "limits.json"
    models_path.write_text(clients.models.model_dump_json())
    limits_path.write_text(clients.limits.model_dump_json())
    monkeypatch.setattr(cli, "ReviewClients", lambda models, limits=None: clients)
    arguments = cli.parser().parse_args(
        [
            "advance",
            str(identifier),
            "--revision",
            str(store.load(identifier).revision),
            "--answer-models",
            str(models_path),
            "--call-limits",
            str(limits_path),
        ]
    )
    state = cli.execute(store, arguments)
    assert state.next_operation.action == "review" and len(clients.calls) == 1
    monkeypatch.setenv("QUERY_DATABASE_URL", "postgresql://grepory@127.0.0.1:55439/query_test")
    monkeypatch.setattr(cli, "database_engine", lambda url: store.engine)
    assert (
        cli.main(
            [
                "advance",
                str(identifier),
                "--revision",
                str(store.load(identifier).revision),
                "--answer-models",
                str(models_path),
                "--call-limits",
                str(limits_path),
            ]
        )
        == 0
    )
    assert '"status": "settled"' in capsys.readouterr().out
    assert len(clients.calls) == 2


def test_pending_execution_cannot_silently_change_model_or_call_bounds(loop):
    store, identifier, clients, _ = loop("review-pending-revision")
    clients.fail = True
    advance(store, identifier, clients)
    retry_at = store.export(identifier).review_attempts[-1].retry_at
    store.clock = lambda: retry_at
    store.resume(identifier, store.load(identifier).revision)
    clients.fail = False
    clients.limits = clients.limits.model_copy(update={"max_output_tokens": 512})
    with pytest.raises(ValueError, match="different work"):
        advance(store, identifier, clients)
    assert len(clients.calls) == 1


def test_partial_adjudication_round_resumes_only_missing_ruling(loop):
    store, identifier, clients, _ = loop("fresh-and-repeated-issues")
    # Both claims now have saved responses and are repeated; adjudicate each independently.
    clients.review_override = lambda data, answer: (
        {**data, "issues": data["issues"][1:]} if clients.review_count == 2 else data
    )
    advance(store, identifier, clients, 4)
    clients.review_override = None
    advance(store, identifier, clients, 2)
    state = store.answer_state(identifier)[2]
    assert state.next_operation.action == "adjudicate"
    first = advance(store, identifier, clients)
    assert first.next_operation.action == "adjudicate"
    second = advance(store, identifier, clients)
    assert len(second.rulings) == 2
    assert clients.ruling_count == 2
    assert second.next_operation.action == "carry_forward"


def test_role_vendor_separation_is_pinned_across_advances(loop):
    store, identifier, clients, _ = loop("editorial-only-rejection")
    advance(store, identifier, clients)
    clients.models = config(author="openai", reviewer="anthropic")
    with pytest.raises(ValueError, match="pinned"):
        advance(store, identifier, clients)
    assert len(clients.calls) == 1
    assert len(store.load(identifier).steps) == 1


def test_durable_commit_rejects_invented_ruling_disposition(loop):
    store, identifier, clients, _ = loop("review-pending-revision")
    advance(store, identifier, clients, 2)

    def invented(data, previous, review, rulings):
        data["issue_responses"][0] = data["issue_responses"][0].model_copy(
            update={"disposition": "applied_ruling", "applied_edit": "Invented ruling"}
        )
        return data

    clients.revision_override = invented
    with pytest.raises(ValueError, match="supplied ruling"):
        advance(store, identifier, clients)
    saved = store.load(identifier)
    assert saved.steps[-1].result is None
    assert len(store.answer_state(identifier)[2].answers) == 1


def test_real_role_clients_complete_durable_adjudication_with_offline_function_models(loop):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
    from pydantic_ai.models.function import FunctionModel

    from query.review_clients import ReviewClients

    store, identifier, template, _ = loop("upheld-same-scope")
    context = store.answer_state(identifier)[1]

    def function(role):
        def respond(messages, info):
            import json
            from uuid import UUID

            payload = json.loads(
                next(
                    p.content
                    for m in reversed(messages)
                    for p in m.parts
                    if isinstance(p, UserPromptPart)
                )
            )
            if role == "author":
                previous = (
                    Answer.model_validate(payload["previous_answer"])
                    if "previous_answer" in payload
                    else None
                )
                review = Review.model_validate(payload["review"]) if previous else None
                output = template.answer(
                    context,
                    UUID(payload["answer_id"]),
                    previous,
                    review,
                    tuple(Ruling.model_validate(r) for r in payload.get("rulings", [])),
                )
            elif role == "reviewer":
                output = template.review(
                    context, UUID(payload["review_id"]), Answer.model_validate(payload["answer"])
                ).output
            else:
                state = store.answer_state(identifier)[2]
                output = template.adjudicate(
                    context,
                    UUID(payload["ruling_id"]),
                    state.answers[-1],
                    state.reviews[-1],
                    payload["issue"]["id"],
                    state.answers[-1].issue_responses[0],
                ).output
                assert payload["author_response"]["review_id"] != payload["review_id"]
                assert payload["response_review"]["id"] == payload["author_response"]["review_id"]
                assert all(c["status"] == "verified" for c in payload["author_evidence"])
                assert payload["source_packet"]
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, output.model_dump(mode="json"))]
            )

        return FunctionModel(respond)

    clients = ReviewClients(
        config(),
        author_model=function("author"),
        reviewer_model=function("reviewer"),
        adjudicator_model=function("adjudicator"),
    )
    final = advance(store, identifier, clients, 7)
    assert final.status == "settled"
    assert len(final.answers) == 3 and len(final.reviews) == 3 and len(final.rulings) == 1
    assert all(
        s.result.output.get("call", {}).get("simulated", True) for s in store.load(identifier).steps
    )
