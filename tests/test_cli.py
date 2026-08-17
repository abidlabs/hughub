from __future__ import annotations

from hughub import cli
from hughub.config import RepoConfig, load_config, save_config
from hughub.process import Result

from .conftest import FakeRunner


def configured_runner(tmp_path, *, mode="github", scripted=None):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    runner = FakeRunner(git_dir, scripted)
    save_config(RepoConfig("acme/widget", "acme/widget", mode=mode), runner, tmp_path)
    return runner


def test_successful_command_is_passed_to_gh(tmp_path, capsys):
    command = ("gh", "pr", "list", "--json", "number")
    runner = configured_runner(
        tmp_path, scripted={command: Result(command, 0, '[{"number":1}]\n', "")}
    )

    result = cli.run(["pr", "list", "--json", "number"], runner=runner, cwd=tmp_path)

    assert result == 0
    assert capsys.readouterr().out == '[{"number":1}]\n'
    assert command in runner.calls


def test_transient_github_failure_automatically_uses_hughub(tmp_path, monkeypatch, capsys):
    command = ("gh", "pr", "create", "--fill")
    runner = configured_runner(
        tmp_path, scripted={command: Result(command, 1, "", "HTTP 503: Service Unavailable\n")}
    )
    dispatched: list[list[str]] = []

    def fake_dispatch(args, _runner, _cwd):
        dispatched.append(args)
        return 0

    monkeypatch.setattr(cli, "_hub_dispatch", fake_dispatch)

    result = cli.run(["pr", "create", "--fill"], runner=runner, cwd=tmp_path)

    assert result == 0
    assert dispatched == [["pr", "create", "--fill"]]
    assert load_config(runner, tmp_path).mode == "overlay"
    assert "continuing on HugHub" in capsys.readouterr().err


def test_user_error_does_not_trigger_failover(tmp_path):
    command = ("gh", "pr", "create", "--fill")
    runner = configured_runner(
        tmp_path, scripted={command: Result(command, 1, "", "GraphQL: Head sha can't be blank")}
    )

    result = cli.run(["pr", "create", "--fill"], runner=runner, cwd=tmp_path)

    assert result == 1
    assert load_config(runner, tmp_path).mode == "github"
