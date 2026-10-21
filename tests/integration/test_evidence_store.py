from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from query import db
from query.evidence import CandidateSelection, FreezeSnapshot, IngestText
from query.investigation import Brief, Lane, Limits, Phase, Question, StepRequest
from query.store import InvestigationStore, StaleRevision


@pytest.fixture
def store(database):
    return InvestigationStore(database[0])


@pytest.fixture
def brief():
    return Brief(
        question="What is known?",
        scope="Supplied documents",
        cutoff=date(2026, 10, 21),
        questions=(
            Question(key="known", text="What is known?", acceptance_criteria="Supported claims"),
        ),
    )


@pytest.fixture
def run(store, brief):
    return store.create(brief, Limits(research=100, review=100, total=150))


def supplied(key="import-1", text="  é\r\n🦊e\u0301  ", document_key="document-1", **kwargs):
    return IngestText(
        operation_key=key,
        document_key=document_key,
        title="Document 1",
        observed_at=datetime(2026, 10, 21, tzinfo=UTC),
        text=text,
        **kwargs,
    )


def ingest(store, run, request=None):
    return store.ingest_text(run.id, store.load(run.id).revision, request or supplied())


def freeze(store, run, candidates=(), key="freeze-1", **kwargs):
    return store.freeze_snapshot(
        run.id,
        store.load(run.id).revision,
        FreezeSnapshot(operation_key=key, candidates=candidates, **kwargs),
    )


def counts(store):
    with store.engine.begin() as conn:
        return {
            table.name: conn.scalar(select(func.count()).select_from(table))
            for table in db.metadata.sorted_tables
        }


def test_exact_roundtrip_versions_shared_sources_and_distinct_keys(store, run, brief):
    first = ingest(store, run)
    saved = store.load_source_version(first.version_id)
    assert saved.text == supplied().text
    assert saved.passage_text(saved.passages[0]) == saved.text
    changed = ingest(store, run, supplied("import-2", "changed"))
    again = ingest(store, run, supplied("import-3"))
    assert changed.version_id != first.version_id == again.version_id
    assert store.load_source_version(again.version_id).passages == saved.passages
    other = store.create(brief, Limits(research=1, review=1, total=2))
    shared = ingest(store, other)
    assert shared.version_id == first.version_id
    different_key = ingest(store, other, supplied("other-doc", document_key="other"))
    assert different_key.version_id != first.version_id
    row_counts = counts(store)
    assert row_counts["sources"] == 2
    assert row_counts["source_versions"] == 3


def test_snapshot_freezes_provenance_order_and_old_brief(store, run, brief):
    first = ingest(store, run, supplied(text="🦊" * 2001))
    version = store.load_source_version(first.version_id)
    snapshot = freeze(
        store,
        run,
        (
            CandidateSelection(
                version_id=first.version_id,
                passage_ids=(version.passages[1].id,),
                exclusion_reason="Only last passage needed",
            ),
        ),
    )
    assert snapshot.snapshot.status == "ready"
    assert snapshot.snapshot.included_bytes == 4
    assert [p.included for p in snapshot.snapshot.candidates[0].passages] == [False, True]
    request = supplied("later", "🦊" * 2001).model_copy(
        update={
            "observed_at": datetime(2026, 10, 22, tzinfo=UTC),
            "published_at": datetime(2026, 10, 20, tzinfo=UTC),
        }
    )
    ingest(store, run, request)
    replay = freeze(
        store,
        run,
        (
            CandidateSelection(
                version_id=first.version_id,
                passage_ids=(version.passages[1].id,),
                exclusion_reason="Only last passage needed",
            ),
        ),
    )
    assert replay == snapshot
    assert replay.observations == (first,)
    store.revise_brief(
        run.id, store.load(run.id).revision, brief.model_copy(update={"cutoff": date(2026, 10, 22)})
    )
    ingest(store, run, supplied("changed", "new text"))
    store.engine.dispose()
    assert InvestigationStore(store.engine).load_snapshot(snapshot.snapshot.id) == snapshot
    newer = freeze(store, run, (CandidateSelection(version_id=first.version_id),), key="freeze-2")
    assert newer.snapshot.brief_version == 2
    assert newer.snapshot.candidates[0].observation_keys == ("import-1", "later")


