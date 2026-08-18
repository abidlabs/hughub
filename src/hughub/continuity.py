from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .automation import provision_automation, set_automation
from .config import RepoConfig, load_config, now_iso, save_config
from .errors import HugHubError
from .git import head_sha, output, remote_url, set_remote, switch_origin, sync_to_hughub
from .process import Runner
from .space import publish_space


def _github_repo(runner: Runner, supplied: str | None, cwd: Path | None) -> str:
    if supplied:
        return supplied
    result = runner.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd=cwd
    )
    if result.returncode:
        raise HugHubError("Could not identify the GitHub repository; pass OWNER/REPO explicitly.")
    return result.stdout.strip()


def _default_branch(runner: Runner, repo: str, cwd: Path | None) -> str:
    result = runner.run(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        cwd=cwd,
    )
    return result.stdout.strip() if result.returncode == 0 else "main"


def _is_private(runner: Runner, repo: str, cwd: Path | None) -> bool:
    result = runner.run(
        ["gh", "repo", "view", repo, "--json", "isPrivate", "--jq", ".isPrivate"],
        cwd=cwd,
    )
    if result.returncode:
        # A failed visibility check must never turn a private codebase public.
        return True
    return result.stdout.strip().lower() != "false"


def enable(
    runner: Runner,
    *,
    github_repo: str | None,
    hf_repo: str | None,
    space_repo: str | None,
    private: bool | None,
    no_push: bool,
    no_automation: bool,
    cwd: Path | None = None,
) -> RepoConfig:
    github_repo = _github_repo(runner, github_repo, cwd)
    hf_repo = hf_repo or github_repo
    if private is None:
        private = _is_private(runner, github_repo, cwd)
    origin = remote_url(runner, "origin", cwd)
    github_url = (
        origin if origin and "github" in origin else f"https://github.com/{github_repo}.git"
    )
    hf_url = f"https://huggingface.co/{hf_repo}"

    create = ["hf", "repos", "create", hf_repo, "--repo-type", "model", "--exist-ok"]
    create.append("--private" if private else "--public")
    runner.run(create, cwd=cwd, check=True)
    set_remote(runner, "github", github_url, cwd)
    set_remote(runner, "hughub", hf_url, cwd)

    config = RepoConfig(
        github_repo=github_repo,
        hf_repo=hf_repo,
        space_repo=space_repo or hf_repo,
        default_branch=_default_branch(runner, github_repo, cwd),
        recovery_base=head_sha(runner, cwd),
        private=private,
    )
    if not no_push:
        sync_to_hughub(
            runner,
            github_remote=config.github_remote,
            hughub_remote=config.hughub_remote,
            cwd=cwd,
            fetch=True,
        )
        config.last_mirrored_sha = output(
            runner,
            [
                "git",
                "rev-parse",
                f"refs/remotes/{config.github_remote}/{config.default_branch}",
            ],
            cwd,
        )
        config.recovery_base = config.last_mirrored_sha
    publish_space(config, runner, private=private, cwd=cwd or Path.cwd())
    save_config(config, runner, cwd)
    if not no_automation:
        provision_automation(config)
        save_config(config, runner, cwd)
    return config


def sync(runner: Runner, cwd: Path | None = None) -> RepoConfig:
    config = load_config(runner, cwd)
    assert config is not None
    if config.mode != "github":
        raise HugHubError(
            "Refusing to mirror GitHub over a promoted HugHub repository. "
            "Run `hh recover` or reconcile the histories first."
        )
    sync_to_hughub(
        runner,
        github_remote=config.github_remote,
        hughub_remote=config.hughub_remote,
        cwd=cwd,
        fetch=True,
    )
    config.last_mirrored_sha = output(
        runner,
        [
            "git",
            "rev-parse",
            f"refs/remotes/{config.github_remote}/{config.default_branch}",
        ],
        cwd,
    )
    if config.mode == "github":
        config.recovery_base = config.last_mirrored_sha
    if config.space_repo:
        publish_space(config, runner, private=config.private, cwd=cwd or Path.cwd())
    save_config(config, runner, cwd)
    return config


