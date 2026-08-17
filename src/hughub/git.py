from __future__ import annotations

from pathlib import Path

from .errors import HugHubError
from .process import Runner


def output(runner: Runner, args: list[str], cwd: Path | None = None) -> str:
    return runner.run(args, cwd=cwd, check=True).stdout.strip()


def head_sha(runner: Runner, cwd: Path | None = None) -> str:
    return output(runner, ["git", "rev-parse", "HEAD"], cwd)


def current_branch(runner: Runner, cwd: Path | None = None) -> str:
    branch = output(runner, ["git", "branch", "--show-current"], cwd)
    if not branch:
        raise HugHubError("HEAD is detached; check out a branch before creating a pull request.")
    return branch


def remote_url(runner: Runner, name: str, cwd: Path | None = None) -> str | None:
    result = runner.run(["git", "remote", "get-url", name], cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def set_remote(runner: Runner, name: str, url: str, cwd: Path | None = None) -> None:
    if remote_url(runner, name, cwd):
        runner.run(["git", "remote", "set-url", name, url], cwd=cwd, check=True)
    else:
        runner.run(["git", "remote", "add", name, url], cwd=cwd, check=True)


def sync_to_hughub(
    runner: Runner,
    *,
    github_remote: str,
    hughub_remote: str,
    cwd: Path | None = None,
    fetch: bool = True,
) -> None:
    """Mirror ordinary branches and tags without deleting HF-side PR refs."""

    if fetch:
        runner.run(["git", "fetch", "--prune", github_remote], cwd=cwd, check=True)
        branches = output(
            runner,
            [
                "git",
                "for-each-ref",
                "--format=%(refname:strip=3)",
                f"refs/remotes/{github_remote}/",
            ],
            cwd,
        )
        refspecs = [
            f"+refs/remotes/{github_remote}/{branch}:refs/heads/{branch}"
            for branch in branches.splitlines()
            if branch and branch != "HEAD"
        ]
        if refspecs:
            runner.run(["git", "push", hughub_remote, *refspecs], cwd=cwd, check=True)
    else:
        runner.run(["git", "push", hughub_remote, "--all"], cwd=cwd, check=True)
    runner.run(["git", "push", hughub_remote, "--tags"], cwd=cwd, check=True)


def switch_origin(runner: Runner, target_url: str, cwd: Path | None = None) -> None:
    set_remote(runner, "origin", target_url, cwd)