@pytest.mark.parametrize(
    "text,limit,reason", [("", 1, "empty_document"), ("🦊", 3, "document_too_large")]
)
def test_rejected_input_is_durable_in_snapshot_and_can_retry(store, run, text, limit, reason):
    request = supplied(text=text, max_bytes=limit)
    rejected = ingest(store, run, request)
    assert rejected.reason == reason
    assert rejected.version_id is None
    row_counts = counts(store)
    assert row_counts["sources"] == row_counts["source_versions"] == 0
    state = store.load(run.id)
    assert ingest(store, run, request) == rejected
    assert store.load(run.id) == state
    snapshot = freeze(store, run)
    assert snapshot.snapshot.status == "limited"
    assert snapshot.snapshot.reason == "empty_selection"
    assert snapshot.snapshot.rejected_inputs == (rejected,)
    if text:
        accepted = ingest(store, run, supplied("larger", text, max_bytes=4))
        assert accepted.status == "accepted"
        ready = freeze(
            store, run, (CandidateSelection(version_id=accepted.version_id),), key="ready"
        )
        assert ready.snapshot.rejected_inputs == (rejected,)


def test_limits_exclusions_foreign_passages_and_thresholds(store, run, brief):
    first = ingest(store, run, supplied(text="🦊" * 2001))
    version = store.load_source_version(first.version_id)
    selection = CandidateSelection(version_id=first.version_id)
    exact = freeze(store, run, (selection,), max_bytes=8004)
    assert exact.snapshot.status == "ready"
    limited = freeze(store, run, (selection,), key="too-big", max_bytes=8003)
    assert limited.snapshot.status == "limited"
    assert limited.snapshot.requested_bytes == 8004
    assert limited.snapshot.included_bytes == 0
    assert all(
        not p.included and p.reason == "snapshot_too_large"
        for p in limited.snapshot.candidates[0].passages
    )
    excluded = freeze(
        store,
        run,
        (
            CandidateSelection(
                version_id=first.version_id, passage_ids=(), exclusion_reason="Not relevant"
            ),
        ),
        key="excluded",
    )
    assert excluded.snapshot.reason == "empty_selection"
    assert all(p.reason == "Not relevant" for p in excluded.snapshot.candidates[0].passages)
    other = store.create(brief, Limits(research=1, review=1, total=2))
    before = counts(store), store.load(other.id)
    with pytest.raises(ValueError, match="not acquired"):
        freeze(store, other, (selection,))
    assert (counts(store), store.load(other.id)) == before
    for passage_ids, reason in [((uuid4(),), "foreign"), ((), None)]:
        before = counts(store), store.load(run.id)
        with pytest.raises(ValueError):
            freeze(
                store,
                run,
                (
                    CandidateSelection(
                        version_id=first.version_id,
                        passage_ids=passage_ids,
                        exclusion_reason=reason,
                    ),
                ),
                key="invalid",
            )
        assert (counts(store), store.load(run.id)) == before
    assert version.passages


def test_retry_conflicts_stale_revision_and_immutable_source_metadata(store, run, brief):
    first = ingest(store, run)
    assert first.revision == 2
    with pytest.raises(StaleRevision):
        store.ingest_text(run.id, 1, supplied())
    for request in [
        supplied(text="changed"),
        supplied(max_bytes=2),
        supplied().model_copy(update={"title": "Other"}),
    ]:
        before = counts(store), store.load(run.id)
        with pytest.raises(ValueError):
            ingest(store, run, request)
        assert (counts(store), store.load(run.id)) == before
    with pytest.raises(ValueError, match="different title"):
        ingest(store, run, supplied("new-key").model_copy(update={"title": "Other"}))
    snapshot = freeze(store, run, (CandidateSelection(version_id=first.version_id),))
    with pytest.raises(ValueError, match="different snapshot"):
        freeze(store, run, (), max_bytes=1)
    store.revise_brief(run.id, store.load(run.id).revision, brief)
    with pytest.raises(ValueError):
        ingest(store, run)
    with pytest.raises(ValueError):
        freeze(store, run, (CandidateSelection(version_id=first.version_id),))
    assert store.load_snapshot(snapshot.snapshot.id) == snapshot


