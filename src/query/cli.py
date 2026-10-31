"""Explicit lifecycle commands; fixture execution makes no provider requests."""

import argparse
import sys
from pathlib import Path
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from query.config import Settings
from query.db import database_engine
from query.evidence import FreezeSnapshot, IngestText
from query.investigation import (
    Brief,
    CompletionChecks,
    Contract,
    Investigation,
    Lane,
    Limits,
    Outcome,
    Phase,
    StepRequest,
    StepResult,
)
from query.store import InvestigationStore, StaleRevision


class FixtureStep(Contract):
    key: str = Field(min_length=1)
    lane: Lane
    reserved_tokens: int = Field(gt=0, strict=True)
    result: StepResult


def read_json(path: Path, model):
    return model.model_validate_json(path.read_bytes())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="query", description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create", help="Create an investigation from brief and limits JSON"
    )
    create.add_argument("--brief", type=Path, required=True)
    create.add_argument("--limits", type=Path, required=True)
    help_text = {
        "inspect": "Read checkpoint, step results and decision history",
        "export": "Export lifecycle and exact saved evidence as audit JSON",
        "ingest": "Save a supplied UTF-8 document with explicit provenance",
        "freeze": "Freeze an explicit evidence selection into an immutable snapshot",
        "advance": "Record a phase change or execute one simulated fixture step",
        "resume": "Explicitly resume held work, optionally increasing allowances",
        "revise": "Append a new brief version or provide clarification",
    }
    for command, help_message in help_text.items():
        cmd = commands.add_parser(command, help=help_message)
        cmd.add_argument("id", type=UUID)
        if command in {"ingest", "freeze", "advance", "resume", "revise"}:
            cmd.add_argument("--revision", type=int, required=True, help="Revision from inspect")
        if command == "export":
            cmd.add_argument("--output", type=Path, help="Write a new file; default is stdout")
        elif command == "ingest":
            cmd.add_argument("--file", type=Path, required=True)
            cmd.add_argument("--operation-key", required=True)
            cmd.add_argument("--document-key", required=True)
            cmd.add_argument("--title", required=True)
            cmd.add_argument("--observed-at", required=True, help="ISO timestamp with timezone")
            cmd.add_argument("--published-at")
            cmd.add_argument("--origin-url")
            cmd.add_argument("--max-bytes", type=int, default=1024 * 1024)
        elif command == "freeze":
            cmd.add_argument("--request", type=Path, required=True, help="FreezeSnapshot JSON")
        elif command == "advance":
            mode = cmd.add_mutually_exclusive_group(required=True)
            mode.add_argument("--to", choices=[p.value for p in Phase])
            mode.add_argument("--fixture", type=Path, help="Execute one explicit simulated step")
            cmd.add_argument("--reason")
            cmd.add_argument("--checks", type=Path, help="Caller-supplied CompletionChecks JSON")
            cmd.add_argument("--outcome", choices=[o.value for o in Outcome])
        elif command == "resume":
            cmd.add_argument("--limits", type=Path, help="Explicit new absolute allowances JSON")
        elif command == "revise":
            cmd.add_argument("--brief", type=Path, required=True)
    return root


def run_fixture(store: InvestigationStore, args) -> Investigation:
    fixture = read_json(args.fixture, FixtureStep)
    # Persist the expected result as input so a changed fixture cannot reconcile old work.
    request = StepRequest(
        key=fixture.key,
        kind="fixture",
        lane=fixture.lane,
        reserved_tokens=fixture.reserved_tokens,
        inputs={"fixture": True, "result": fixture.result.model_dump(mode="json")},
    )
    current = store.load(args.id)
    if current.revision != args.revision:
        raise StaleRevision(
            f"Expected revision {args.revision}; current revision is {current.revision}"
        )
    if current.checkpoint.phase in {
        Phase.PAUSED,
        Phase.PARKED,
        Phase.STOPPED_AT_LIMIT,
        Phase.AWAITING_INPUT,
        Phase.COMPLETE,
        Phase.CANCELED,
    }:
        raise ValueError("Fixture execution requires an active phase; resume held work first")
    planned = store.plan_step(args.id, args.revision, request)
    result = fixture.result.model_copy(
        update={"output": {"fixture": True, "data": fixture.result.output}}
    )
    return store.finish_step(args.id, planned.revision, request.key, result)


def execute(store: InvestigationStore, args):
    if args.command == "create":
        return store.create(read_json(args.brief, Brief), read_json(args.limits, Limits))
    if args.command == "inspect":
        return store.load(args.id)
    if args.command == "export":
        return store.export(args.id)
    if args.command == "ingest":
        # Decode bytes directly to preserve CRLF, whitespace and Unicode exactly.
        request = IngestText(
            operation_key=args.operation_key,
            document_key=args.document_key,
            title=args.title,
            text=args.file.read_bytes().decode("utf-8"),
            observed_at=args.observed_at,
            published_at=args.published_at,
            origin_url=args.origin_url,
            max_bytes=args.max_bytes,
        )
        return store.ingest_text(args.id, args.revision, request)
    if args.command == "freeze":
        return store.freeze_snapshot(
            args.id, args.revision, read_json(args.request, FreezeSnapshot)
        )
    if args.command == "revise":
        return store.revise_brief(args.id, args.revision, read_json(args.brief, Brief))
    if args.command == "resume":
        current = store.load(args.id)
        if current.revision != args.revision:
            raise StaleRevision(
                f"Expected revision {args.revision}; current revision is {current.revision}"
            )
        if current.checkpoint.phase not in {Phase.PAUSED, Phase.PARKED, Phase.STOPPED_AT_LIMIT}:
            raise ValueError("Only paused, parked, or limit-stopped work can resume")
        if args.limits:
            current = store.grant(args.id, args.revision, read_json(args.limits, Limits))
        return store.resume(args.id, current.revision)
    if args.fixture:
        if args.reason or args.checks or args.outcome:
            raise ValueError("--reason, --checks and --outcome require --to, not --fixture")
        return run_fixture(store, args)
    target = Phase(args.to)
    if target != Phase.COMPLETE and (args.checks or args.outcome):
        raise ValueError("--checks and --outcome require --to complete")
    return store.move(
        args.id,
        args.revision,
        target,
        reason=args.reason,
        completion=read_json(args.checks, CompletionChecks) if args.checks else None,
        outcome=Outcome(args.outcome) if args.outcome else None,
    )


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    engine = None
    try:
        try:
            settings = Settings()
        except ValidationError:
            raise ValueError(
                "Set QUERY_DATABASE_URL to an explicit PostgreSQL database URL"
            ) from None
        engine = database_engine(str(settings.database_url))
        result = execute(InvestigationStore(engine), args)
        rendered = result.model_dump_json(indent=2) + "\n"
        if args.command == "export" and args.output:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 0
    except StaleRevision as exc:
        print(f"query: {exc}. Run inspect and retry with its revision.", file=sys.stderr)
    except KeyError as exc:
        print(f"query: Record not found: {exc.args[0]}", file=sys.stderr)
    except SQLAlchemyError:
        print(
            "query: Database operation failed; check connectivity and apply migrations.",
            file=sys.stderr,
        )
    except (ValueError, OSError) as exc:
        print(f"query: {exc}", file=sys.stderr)
    finally:
        if engine is not None:
            engine.dispose()
    return 1
