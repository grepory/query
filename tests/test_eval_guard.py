import pytest

from evals.smoke import main


def test_eval_requires_opt_in(monkeypatch, capsys):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr("sys.argv", ["smoke"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "opt into" in capsys.readouterr().err


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS"])
def test_eval_refuses_ci_even_with_opt_in(monkeypatch, capsys, variable):
    monkeypatch.setenv(variable, "true")
    monkeypatch.setattr("sys.argv", ["smoke", "--allow-model-requests"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "disabled in CI" in capsys.readouterr().err
