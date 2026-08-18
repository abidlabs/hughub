"""Ephemeral HugHub webhook dispatcher.

This file is uploaded to each HugHub Static Space. A native HF webhook reruns a source Job
that downloads and executes this file. Keep it self-contained: only huggingface_hub and
PyYAML are installed by the source Job.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from huggingface_hub import HfApi, hf_hub_download


class WorkflowLoader(yaml.SafeLoader):
    pass


for first_character, resolvers in list(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[first_character] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def load_yaml(path: str) -> dict[str, Any]:
    value = yaml.load(Path(path).read_text(), Loader=WorkflowLoader)
    return value if isinstance(value, dict) else {}


def matrixes(job: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = job.get("strategy", {})
    matrix = strategy.get("matrix", {}) if isinstance(strategy, dict) else {}
    if not isinstance(matrix, dict) or not matrix:
        return [{}]
    axes = {key: value for key, value in matrix.items() if key not in {"include", "exclude"}}
    combinations = [
        dict(zip(axes, values, strict=True)) for values in itertools.product(*axes.values())
    ]
    excluded = matrix.get("exclude", [])
    combinations = [
        item
        for item in combinations
        if not any(all(item.get(key) == value for key, value in rule.items()) for rule in excluded)
    ]
    combinations.extend(matrix.get("include", []))
    return combinations or [{}]


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


def matches(workflow: dict[str, Any], event_name: str, ref: str) -> bool:
    triggers = workflow.get("on", {})
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
    if included and not any(fnmatch.fnmatch(branch, pattern) for pattern in included):
        return False
    return not any(fnmatch.fnmatch(branch, pattern) for pattern in ignored)


def runner_specs(
    api: HfApi, repo_id: str, revision: str, files: list[str]
) -> dict[str, dict[str, str]]:
    default = {
        "ubuntu-latest": {
            "flavor": "cpu-upgrade",
            "image": "mcr.microsoft.com/playwright:v1.60.0-jammy",
        }
    }
    if ".hughub.yml" not in files:
        return default
    path = hf_hub_download(repo_id, ".hughub.yml", revision=revision, token=True)
    config = load_yaml(path)
    return config.get("runners", default)


def script_for_job(
    repo_id: str,
    sha: str,
    ref: str,
    event_name: str,
    workflow_name: str,
    job: dict[str, Any],
    matrix: dict[str, Any],
) -> str:
    context = {
        "repository": repo_id,
        "sha": sha,
        "ref": ref,
        "event_name": event_name,
        "workflow": workflow_name,
    }
    lines = [
        "set -eu",
        'if [ -n "${HH_JOB_TOKEN:-}" ]; then '
        'git -c http.extraHeader="Authorization: Bearer ${HH_JOB_TOKEN}" '
        'clone "$HH_REPO_URL" /workspace/repo; '
        'else git clone "$HH_REPO_URL" /workspace/repo; fi',
        "cd /workspace/repo",
        'git checkout "$GITHUB_SHA"',
    ]
    environment = {
        **(job.get("env", {}) or {}),
        **{f"MATRIX_{key.upper()}": value for key, value in matrix.items()},
    }
    for key, value in environment.items():
        rendered = substitute(str(value), matrix, context)
        lines.append(f"export {key}={shlex.quote(rendered)}")
    for step in job.get("steps", []):
        if "uses" in step:
            if not str(step["uses"]).startswith("actions/checkout@"):
                raise ValueError(f"Unsupported action: {step['uses']}")
            continue
        command = substitute(str(step.get("run", "")), matrix, context)
        step_env = step.get("env", {}) or {}
        if step_env:
            assignments = " ".join(
                f"{key}={shlex.quote(substitute(str(value), matrix, context))}"
                for key, value in step_env.items()
            )
            command = f"env {assignments} sh -eu -c {shlex.quote(command)}"
        lines.append(command)
    return "\n".join(lines)


def dispatch(payload: dict[str, Any]) -> int:
    token = os.environ["HF_TOKEN"]
    repo_id = os.environ.get("WEBHOOK_REPO_ID") or payload.get("repo", {}).get("name")
    if not repo_id:
        raise ValueError("Webhook payload has no repository ID")
    github_repo = os.environ.get("HH_GITHUB_REPO", repo_id)
    api = HfApi(token=token)
    private = os.environ.get("HH_PRIVATE") == "1"
    job_token = os.environ.get("HH_JOB_TOKEN")
    if private and not job_token:
        raise ValueError("Private HugHub jobs require a separate read-only HH_JOB_TOKEN")
    launched = 0
    for event_name, ref, sha in event_targets(payload):
        files = api.list_repo_files(repo_id, revision=sha, repo_type="model")
        specs = runner_specs(api, repo_id, sha, files)
        workflow_files = [
            path
            for path in files
            if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
        ]
        for workflow_file in workflow_files:
            path = hf_hub_download(
                repo_id, workflow_file, revision=sha, repo_type="model", token=token
            )
            workflow = load_yaml(path)
            if not matches(workflow, event_name, ref):
                continue
            for job_name, job in workflow.get("jobs", {}).items():
                if "needs" in job or "services" in job or "container" in job:
                    raise ValueError(f"Unsupported orchestration in {workflow_file}:{job_name}")
                label = str(job.get("runs-on", "ubuntu-latest"))
                if label not in specs:
                    raise ValueError(f"No HugHub runner mapping for {label}")
                spec = specs[label]
                for matrix in matrixes(job):
                    script = script_for_job(
                        repo_id, sha, ref, event_name, workflow_file, job, matrix
                    )
                    suffix = "-".join(str(value) for value in matrix.values())
                    job_kwargs = {}
                    if job_token:
                        job_kwargs["secrets"] = {"HH_JOB_TOKEN": job_token}
                    api.run_job(
                        image=spec["image"],
                        command=["bash", "-lc", script],
                        flavor=spec["flavor"],
                        name=f"hh-{job_name}-{suffix}".strip("-")[:63],
                        labels={
                            "hughub.repo": repo_id.replace("/", "--"),
                            "hughub.workflow": Path(workflow_file).name,
                            "hughub.event": event_name,
                            "hughub.sha": sha,
                        },
                        env={
                            "HH_REPO_URL": f"https://huggingface.co/{repo_id}",
                            "GITHUB_REPOSITORY": github_repo,
                            "GITHUB_SHA": sha,
                            "GITHUB_REF": ref,
                            "GITHUB_EVENT_NAME": event_name,
                            "GITHUB_WORKFLOW": workflow.get("name", workflow_file),
                        },
                        **job_kwargs,
                    )
                    launched += 1
    print(json.dumps({"repo": repo_id, "launched": launched, "payload": payload}))
    return launched


if __name__ == "__main__" or os.environ.get("WEBHOOK_PAYLOAD"):
    raw_payload = os.environ.get("WEBHOOK_PAYLOAD")
    if raw_payload:
        dispatch(json.loads(raw_payload))
    else:
        print("HugHub dispatcher source Job is ready; no webhook payload was supplied.")
