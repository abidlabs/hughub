from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .errors import HugHubError


@dataclass(frozen=True)
class Result:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner:
    """Small process boundary that keeps command construction testable."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
        capture: bool = True,
        stdin: IO[str] | None = None,
    ) -> Result:
        command = [str(arg) for arg in args]
        completed = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=stdin,
            text=True,
            capture_output=capture,
            check=False,
        )
        result = Result(
            args=tuple(command),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HugHubError(f"Command failed ({' '.join(command)}): {detail}")
        return result

    def exec(self, args: Sequence[str]) -> None:
        """Replace this process, preserving gh's exact interactive behavior."""

        os.execvp(str(args[0]), [str(arg) for arg in args])
