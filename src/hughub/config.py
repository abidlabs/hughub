from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .errors import HugHubError
from .process import Runner

Mode = Literal["github", "overlay", "hughub"]


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class RepoConfig:
    github_repo: str
    hf_repo: str
    space_repo: str | None = None
    mode: Mode = "github"
    github_remote: str = "github"
    hughub_remote: str = "hughub"
    enabled_at: str = field(default_factory=now_iso)
    promoted_at: str | None = None
    recovery_base: str | None = None
    last_mirrored_sha: str | None = None
    default_branch: str = "main"
    dispatcher_job_id: str | None = None
    webhook_id: str | None = None
    automation_jobs: list[str] = field(default_factory=list)
    automation_webhooks: list[str] = field(default_factory=list)
    automation_revision: str | None = None
    automation_enabled: bool = False
    private: bool = False
    version: int = 3


def git_dir(runner: Runner, cwd: Path | None = None) -> Path:
    result = runner.run(["git", "rev-parse", "--absolute-git-dir"], cwd=cwd)
    if result.returncode:
        raise HugHubError("This command must be run inside a Git repository.")
    return Path(result.stdout.strip())


def config_path(runner: Runner, cwd: Path | None = None) -> Path:
    return git_dir(runner, cwd) / "hughub" / "config.json"


def load_config(
    runner: Runner, cwd: Path | None = None, *, required: bool = True
) -> RepoConfig | None:
    try:
        path = config_path(runner, cwd)
    except HugHubError:
        if required:
            raise
        return None
    if not path.exists():
        if required:
            raise HugHubError("HugHub continuity is not enabled. Run `hh continuity enable` first.")
        return None
    try:
        data = json.loads(path.read_text())
        return RepoConfig(**data)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise HugHubError(f"Could not read {path}: {exc}") from exc


def save_config(config: RepoConfig, runner: Runner, cwd: Path | None = None) -> Path:
    path = config_path(runner, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(asdict(config), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path
