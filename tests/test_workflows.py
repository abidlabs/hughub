from __future__ import annotations

from hughub.config import RepoConfig
from hughub.process import Result
from hughub.workflows import doctor, run_workflow

from .conftest import FakeRunner


def write_workflow(tmp_path, content):
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    path = directory / "test.yml"
    path.write_text(content)
    return path


def test_doctor_accepts_shell_workflow(tmp_path, capsys):
    write_workflow(
        tmp_path,
        """
name: test
on: [push, workflow_dispatch]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest
""",
    )

    result = doctor("test.yml", tmp_path)

    assert result == 0
    assert "compatible" in capsys.readouterr().out


def test_doctor_rejects_unsupported_action(tmp_path, capsys):
    write_workflow(
        tmp_path,
        """
on: push
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: vendor/unknown@v1
""",
    )

    assert doctor(None, tmp_path) == 1
    assert "not supported" in capsys.readouterr().out


def test_workflow_launches_hf_job_with_repo_labels(tmp_path, capsys):
    write_workflow(
        tmp_path,
        """
on: workflow_dispatch
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python -m pytest
""",
    )
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    runner = FakeRunner(git_dir)
    runner.scripted[("git", "branch", "--show-current")] = Result(("git",), 0, "feature\n", "")
    config = RepoConfig("acme/widget", "hf-acme/widget", mode="overlay")

    result = run_workflow(config, runner, name="test.yml", ref=None, cwd=tmp_path)

    assert result == 0
    jobs_calls = [call for call in runner.calls if call[:3] == ("hf", "jobs", "run")]
    assert len(jobs_calls) == 1
    assert "hughub-repo=hf-acme--widget" in jobs_calls[0]
    assert "Launched 1" in capsys.readouterr().out
