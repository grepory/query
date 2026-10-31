"""Run each CLI command in a fresh process, printing a self-contained audit export."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent


def command(*args):
    result = subprocess.run(
        [sys.executable, "-m", "query", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main():
    run = command("create", "--brief", HERE / "brief.json", "--limits", HERE / "limits.json")
    identifier = run["id"]
    observation = command(
        "ingest",
        identifier,
        "--revision",
        run["revision"],
        "--file",
        HERE / "announcement.txt",
        "--operation-key",
        "supply-1",
        "--document-key",
        f"synthetic-capacity-{identifier}",
        "--title",
        "Aster capacity fixture",
        "--observed-at",
        "2026-10-01T12:00:00Z",
    )
    with TemporaryDirectory() as temporary:
        request = Path(temporary) / "snapshot.json"
        request.write_text(
            json.dumps(
                {
                    "operation_key": "freeze-1",
                    "candidates": [{"version_id": observation["version_id"]}],
                }
            )
        )
        frozen = command(
            "freeze",
            identifier,
            "--revision",
            observation["revision"],
            "--request",
            request,
        )
        run = command("inspect", identifier)
        for phase in ("planning", "reviewing"):
            run = command("advance", identifier, "--revision", run["revision"], "--to", phase)
        fixture = json.loads((HERE / "step.json").read_text())
        fixture["result"]["output"]["snapshot_id"] = frozen["snapshot"]["id"]
        request.write_text(json.dumps(fixture))
        run = command("advance", identifier, "--revision", run["revision"], "--fixture", request)
    run = command(
        "advance",
        identifier,
        "--revision",
        run["revision"],
        "--to",
        "paused",
        "--reason",
        "Explicit fixture interruption",
    )
    command("resume", identifier, "--revision", run["revision"])
    print(json.dumps(command("export", identifier), indent=2))


if __name__ == "__main__":
    main()
