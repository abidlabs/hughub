from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from hughub.process import Result


class FakeRunner:
    def __init__(
        self, git_dir: Path, scripted: dict[tuple[str, ...], Result] | None = None
    ) -> None:
        self.git_dir = git_dir
        self.scripted = scripted or {}
        self.calls: list[tuple[str, ...]] = []
        self.exec_calls: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str], **_kwargs: object) -> Result:
        command = tuple(str(arg) for arg in args)
        self.calls.append(command)
        if command == ("git", "rev-parse", "--absolute-git-dir"):
            return Result(command, 0, f"{self.git_dir}\n", "")
        if command == ("git", "rev-parse", "HEAD"):
            return Result(command, 0, "a" * 40 + "\n", "")
        return self.scripted.get(command, Result(command, 0, "", ""))

    def exec(self, args: Sequence[str]) -> None:
        self.exec_calls.append(tuple(str(arg) for arg in args))
