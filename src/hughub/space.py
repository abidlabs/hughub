from __future__ import annotations

import html
import json
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi

from .automation import dispatcher_source
from .config import RepoConfig
from .errors import HugHubError
from .git import head_sha
from .process import Runner


def _git(runner: Runner, args: list[str], cwd: Path) -> str:
    result = runner.run(["git", *args], cwd=cwd, check=True)
    return result.stdout.strip()


def _commits(runner: Runner, cwd: Path) -> list[dict[str, str]]:
    raw = _git(
        runner,
        [
            "log",
            "-12",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1e",
        ],
        cwd,
    )
    commits: list[dict[str, str]] = []
    for record in raw.split("\x1e"):
        values = record.strip().split("\x1f")
        if len(values) == 5:
            commits.append(
                dict(zip(("sha", "shortSha", "author", "date", "subject"), values, strict=True))
            )
    return commits


def _readme(cwd: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = cwd / name
        if path.is_file():
            return path.read_text(errors="replace")[:100_000]
    return "No README was found in this repository."


def snapshot_data(config: RepoConfig, runner: Runner, cwd: Path) -> dict[str, Any]:
    revision = config.last_mirrored_sha or head_sha(runner, cwd)
    tree = _git(runner, ["ls-tree", "-r", "--name-only", revision], cwd).splitlines()
    return {
        "githubRepo": config.github_repo,
        "hfRepo": config.hf_repo,
        "spaceRepo": config.space_repo or config.hf_repo,
        "defaultBranch": config.default_branch,
        "sha": revision,
        "private": config.private,
        "mode": config.mode,
        "files": tree,
        "commits": _commits(runner, cwd),
        "readme": _readme(cwd),
    }


def render_index(data: dict[str, Any]) -> str:
    template = files("hughub.assets").joinpath("index.html").read_text()
    encoded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("__HUGHUB_DATA__", encoded)


def _space_readme(config: RepoConfig) -> str:
    return f"""---
title: HugHub · {config.github_repo}
emoji: 🛟
colorFrom: yellow
colorTo: gray
sdk: static
app_file: index.html
pinned: false
---

# HugHub standby for `{config.github_repo}`

This free Static Space is the read-only continuity UI for the git mirror at
[`{config.hf_repo}`](https://huggingface.co/{config.hf_repo}). It contains no secrets and
runs no server. Native Hugging Face webhooks launch ephemeral Jobs when HugHub is promoted.
"""


def publish_space(
    config: RepoConfig,
    runner: Runner,
    *,
    private: bool,
    cwd: Path,
    api: HfApi | None = None,
) -> str:
    api = api or HfApi()
    space_repo = config.space_repo or config.hf_repo
    config.space_repo = space_repo
    api.create_repo(
        repo_id=space_repo,
        repo_type="space",
        space_sdk="static",
        private=private,
        exist_ok=True,
    )
    data = snapshot_data(config, runner, cwd)
    with tempfile.TemporaryDirectory(prefix="hughub-space-") as temporary:
        directory = Path(temporary)
        (directory / "index.html").write_text(render_index(data))
        (directory / "README.md").write_text(_space_readme(config))
        (directory / "dispatcher.py").write_text(dispatcher_source())
        api.upload_folder(
            repo_id=space_repo,
            repo_type="space",
            folder_path=directory,
            commit_message=f"Update HugHub snapshot to {data['sha'][:12]}",
        )
    return f"https://huggingface.co/spaces/{space_repo}"


def space_url(config: RepoConfig) -> str:
    if not config.space_repo:
        raise HugHubError("No HugHub Static Space is configured for this repository.")
    return f"https://huggingface.co/spaces/{html.escape(config.space_repo, quote=True)}"