@pytest.mark.parametrize(
    "phase",
    [
        Phase.PAUSED,
        Phase.PARKED,
        Phase.AWAITING_INPUT,
        Phase.STOPPED_AT_LIMIT,
        Phase.CANCELED,
        Phase.REVIEWING,
    ],
)
def test_evidence_mutations_reject_held_terminal_and_reviewing(store, run, phase):
    if phase == Phase.REVIEWING:
        state = store.move(run.id, run.revision, Phase.PLANNING)
        state = store.move(run.id, state.revision, phase)
    else:
        state = store.move(
            run.id,
            run.revision,
            phase,
            reason="budget" if phase == Phase.STOPPED_AT_LIMIT else "test",
        )
    before = counts(store)
    with pytest.raises(ValueError, match="research phase"):
        store.ingest_text(run.id, state.revision, supplied())
    with pytest.raises(ValueError, match="research phase"):
        freeze(store, run)
    assert counts(store) == before


def test_pending_step_and_invalid_text_prevent_mutation(store, run):
    state = store.plan_step(
        run.id,
        run.revision,
        StepRequest(
            key="pending", kind="extract", lane=Lane.RESEARCH, reserved_tokens=1, inputs={}
        ),
    )
    before = counts(store)
    with pytest.raises(ValueError, match="pending"):
        store.ingest_text(run.id, state.revision, supplied())
    with pytest.raises(ValueError, match="pending"):
        freeze(store, run)
    for text in ["\x00", "\ud800"]:
        with pytest.raises(ValueError):
            store.ingest_text(run.id, state.revision, supplied().model_copy(update={"text": text}))
    assert counts(store) == before


@pytest.mark.parametrize("operation", ["ingest", "snapshot"])
def test_failure_before_event_rolls_back_everything(store, run, monkeypatch, operation):
    first = ingest(store, run) if operation == "snapshot" else None
    before = counts(store), store.load(run.id)
    original = store._save

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(RuntimeError):
        if operation == "snapshot":
            freeze(store, run, (CandidateSelection(version_id=first.version_id),))
        else:
            ingest(store, run)
    assert (counts(store), store.load(run.id)) == before


def test_concurrent_shared_source_import_is_deduplicated(store, run, brief):
    other = store.create(brief, Limits(research=1, review=1, total=2))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda r: store.ingest_text(r.id, r.revision, supplied()), [run, other])
        )
    assert results[0].version_id == results[1].version_id
    row_counts = counts(store)
    assert row_counts["sources"] == row_counts["source_versions"] == row_counts["passages"] == 1


def test_concurrent_same_investigation_has_one_winner(store, run):
    def attempt(key):
        try:
            return store.ingest_text(run.id, run.revision, supplied(key))
        except StaleRevision:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["one", "two"]))
    assert sum(r is not None for r in results) == 1
    assert counts(store)["evidence_inputs"] == 1


def test_concurrent_brief_change_and_freeze_never_mix_brief(store, run, brief):
    first = ingest(store, run)
    revision = store.load(run.id).revision

    def revise():
        try:
            return store.revise_brief(run.id, revision, brief)
        except StaleRevision:
            return None

    def snapshot():
        try:
            return store.freeze_snapshot(
                run.id,
                revision,
                FreezeSnapshot(
                    operation_key="race",
                    candidates=(CandidateSelection(version_id=first.version_id),),
                ),
            )
        except StaleRevision:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = executor.submit(revise), executor.submit(snapshot)
        revised, frozen = a.result(), b.result()
    assert (revised is None) != (frozen is None)
    if frozen:
        assert frozen.snapshot.brief_version == 1


