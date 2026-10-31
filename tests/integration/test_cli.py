"""Each invocation starts a fresh Python process against one disposable schema."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from query.cli import FixtureStep
from query.evidence import text_identity
from query.export import InvestigationExport


@pytest.fixture
def cli(database, tmp_path):
    engine, _ = database
    with engine.connect() as conn:
        schema = conn.execute(text("select current_schema()")).scalar_one()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("QUERY_") and key not in {"PGOPTIONS", "PGSERVICE"}
    }
    env.update(
        QUERY_DATABASE_URL=engine.url.render_as_string(hide_password=False),
        PGOPTIONS=f"-csearch_path={schema}",
    )

    def invoke(*args, ok=True):
        completed = subprocess.run(
            [sys.executable, "-m", "query", *map(str, args)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if ok:
            assert completed.returncode == 0, completed.stderr
            assert completed.stderr == ""
            return json.loads(completed.stdout) if completed.stdout else None
        assert completed.returncode != 0
        assert completed.stdout == ""
        assert "Traceback" not in completed.stderr
        return completed.stderr

    return invoke


def write_json(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def created(cli, tmp_path):
    brief = write_json(
        tmp_path,
        "brief.json",
        {
            "question": "What capacity is operational?",
            "scope": "Supplied documents only",
            "cutoff": "2026-10-01",
            "questions": [
                {
                    "key": "capacity",
                    "text": "How much?",
                    "acceptance_criteria": "Dated operational MW",
                }
            ],
        },
    )
    limits = write_json(tmp_path, "limits.json", {"research": 100, "review": 100, "total": 200})
    return cli("create", "--brief", brief, "--limits", limits)


def advance(cli, run, *args, ok=True):
    return cli("advance", run["id"], "--revision", run["revision"], *args, ok=ok)


def test_supplied_document_survives_processes_pause_resume_and_export(cli, created, tmp_path):
    run = created
    identifier = run["id"]
    source = tmp_path / "document.txt"
    exact = "Aster operates 900 MW.\r\nExpansion is planned.  \n"
    source.write_bytes(exact.encode())
    observation = cli(
        "ingest",
        identifier,
        "--revision",
        run["revision"],
        "--file",
        source,
        "--operation-key",
        "supply-1",
        "--document-key",
        f"synthetic-{identifier}",
        "--title",
        "Capacity",
        "--observed-at",
        "2026-10-01T12:00:00Z",
    )
    run = cli("inspect", identifier)
    assert observation["content_hash"] == text_identity(exact).content_hash
    freeze = write_json(
        tmp_path,
        "freeze.json",
        {
            "operation_key": "freeze-1",
            "candidates": [{"version_id": observation["version_id"]}],
        },
    )
    snapshot = cli("freeze", identifier, "--revision", run["revision"], "--request", freeze)
    assert snapshot["snapshot"]["status"] == "ready"
    run = cli("inspect", identifier)
    run = advance(cli, run, "--to", "planning")
    run = advance(cli, run, "--to", "reviewing")
    fixture = write_json(
        tmp_path,
        "step.json",
        {
            "key": "fixture-answer-1",
            "lane": "review",
            "reserved_tokens": 20,
            "result": {
                "status": "succeeded",
                "used_tokens": 10,
                "output": {
                    "answer": "Synthetic answer: 900 MW operational; expansion planned.",
                    "snapshot_id": snapshot["snapshot"]["id"],
                    "decision": "Fixture only; no integrity or sufficiency assessment.",
                },
            },
        },
    )
    run = advance(cli, run, "--fixture", fixture)
    replay = advance(cli, run, "--fixture", fixture)
    assert replay == run
    paused = advance(cli, run, "--to", "paused", "--reason", "Explicit fixture interruption")
    assert "active phase" in advance(cli, paused, "--fixture", fixture, ok=False)
    run = cli("resume", identifier, "--revision", paused["revision"])
    assert run["checkpoint"]["phase"] == "reviewing"
    assert run["checkpoint"]["budget"]["review_spent"] == 10
    export = cli("export", identifier)
    validated = InvestigationExport.model_validate(export)
    assert validated.source_versions[0].text == exact
    assert str(validated.snapshots[0].id) == snapshot["snapshot"]["id"]
    result = export["investigation"]["steps"][0]["result"]["output"]
    assert result["fixture"] is True and "900 MW" in result["data"]["answer"]
    assert [e["kind"] for e in export["investigation"]["events"]][-2:] == ["transition", "resumed"]
    assert export == cli("export", identifier)
    output = tmp_path / "audit.json"
    assert cli("export", identifier, "--output", output) is None
    assert json.loads(output.read_text()) == export
    assert "File exists" in cli("export", identifier, "--output", output, ok=False)
    assert json.loads(output.read_text()) == export


def test_errors_preserve_revision_and_explain_state(cli, created, tmp_path):
    run = created
    error = cli("resume", run["id"], "--revision", run["revision"], ok=False)
    assert "Only paused, parked, or limit-stopped" in error
    error = advance(cli, run, "--to", "complete", ok=False)
    assert "Cannot move from scope to complete" in error
    error = cli("advance", run["id"], "--revision", 99, "--to", "planning", ok=False)
    assert "Run inspect" in error and "current revision is 1" in error
    assert cli("inspect", run["id"]) == run
    assert "Record not found" in cli("inspect", uuid4(), ok=False)
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json")
    assert "Invalid JSON" in cli("freeze", run["id"], "--revision", 1, "--request", bad, ok=False)
    fixture = write_json(
        tmp_path,
        "fixture.json",
        {
            "key": "unused",
            "lane": "research",
            "reserved_tokens": 1,
            "result": {"status": "succeeded", "used_tokens": 0, "output": {}},
        },
    )
    assert "require --to" in advance(
        cli,
        run,
        "--fixture",
        fixture,
        "--reason",
        "Incompatible flag",
        ok=False,
    )
    assert cli("inspect", run["id"]) == run


def test_pending_fixture_reconciles_after_restart_and_rejects_changed_result(
    cli,
    created,
    database,
    tmp_path,
):
    from query.investigation import StepRequest
    from query.store import InvestigationStore

    run = created
    fixture_path = write_json(
        tmp_path,
        "pending.json",
        {
            "key": "pending",
            "lane": "research",
            "reserved_tokens": 10,
            "result": {
                "status": "succeeded",
                "used_tokens": 3,
                "output": {"fixture-note": "saved"},
            },
        },
    )
    fixture = FixtureStep.model_validate_json(fixture_path.read_bytes())
    store = InvestigationStore(database[0])
    identifier = UUID(run["id"])
    planned = store.plan_step(
        identifier,
        1,
        StepRequest(
            key=fixture.key,
            kind="fixture",
            lane=fixture.lane,
            reserved_tokens=fixture.reserved_tokens,
            inputs={"fixture": True, "result": fixture.result.model_dump(mode="json")},
        ),
    )
    # A fresh process finishes only the planned operation and charges it once.
    run = cli("inspect", identifier)
    changed = json.loads(fixture_path.read_text())
    changed["result"]["output"] = {"fixture-note": "changed"}
    other = write_json(tmp_path, "changed.json", changed)
    assert "different work" in advance(cli, run, "--fixture", other, ok=False)
    assert cli("inspect", identifier)["revision"] == planned.revision
    finished = advance(cli, run, "--fixture", fixture_path)
    assert finished["checkpoint"]["budget"]["research_spent"] == 3
    assert advance(cli, finished, "--fixture", fixture_path) == finished


def test_export_retains_rejected_unfrozen_versions_and_old_snapshot(cli, created, tmp_path):
    identifier = created["id"]
    versions = []
    for n, content in enumerate(("Old capacity.", "New capacity.", "Too large")):
        run = cli("inspect", identifier)
        path = tmp_path / "evidence.txt"
        path.write_text(content)
        observation = cli(
            "ingest",
            identifier,
            "--revision",
            run["revision"],
            "--file",
            path,
            "--operation-key",
            f"ingest-{n}",
            "--document-key",
            f"doc-{identifier}",
            "--title",
            "Capacity",
            "--observed-at",
            "2026-10-01T12:00:00Z",
            "--max-bytes",
            1 if n == 2 else 100,
        )
        versions.append(observation)
        if n == 0:
            request = write_json(
                tmp_path,
                "selection.json",
                {
                    "operation_key": "freeze-old",
                    "candidates": [
                        {
                            "version_id": observation["version_id"],
                            "passage_ids": [],
                            "exclusion_reason": "Synthetic explicit exclusion",
                        }
                    ],
                },
            )
            cli("freeze", identifier, "--revision", observation["revision"], "--request", request)
    audit = cli("export", identifier)
    assert len(audit["evidence_inputs"]) == 3
    assert {v["text"] for v in audit["source_versions"]} == {"Old capacity.", "New capacity."}
    assert audit["evidence_inputs"][-1]["reason"] == "document_too_large"
    assert audit["snapshots"][0]["candidates"][0]["version_id"] == versions[0]["version_id"]
    assert (
        audit["snapshots"][0]["candidates"][0]["passages"][0]["reason"]
        == "Synthetic explicit exclusion"
    )


def test_failed_fixture_limit_stop_grant_and_resume(cli, created, tmp_path):
    fixture = write_json(
        tmp_path,
        "failed.json",
        {
            "key": "failure",
            "lane": "research",
            "reserved_tokens": 100,
            "result": {
                "status": "failed",
                "used_tokens": 100,
                "output": {},
                "error": "Synthetic simulated failure",
            },
        },
    )
    stopped = advance(cli, created, "--fixture", fixture)
    assert stopped["checkpoint"]["phase"] == "stopped_at_limit"
    assert stopped["checkpoint"]["outcome"] is None
    limits = write_json(tmp_path, "grant.json", {"research": 200, "review": 100, "total": 300})
    resumed = cli("resume", created["id"], "--revision", stopped["revision"], "--limits", limits)
    assert resumed["checkpoint"]["phase"] == "planning"
    assert resumed["checkpoint"]["budget"]["research_spent"] == 100
    assert resumed["steps"][0]["result"]["error"] == "Synthetic simulated failure"
    assert [e["kind"] for e in resumed["events"]][-2:] == ["budget_granted", "resumed"]


def test_clarification_and_explicit_completion_attestations(cli, created, tmp_path):
    held = advance(cli, created, "--to", "awaiting_input", "--reason", "Scope clarification")
    changed = created["briefs"][0]["brief"]
    changed["scope"] = "Clarified supplied corpus"
    brief = write_json(tmp_path, "changed-brief.json", changed)
    revised = cli("revise", created["id"], "--revision", held["revision"], "--brief", brief)
    assert revised["checkpoint"]["phase"] == "scope"
    assert revised["checkpoint"]["brief_version"] == 2
    run = advance(cli, revised, "--to", "planning")
    run = advance(cli, run, "--to", "reviewing")
    checks = {
        "brief_version": 2,
        "answer_ref": "caller-fixture-answer",
        "answer_integrity": True,
        "research_sufficiency": False,
        "rationale": "Explicit synthetic attestations for lifecycle testing",
    }
    path = write_json(tmp_path, "checks.json", checks)
    assert "Both completion checks" in advance(
        cli,
        run,
        "--to",
        "complete",
        "--checks",
        path,
        "--outcome",
        "qualified",
        ok=False,
    )
    checks["research_sufficiency"] = True
    path.write_text(json.dumps(checks))
    complete = advance(cli, run, "--to", "complete", "--checks", path, "--outcome", "qualified")
    assert complete["checkpoint"]["outcome"] == "qualified"
    audit = cli("export", run["id"])
    assert audit["investigation"]["checkpoint"]["completion"] == checks
    assert "Only paused, parked" in cli(
        "resume",
        run["id"],
        "--revision",
        complete["revision"],
        ok=False,
    )


def test_checked_in_example_runs_without_model_credentials(database, tmp_path):
    engine, _ = database
    with engine.connect() as conn:
        schema = conn.execute(text("select current_schema()")).scalar_one()
    env = {k: v for k, v in os.environ.items() if not k.startswith("QUERY_")}
    env.update(
        QUERY_DATABASE_URL=engine.url.render_as_string(hide_password=False),
        PGOPTIONS=f"-csearch_path={schema}",
    )
    example = Path(__file__).resolve().parents[2] / "examples" / "supplied" / "run.py"
    result = subprocess.run(
        [sys.executable, str(example)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    audit = InvestigationExport.model_validate_json(result.stdout)
    assert audit.investigation.checkpoint.phase == "reviewing"
    assert audit.investigation.steps[0].result.output["fixture"] is True
    assert audit.snapshots[0].status == "ready"
    assert audit.source_versions[0].text == (example.parent / "announcement.txt").read_text()
