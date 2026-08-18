"""Credential-free runner for native Hugging Face Job webhooks.

HugHub uploads this file to its public Static Space. Each dormant workflow Job downloads
it when created and whenever its native webhook is triggered. Keep this file stdlib-only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def substitute(value: str, matrix: dict[str, Any], context: dict[str, str]) -> str:
    for key, replacement in matrix.items():
        value = value.replace(f"${{{{ matrix.{key} }}}}", str(replacement))
    for key, replacement in context.items():
        value = value.replace(f"${{{{ github.{key} }}}}", replacement)
    return value


def event_targets(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    event = payload.get("event", {})
    if event.get("scope") != "repo.content":
        return []
    targets: list[tuple[str, str, str]] = []
    for update in payload.get("updatedRefs", []):
        ref, sha = update.get("ref", ""), update.get("newSha")
        if not sha:
            continue
        if ref.startswith("refs/heads/"):
            targets.append(("push", ref, sha))
        elif ref.startswith("refs/pr/"):
            targets.append(("pull_request", ref, sha))
    return targets


def matches(triggers: Any, event_name: str, ref: str) -> bool:
    if isinstance(triggers, str):
        return triggers == event_name
    if isinstance(triggers, list):
        return event_name in triggers
    if not isinstance(triggers, dict) or event_name not in triggers:
        return False
    rule = triggers[event_name]
    if not isinstance(rule, dict) or event_name != "push":
        return True
    branch = ref.removeprefix("refs/heads/")
    included = rule.get("branches", [])
    ignored = rule.get("branches-ignore", [])
    if isinstance(included, str):
        included = [included]
    if isinstance(ignored, str):
        ignored = [ignored]
    if included and not any(fnmatch.fnmatch(branch, pattern) for pattern in included):
        return False
    return not any(fnmatch.fnmatch(branch, pattern) for pattern in ignored)


def _run_target(spec: dict[str, Any], event_name: str, ref: str, sha: str) -> None:
    matrix = spec.get("matrix", {})
    context = {
        "repository": spec["github_repo"],
        "sha": sha,
        "ref": ref,
        "event_name": event_name,
        "workflow": spec["workflow_name"],
    }
    environment = {
        **os.environ,
        "GITHUB_REPOSITORY": context["repository"],
        "GITHUB_SHA": sha,
        "GITHUB_REF": ref,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_WORKFLOW": context["workflow"],
        "HH_REPO_URL": f"https://huggingface.co/{spec['hf_repo']}",
        **{f"MATRIX_{key.upper()}": str(value) for key, value in matrix.items()},
    }
    for key, value in spec.get("job_env", {}).items():
        environment[str(key)] = substitute(str(value), matrix, context)

    temporary = Path(tempfile.mkdtemp(prefix="hughub-job-"))
    checkout = temporary / "repo"
    try:
        subprocess.run(
            ["git", "clone", "--no-checkout", environment["HH_REPO_URL"], str(checkout)],
            check=True,
            env=environment,
        )
        subprocess.run(["git", "checkout", sha], cwd=checkout, check=True, env=environment)
        for step in spec.get("steps", []):
            if "uses" in step:
                # The exact webhook revision has already been checked out above.
                continue
            command = substitute(str(step.get("run", "")), matrix, context)
            step_environment = environment.copy()
            for key, value in (step.get("env", {}) or {}).items():
                step_environment[str(key)] = substitute(str(value), matrix, context)
            print(f"::group::{step.get('name', 'Run')}", flush=True)
            subprocess.run(
                ["bash", "-eu", "-o", "pipefail", "-c", command],
                cwd=checkout,
                check=True,
                env=step_environment,
            )
            print("::endgroup::", flush=True)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def run(spec: dict[str, Any], payload: dict[str, Any]) -> int:
    matched = 0
    for event_name, ref, sha in event_targets(payload):
        if not matches(spec.get("triggers", {}), event_name, ref):
            continue
        print(
            f"HugHub: {spec['workflow_name']} / {spec['job_name']} @ {sha[:12]} ({event_name})",
            flush=True,
        )
        _run_target(spec, event_name, ref, sha)
        matched += 1
    if not matched:
        print("HugHub: webhook did not match this workflow job; nothing to run.")
    return matched


def main() -> None:
    raw_spec = os.environ.get("HH_JOB_SPEC")
    if not raw_spec:
        raise RuntimeError("HH_JOB_SPEC is missing")
    raw_payload = os.environ.get("WEBHOOK_PAYLOAD")
    if not raw_payload:
        print("HugHub workflow Job is ready; waiting for a matching repo webhook.")
        return
    run(json.loads(raw_spec), json.loads(raw_payload))


if __name__ == "__main__" or os.environ.get("HH_JOB_SPEC"):
    main()
