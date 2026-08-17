from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi

from .config import RepoConfig
from .errors import HugHubError
from .git import current_branch, output
from .process import Runner


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, dict) and name in obj:
            return obj[name]
    return default


def _author(item: Any) -> str:
    author = _value(item, "author", default="")
    return str(_value(author, "name", "username", default=author or "unknown"))


def _discussion_dict(item: Any) -> dict[str, Any]:
    return {
        "number": _value(item, "num"),
        "title": _value(item, "title"),
        "state": _value(item, "status"),
        "author": _author(item),
        "isPullRequest": bool(_value(item, "is_pull_request", "isPullRequest", default=False)),
        "url": _value(item, "url", "html_url", default=""),
    }


class HubBackend:
    def __init__(
        self,
        config: RepoConfig,
        runner: Runner,
        *,
        api: HfApi | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.api = api or HfApi()
        self.cwd = cwd

    def _print_items(self, items: Iterable[Any], *, json_fields: str | None = None) -> int:
        values = [_discussion_dict(item) for item in items]
        if json_fields is not None:
            requested = [field.strip() for field in json_fields.split(",") if field.strip()]
            print(json.dumps([{key: row.get(key) for key in requested} for row in values]))
        else:
            for row in values:
                kind = "PR" if row["isPullRequest"] else "ISSUE"
                print(f"{row['number']}\t{row['state']}\t{row['title']}\t{kind}")
        return 0

    @staticmethod
    def _option(args: list[str], *names: str) -> str | None:
        for name in names:
            if name in args:
                index = args.index(name)
                if index + 1 >= len(args):
                    raise HugHubError(f"{name} requires a value")
                return args[index + 1]
        return None

    @staticmethod
    def _number(args: list[str]) -> int:
        for value in args:
            if not value.startswith("-") and value.isdigit():
                return int(value)
        raise HugHubError("A pull request or issue number is required.")

    def dispatch(self, args: list[str]) -> int:
        if not args:
            raise HugHubError("A HugHub command is required.")
        if args[0] == "pr":
            return self.pr(args[1:])
        if args[0] == "issue":
            return self.issue(args[1:])
        if args[0] == "run":
            return self.run(args[1:])
        raise HugHubError(f"`hh {args[0]}` is not available on the HugHub backend yet.")

    def pr(self, args: list[str]) -> int:
        action = args[0] if args else "list"
        rest = args[1:]
        if action in {"list", "ls"}:
            items = self.api.get_repo_discussions(
                self.config.hf_repo, discussion_type="pull_request", repo_type="model"
            )
            return self._print_items(items, json_fields=self._option(rest, "--json"))
        if action == "create":
            title = self._option(rest, "--title", "-t")
            body = self._option(rest, "--body", "-b")
            if "--fill" in rest or not title:
                title = title or output(self.runner, ["git", "log", "-1", "--pretty=%s"], self.cwd)
                body = body or output(self.runner, ["git", "log", "-1", "--pretty=%b"], self.cwd)
            branch = self._option(rest, "--head", "-H") or current_branch(self.runner, self.cwd)
            if self.config.mode == "overlay":
                # Git often remains healthy during an API outage. Refresh the base branch so
                # the HF-side diff is accurate without depending on GitHub's API.
                fetched = self.runner.run(
                    [
                        "git",
                        "fetch",
                        self.config.github_remote,
                        f"refs/heads/{self.config.default_branch}",
                    ],
                    cwd=self.cwd,
                )
                if fetched.returncode == 0:
                    self.runner.run(
                        [
                            "git",
                            "push",
                            self.config.hughub_remote,
                            f"FETCH_HEAD:refs/heads/{self.config.default_branch}",
                            "--force-with-lease",
                        ],
                        cwd=self.cwd,
                    )
            created = self.api.create_pull_request(
                self.config.hf_repo,
                title=title or branch,
                description=body or f"Changes from `{branch}`",
                repo_type="model",
            )
            number = int(_value(created, "num"))
            self.runner.run(
                ["git", "push", self.config.hughub_remote, f"HEAD:refs/pr/{number}"],
                cwd=self.cwd,
                check=True,
            )
            print(
                _value(
                    created,
                    "url",
                    default=f"https://huggingface.co/{self.config.hf_repo}/discussions/{number}",
                )
            )
            return 0
        if action in {"view", "status"}:
            item = self.api.get_discussion_details(
                self.config.hf_repo, self._number(rest), repo_type="model"
            )
            print(json.dumps(_discussion_dict(item), indent=2))
            return 0
        if action == "checkout":
            number = self._number(rest)
            branch = self._option(rest, "--branch", "-b") or f"pr-{number}"
            self.runner.run(
                ["git", "fetch", self.config.hughub_remote, f"refs/pr/{number}"],
                cwd=self.cwd,
                check=True,
            )
            self.runner.run(
                ["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=self.cwd, check=True
            )
            return 0
        if action == "comment":
            number = self._number(rest)
            body = self._option(rest, "--body", "-b")
            if not body:
                raise HugHubError("`hh pr comment` currently requires --body.")
            self.api.comment_discussion(self.config.hf_repo, number, body, repo_type="model")
            return 0
        if action == "merge":
            self.api.merge_pull_request(self.config.hf_repo, self._number(rest), repo_type="model")
            return 0
        if action == "close":
            self.api.change_discussion_status(
                self.config.hf_repo, self._number(rest), "closed", repo_type="model"
            )
            return 0
        raise HugHubError(f"`hh pr {action}` is not available on the HugHub backend yet.")

    def issue(self, args: list[str]) -> int:
        action = args[0] if args else "list"
        rest = args[1:]
        if action in {"list", "ls"}:
            items = self.api.get_repo_discussions(
                self.config.hf_repo, discussion_type="discussion", repo_type="model"
            )
            return self._print_items(items, json_fields=self._option(rest, "--json"))
        if action == "create":
            title = self._option(rest, "--title", "-t")
            body = self._option(rest, "--body", "-b")
            if not title:
                raise HugHubError("`hh issue create` currently requires --title.")
            created = self.api.create_discussion(
                self.config.hf_repo, title=title, description=body, repo_type="model"
            )
            print(_value(created, "url", default="Issue created on HugHub"))
            return 0
        if action == "view":
            item = self.api.get_discussion_details(
                self.config.hf_repo, self._number(rest), repo_type="model"
            )
            print(json.dumps(_discussion_dict(item), indent=2))
            return 0
        if action == "comment":
            number = self._number(rest)
            body = self._option(rest, "--body", "-b")
            if not body:
                raise HugHubError("`hh issue comment` currently requires --body.")
            self.api.comment_discussion(self.config.hf_repo, number, body, repo_type="model")
            return 0
        if action in {"close", "reopen"}:
            state = "closed" if action == "close" else "open"
            self.api.change_discussion_status(
                self.config.hf_repo, self._number(rest), state, repo_type="model"
            )
            return 0
        raise HugHubError(f"`hh issue {action}` is not available on the HugHub backend yet.")

    def run(self, args: list[str]) -> int:
        action = args[0] if args else "list"
        rest = args[1:]
        label = ["--label", f"hughub.repo={self.config.hf_repo.replace('/', '--')}"]
        if action in {"list", "ls"}:
            result = self.runner.run(["hf", "jobs", "list", "--all", *label, *rest], cwd=self.cwd)
        elif action in {"view", "watch", "cancel"}:
            if not rest:
                raise HugHubError(f"`hh run {action}` requires a run ID.")
            command = {"view": "inspect", "watch": "wait", "cancel": "cancel"}[action]
            result = self.runner.run(["hf", "jobs", command, *rest], cwd=self.cwd)
        else:
            raise HugHubError(f"`hh run {action}` is not available on the HugHub backend yet.")
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        return result.returncode