def test_sql_metacharacters_remain_literal_values(store, run):
    malicious = "x'); DROP TABLE investigations; --"
    request = supplied(key=malicious, document_key=malicious, text=malicious).model_copy(
        update={"title": malicious, "origin_url": malicious}
    )
    imported = ingest(store, run, request)
    assert store.load_source_version(imported.version_id).text == malicious
    loaded = freeze(
        store,
        run,
        (
            CandidateSelection(
                version_id=imported.version_id, passage_ids=(), exclusion_reason=malicious
            ),
        ),
        key=malicious,
    )
    assert loaded.observations[0].document_key == malicious
    assert loaded.snapshot.candidates[0].passages[0].reason == malicious
    assert store.load(run.id).id == run.id
    assert counts(store)["investigations"] == 1


def test_missing_reads_raise_key_error(store):
    with pytest.raises(KeyError):
        store.load_source_version(uuid4())
    with pytest.raises(KeyError):
        store.load_snapshot(uuid4())


def test_complete_investigation_cannot_accept_new_evidence(store, run):
    from query.investigation import CompletionChecks, Outcome

    state = store.move(run.id, run.revision, Phase.PLANNING)
    state = store.move(run.id, state.revision, Phase.REVIEWING)
    state = store.move(
        run.id,
        state.revision,
        Phase.COMPLETE,
        completion=CompletionChecks(
            brief_version=1,
            answer_ref="answer-1",
            answer_integrity=True,
            research_sufficiency=True,
            rationale="Reviewed",
        ),
        outcome=Outcome.ANSWERED,
    )
    before = counts(store), state
    with pytest.raises(ValueError, match="research phase"):
        store.ingest_text(run.id, state.revision, supplied())
    with pytest.raises(ValueError, match="research phase"):
        freeze(store, run)
    assert (counts(store), store.load(run.id)) == before


def test_candidate_order_and_prior_brief_rejections_are_explicit(store, run, brief):
    rejected = ingest(store, run, supplied("rejected", "", document_key="rejected"))
    first = ingest(store, run)
    second = ingest(store, run, supplied("second", "second", document_key="second"))
    selections = (
        CandidateSelection(version_id=second.version_id),
        CandidateSelection(version_id=first.version_id),
    )
    snapshot = freeze(store, run, selections)
    assert [v.id for v in snapshot.versions] == [second.version_id, first.version_id]
    assert snapshot.snapshot.rejected_inputs == (rejected,)
    store.revise_brief(run.id, store.load(run.id).revision, brief)
    new = freeze(store, run, selections, key="new-brief")
    assert new.snapshot.rejected_inputs == ()
    assert new.observations == (second, first)
    assert store.load_snapshot(snapshot.snapshot.id) == snapshot


def test_content_hash_collision_cannot_reuse_a_different_text(store, run, monkeypatch):
    import query.store as store_module

    first = ingest(store, run, supplied(text="aaa"))
    original = store_module.ingest_identity

    def collide(request):
        fields = original(request)
        fields["content_hash"] = first.content_hash
        return fields

    monkeypatch.setattr(store_module, "ingest_identity", collide)
    before = counts(store), store.load(run.id)
    for key in ["import-1", "new"]:
        with pytest.raises(ValueError, match="collision"):
            ingest(store, run, supplied(key, "bbb"))
        assert (counts(store), store.load(run.id)) == before


def test_migration_upgrade_preserves_existing_investigation(database, store, run):
    from alembic import command

    engine, config = database
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0001")
        command.upgrade(config, "head")
    assert store.load(run.id) == run
    assert ingest(store, run).status == "accepted"


@pytest.mark.parametrize("target", [Phase.SCOPE, Phase.PLANNING, Phase.GATHERING, Phase.ASSESSING])
def test_evidence_mutations_preserve_phase_and_budget(store, run, target):
    state = run
    for phase in [Phase.PLANNING, Phase.GATHERING, Phase.ASSESSING]:
        if state.checkpoint.phase == target:
            break
        state = store.move(run.id, state.revision, phase)
    original = state.checkpoint
    observation = ingest(store, run)
    loaded = freeze(store, run, (CandidateSelection(version_id=observation.version_id),))
    state = store.load(run.id)
    assert state.checkpoint == original
    assert state.revision == loaded.snapshot.revision == observation.revision + 1
    assert [e.kind for e in state.events[-2:]] == ["evidence_ingested", "snapshot_frozen"]