def promote(runner: Runner, mode: str, cwd: Path | None = None) -> RepoConfig:
    config = load_config(runner, cwd)
    assert config is not None
    if mode not in {"overlay", "hughub"}:
        raise HugHubError("Failover mode must be `overlay` or `hughub`.")

    # Preserve everything available in the local checkout even if GitHub cannot be fetched.
    runner.run(["git", "push", config.hughub_remote, "--all"], cwd=cwd, check=True)
    runner.run(["git", "push", config.hughub_remote, "--tags"], cwd=cwd, check=True)
    if not config.recovery_base:
        config.recovery_base = config.last_mirrored_sha or head_sha(runner, cwd)
    config.mode = mode  # type: ignore[assignment]
    config.promoted_at = now_iso()
    if mode == "hughub":
        hf_url = remote_url(runner, config.hughub_remote, cwd)
        if not hf_url:
            raise HugHubError("The HugHub git remote is missing.")
        switch_origin(runner, hf_url, cwd)
    set_automation(config, enabled=True)
    if config.space_repo:
        publish_space(config, runner, private=config.private, cwd=cwd or Path.cwd())
    save_config(config, runner, cwd)
    return config


def recover(
    runner: Runner,
    *,
    create_pr: bool,
    branch_name: str | None,
    cwd: Path | None = None,
) -> str:
    config = load_config(runner, cwd)
    assert config is not None
    if config.mode == "github":
        raise HugHubError("This repository is already using GitHub.")

    set_automation(config, enabled=False)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    recovery_branch = branch_name or f"hughub-recovery-{timestamp}"
    runner.run(
        ["git", "fetch", config.hughub_remote, f"refs/heads/{config.default_branch}"],
        cwd=cwd,
        check=True,
    )
    runner.run(
        ["git", "push", config.github_remote, f"FETCH_HEAD:refs/heads/{recovery_branch}"],
        cwd=cwd,
        check=True,
    )

    if create_pr:
        base = config.default_branch
        body = (
            "Recovery of work completed on HugHub while GitHub was unavailable.\n\n"
            f"HugHub repository: https://huggingface.co/{config.hf_repo}\n"
            f"Recovery base: `{config.recovery_base or 'unknown'}`\n"
            f"Promoted at: {config.promoted_at or 'unknown'}"
        )
        runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                config.github_repo,
                "--head",
                recovery_branch,
                "--base",
                base,
                "--title",
                "Recover work from HugHub",
                "--body",
                body,
            ],
            cwd=cwd,
            check=True,
        )

    github_url = remote_url(runner, config.github_remote, cwd)
    if not github_url:
        raise HugHubError("The GitHub git remote is missing.")
    switch_origin(runner, github_url, cwd)
    config.mode = "github"
    config.promoted_at = None
    if config.space_repo:
        publish_space(config, runner, private=config.private, cwd=cwd or Path.cwd())
    save_config(config, runner, cwd)
    return recovery_branch


def render_status(config: RepoConfig, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(asdict(config), indent=2, sort_keys=True)
    lines = [
        f"Mode:            {config.mode}",
        f"GitHub:          {config.github_repo}",
        f"HugHub:          {config.hf_repo}",
        f"Static Space:    {config.space_repo or 'not configured'}",
        f"HF automation:   {'enabled' if config.automation_enabled else 'standby'}",
        f"Last mirror:     {config.last_mirrored_sha or 'not yet mirrored'}",
        f"Recovery base:   {config.recovery_base or 'not recorded'}",
    ]
    if config.promoted_at:
        lines.append(f"Promoted at:     {config.promoted_at}")
    return "\n".join(lines)


def warn_auto_failover(command: list[str]) -> None:
    print(
        f"GitHub is unavailable for `gh {' '.join(command)}`; continuing on HugHub.",
        file=sys.stderr,
    )
