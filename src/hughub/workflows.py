from __future__ import annotations

import itertools
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import RepoConfig
from .errors import HugHubError
from .git import current_branch
from .process import Runner


class WorkflowLoader(yaml.SafeLoader):
    """YAML 1.2-like booleans so the workflow key `on` stays a string."""


for first_character, resolvers in list(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[first_character] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


@dataclass(frozen=True)
class RunnerSpec:
    flavor: str = "cpu-upgrade"
    image: str = "mcr.microsoft.com/playwright:v1.60.0-jammy"


@dataclass(frozen=True)
class Finding:
    level: str
    location: str
    message: str


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(), Loader=WorkflowLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise HugHubError(f"Could not read workflow {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HugHubError(f"Workflow {path} must contain a YAML object.")
    return value


def workflow_path(name: str, cwd: Path) -> Path:
    supplied = Path(name)
    candidates = [supplied] if supplied.is_absolute() else [cwd / supplied]
    candidates.extend([cwd / ".github" / "workflows" / name])
    if not name.endswith((".yml", ".yaml")):
        candidates.extend(
            [
                cwd / ".github" / "workflows" / f"{name}.yml",
                cwd / ".github" / "workflows" / f"{name}.yaml",
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HugHubError(f"Could not find workflow `{name}`.")


def runner_specs(cwd: Path) -> dict[str, RunnerSpec]:
    path = cwd / ".hughub.yml"
    if not path.exists():
        return {"ubuntu-latest": RunnerSpec()}
    data = _load_yaml(path)
    raw = data.get("runners", {})
    if not isinstance(raw, dict):
        raise HugHubError("`.hughub.yml` runners must be an object.")
    specs: dict[str, RunnerSpec] = {}
    for label, value in raw.items():
        if not isinstance(value, dict):
            raise HugHubError(f"Runner `{label}` must be an object.")
        specs[str(label)] = RunnerSpec(
            flavor=str(value.get("flavor", "cpu-upgrade")),
            image=str(value.get("image", RunnerSpec.image)),
        )
    return specs


def inspect_workflow(path: Path, cwd: Path) -> list[Finding]:
    workflow = _load_yaml(path)
    findings: list[Finding] = []
    supported_events = {"push", "pull_request", "workflow_dispatch"}
    events = workflow.get("on", {})
    event_names = {events} if isinstance(events, str) else set(events or {})
    for event in sorted(event_names - supported_events):
        findings.append(Finding("warning", "on", f"event `{event}` is not triggered by HugHub yet"))

    specs = runner_specs(cwd)
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict) or not jobs:
        findings.append(Finding("error", "jobs", "workflow has no jobs"))
        return findings
    for job_name, job in jobs.items():
        location = f"jobs.{job_name}"
        if not isinstance(job, dict):
            findings.append(Finding("error", location, "job must be an object"))
            continue
        label = str(job.get("runs-on", "ubuntu-latest"))
        if label not in specs:
            findings.append(
                Finding(
                    "error", f"{location}.runs-on", f"runner `{label}` is not mapped in .hughub.yml"
                )
            )
        if "needs" in job:
            findings.append(
                Finding("error", f"{location}.needs", "job dependencies are not supported yet")
            )
        if "services" in job or "container" in job:
            findings.append(
                Finding("error", location, "service and job containers are not supported yet")
            )
        strategy = job.get("strategy", {})
        if isinstance(strategy, dict) and strategy.get("matrix"):
            findings.append(
                Finding("info", f"{location}.strategy.matrix", "matrix will be expanded")
            )
        for index, step in enumerate(job.get("steps", [])):
            step_location = f"{location}.steps[{index}]"
            if not isinstance(step, dict):
                findings.append(Finding("error", step_location, "step must be an object"))
            elif "uses" in step and not str(step["uses"]).startswith("actions/checkout@"):
                findings.append(
                    Finding("error", step_location, f"action `{step['uses']}` is not supported yet")
                )
            elif "run" not in step and "uses" not in step:
                findings.append(
                    Finding("error", step_location, "step has neither `run` nor `uses`")
                )
    return findings


def doctor(name: str | None, cwd: Path) -> int:
    paths = (
        [workflow_path(name, cwd)]
        if name
        else sorted((cwd / ".github" / "workflows").glob("*.y*ml"))
    )
    if not paths:
        raise HugHubError("No workflows found under .github/workflows.")
    errors = 0
    for path in paths:
        findings = inspect_workflow(path, cwd)
        print(f"{path.relative_to(cwd)}")
        if not findings:
            print("  ok: compatible with the current HugHub runner")
        for finding in findings:
            print(f"  {finding.level}: {finding.location}: {finding.message}")
            errors += finding.level == "error"
    return 1 if errors else 0


def _matrixes(job: dict[str, Any]) -> list[dict[str, Any]]:
    strategy = job.get("strategy", {})
    matrix = strategy.get("matrix", {}) if isinstance(strategy, dict) else {}
    if not isinstance(matrix, dict) or not matrix:
        return [{}]
    axes = {key: value for key, value in matrix.items() if key not in {"include", "exclude"}}
    if any(not isinstance(value, list) for value in axes.values()):
        raise HugHubError("Every matrix axis must be a list.")
    combinations = [
        dict(zip(axes, values, strict=True)) for values in itertools.product(*axes.values())
    ]
    excluded = matrix.get("exclude", [])
    combinations = [
        item
        for item in combinations
        if not any(all(item.get(k) == v for k, v in rule.items()) for rule in excluded)
    ]
    combinations.extend(matrix.get("include", []))
    return combinations or [{}]


def _substitute(value: str, matrix: dict[str, Any]) -> str:
    for key, replacement in matrix.items():
        value = value.replace(f"${{{{ matrix.{key} }}}}", str(replacement))
    return value


def _job_script(
    config: RepoConfig, branch: str, job: dict[str, Any], matrix: dict[str, Any]
) -> str:
    lines = [
        "set -eu",
        'git -c http.extraHeader="Authorization: Bearer ${HF_TOKEN:-}" '
        'clone "$HH_REPO_URL" /workspace/repo',
        "cd /workspace/repo",
        'git checkout "$HH_REF"',
    ]
    environment = {
        **(job.get("env", {}) or {}),
        **{f"MATRIX_{k.upper()}": v for k, v in matrix.items()},
    }
    for key, value in environment.items():
        lines.append(f"export {key}={shlex.quote(_substitute(str(value), matrix))}")
    for step in job.get("steps", []):
        if "uses" in step:
            # checkout is performed once above, before any workflow steps.
            continue
        command = _substitute(str(step.get("run", "")), matrix)
        step_env = step.get("env", {}) or {}
        if step_env:
            assignments = " ".join(
                f"{key}={shlex.quote(_substitute(str(value), matrix))}"
                for key, value in step_env.items()
            )
            command = f"env {assignments} sh -eu -c {shlex.quote(command)}"
        lines.append(command)
    return "\n".join(lines)


def run_workflow(
    config: RepoConfig,
    runner: Runner,
    *,
    name: str,
    ref: str | None,
    cwd: Path,
) -> int:
    path = workflow_path(name, cwd)
    findings = inspect_workflow(path, cwd)
    errors = [finding for finding in findings if finding.level == "error"]
    if errors:
        joined = "; ".join(f"{item.location}: {item.message}" for item in errors)
        raise HugHubError(f"Workflow is not HugHub-compatible: {joined}. Run `hh workflow doctor`.")

    workflow = _load_yaml(path)
    specs = runner_specs(cwd)
    branch = ref or current_branch(runner, cwd)
    runner.run(
        ["git", "push", config.hughub_remote, f"HEAD:refs/heads/{branch}"], cwd=cwd, check=True
    )
    repo_url = f"https://huggingface.co/{config.hf_repo}"
    launched = 0
    for job_name, job in workflow["jobs"].items():
        spec = specs[str(job.get("runs-on", "ubuntu-latest"))]
        for matrix in _matrixes(job):
            suffix = "-" + "-".join(str(value) for value in matrix.values()) if matrix else ""
            script = _job_script(config, branch, job, matrix)
            command = [
                "hf",
                "jobs",
                "run",
                "--detach",
                "--flavor",
                spec.flavor,
                "--name",
                f"hh-{job_name}{suffix}"[:63],
                "--label",
                f"hughub.repo={config.hf_repo.replace('/', '--')}",
                "--label",
                f"hughub.workflow={path.name}",
                "--env",
                f"HH_REPO_URL={repo_url}",
                "--env",
                f"HH_REF={branch}",
                "--secrets",
                "HF_TOKEN",
                spec.image,
                "bash",
                "-lc",
                script,
            ]
            result = runner.run(command, cwd=cwd)
            print(result.stdout, end="")
            if result.returncode:
                raise HugHubError(result.stderr.strip() or f"Could not launch job {job_name}")
            launched += 1
    print(f"Launched {launched} HugHub job(s) on Hugging Face Jobs.")
    return 0


def explain_support() -> str:
    return json.dumps(
        {
            "events": ["push", "pull_request", "workflow_dispatch"],
            "steps": ["run", "actions/checkout"],
            "features": ["env", "matrix", "timeouts delegated to HF Jobs"],
        },
        indent=2,
    )
