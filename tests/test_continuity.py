from __future__ import annotations

from hughub import continuity
from hughub.config import RepoConfig, load_config, save_config
from hughub.continuity import mirror, promote, recover
from hughub.process import Result

from .conftest import FakeRunner


class FakeApi:
    def __init__(self):
        self.created = []

    def whoami(self):
        return {"name": "hf-acme"}

    def create_repo(self, **kwargs):
        self.created.append(kwargs)


def test_mirror_pushes_head_and_configures_origin_for_both_hosts(tmp_path, monkeypatch):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    runner = FakeRunner(
        git_dir,
        {
            ("git", "branch", "--show-current"): Result(("git",), 0, "feature\n", ""),
            (
                "gh",
                "repo",
                "view",
                "--json",
                "nameWithOwner",
                "--jq",
                ".nameWithOwner",
            ): Result(("gh",), 0, "acme/widget\n", ""),
            (
                "gh",
                "repo",
                "view",
                "acme/widget",
                "--json",
                "isPrivate",
                "--jq",
                ".isPrivate",
            ): Result(("gh",), 0, "false\n", ""),
            ("git", "remote", "get-url", "origin"): Result(
                ("git",), 0, "git@github.com:acme/widget.git\n", ""
            ),
            ("git", "ls-tree", "-r", "--name-only", "a" * 40): Result(
                ("git",), 0, "README.md\n", ""
            ),
            (
                "git",
                "log",
                "-12",
                "--date=iso-strict",
                "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1e",
            ): Result(("git",), 0, "", ""),
        },
    )
    (tmp_path / "README.md").write_text("# Widget\n")
    api = FakeApi()
    published = []
    monkeypatch.setattr(
        continuity,
        "publish_space",
        lambda config, *_args, **_kwargs: published.append(config.space_repo),
    )

    config = mirror(runner, cwd=tmp_path, api=api)

    assert config.hf_repo == "hf-acme/widget"
    assert config.space_repo == "hf-acme/widget"
    assert ("git", "push", "hughub", "HEAD:refs/heads/feature") in runner.calls
    assert (
        "git",
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "https://huggingface.co/hf-acme/widget",
    ) in runner.calls
    assert (
        "git",
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "git@github.com:acme/widget.git",
    ) in runner.calls
    assert published == ["hf-acme/widget"]
    assert load_config(runner, tmp_path).last_mirrored_sha == "a" * 40


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
