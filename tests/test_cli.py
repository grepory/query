from uuid import uuid4

import pytest

from query.cli import main, parser


def test_help_needs_no_database_or_model_configuration(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert all(
        command in output for command in ("create", "inspect", "advance", "resume", "export")
    )


def test_missing_database_has_actionable_error_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["inspect", str(uuid4())]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Set QUERY_DATABASE_URL" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["advance", str(uuid4()), "--revision", "1"],
        ["advance", str(uuid4()), "--revision", "1", "--fixture", "f.json", "--to", "planning"],
        ["resume", str(uuid4())],
        ["inspect", "invalid-id"],
    ],
)
def test_invalid_command_shapes_fail_before_io(arguments):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(arguments)
    assert exc.value.code == 2
