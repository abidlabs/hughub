from __future__ import annotations

from hughub.config import RepoConfig, load_config, save_config
from hughub.continuity import promote, recover
from hughub.process import Result

from .conftest import FakeRunner


def test_full_failover_switches_origin_without_contacting_github(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    runner = FakeRunner(git_dir)
    config = RepoConfig(
        "acme/widget", "hf-acme/widget", recovery_base="base", last_mirrored_sha="base"
    )
    save_config(config, runner, tmp_path)
    runner.scripted[("git", "remote", "get-url", "hughub")] = Result(
        ("git",), 0, "https://huggingface.co/hf-acme/widget\n", ""
    )
    runner.scripted[("git", "remote", "get-url", "origin")] = Result(
        ("git",), 0, "https://github.com/acme/widget.git\n", ""
    )

    promoted = promote(runner, "hughub", tmp_path)

    assert promoted.mode == "hughub"
    assert ("git", "push", "hughub", "--all") in runner.calls
    assert (
        "git",
        "remote",
        "set-url",
        "origin",
        "https://huggingface.co/hf-acme/widget",
    ) in runner.calls


def test_recovery_pushes_patch_branch_and_opens_pr(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    runner = FakeRunner(git_dir)
    config = RepoConfig(
        "acme/widget",
        "hf-acme/widget",
        mode="hughub",
        recovery_base="base",
        promoted_at="2026-08-17T10:00:00Z",
    )
    save_config(config, runner, tmp_path)
    runner.scripted[("git", "remote", "get-url", "github")] = Result(
        ("git",), 0, "https://github.com/acme/widget.git\n", ""
    )
    runner.scripted[("git", "remote", "get-url", "origin")] = Result(
        ("git",), 0, "https://huggingface.co/hf-acme/widget\n", ""
    )

    branch = recover(runner, create_pr=True, branch_name="recover-me", cwd=tmp_path)

    assert branch == "recover-me"
    assert ("git", "fetch", "hughub", "refs/heads/main") in runner.calls
    assert ("git", "push", "github", "FETCH_HEAD:refs/heads/recover-me") in runner.calls
    gh_calls = [call for call in runner.calls if call[:3] == ("gh", "pr", "create")]
    assert len(gh_calls) == 1
    assert load_config(runner, tmp_path).mode == "github"
