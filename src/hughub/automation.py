from __future__ import annotations

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi

from .config import RepoConfig
from .errors import HugHubError
from .workflows import _load_yaml, _matrixes, inspect_workflow, runner_specs


def runner_source() -> str:
    return files("hughub.assets").joinpath("runner.py").read_text()


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_=-]", "-", value)[:256]


def _runner_command(space_repo: str) -> list[str]:
    url = f"https://huggingface.co/spaces/{space_repo}/resolve/main/runner.py"
    loader = (
        "import urllib.request; "
        f"source=urllib.request.urlopen({url!r}).read(); "
        "exec(compile(source,'runner.py','exec'))"
    )
    return ["python", "-c", loader]


def _workflow_specs(config: RepoConfig, cwd: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    runners = runner_specs(cwd)
    paths = sorted((cwd / ".github" / "workflows").glob("*.y*ml"))
    for path in paths:
        errors = [item for item in inspect_workflow(path, cwd) if item.level == "error"]
        if errors:
            # Keep GitHub-only workflows in the mirror without breaking continuity setup.
            continue
        workflow = _load_yaml(path)
        workflow_env = workflow.get("env", {}) or {}
        for job_name, job in workflow["jobs"].items():
            runner = runners[str(job.get("runs-on", "ubuntu-latest"))]
            for matrix in _matrixes(job):
                specs.append(
                    {
                        "github_repo": config.github_repo,
                        "hf_repo": config.hf_repo,
                        "workflow_file": str(path.relative_to(cwd)),
                        "workflow_name": str(workflow.get("name", path.name)),
                        "triggers": workflow.get("on", {}),
                        "job_name": str(job_name),
                        "job_env": {**workflow_env, **(job.get("env", {}) or {})},
                        "steps": job.get("steps", []),
                        "matrix": matrix,
                        "image": runner.image,
                        "flavor": runner.flavor,
                        "timeout": f"{int(job.get('timeout-minutes', 60))}m",
                    }
                )
    return specs


def _revision(specs: list[dict[str, Any]]) -> str:
    encoded = json.dumps(specs, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _delete_webhooks(config: RepoConfig, api: HfApi) -> None:
    ids = [*config.automation_webhooks]
    if config.webhook_id and config.webhook_id not in ids:
        ids.append(config.webhook_id)
    for webhook_id in ids:
        try:
            api.delete_webhook(webhook_id)
        except Exception as exc:
            # A stale local ID is expected after manual cleanup in the HF settings/API.
            if "404" not in str(exc) and "not found" not in str(exc).lower():
                raise HugHubError(
                    f"Could not remove old HF webhook {webhook_id}: {exc}"
                ) from exc
    config.automation_jobs = []
    config.automation_webhooks = []
    config.dispatcher_job_id = None
    config.webhook_id = None
    config.automation_enabled = False


def provision_automation(
    config: RepoConfig, *, cwd: Path | None = None, api: HfApi | None = None
) -> None:
    if config.private:
        raise HugHubError(
            "Native push automation is only safe for public mirrors. Private mirrors can use "
            "`hh workflow run` with a separate read-only HH_JOB_TOKEN."
        )
    if config.automation_webhooks or config.webhook_id:
        raise HugHubError(
            "HF automation is already provisioned. Use `hh continuity automation setup` to "
            "replace it."
        )
    if not config.space_repo:
        raise HugHubError("Create the HugHub Static Space before provisioning automation.")
    cwd = cwd or Path.cwd()
    workflow_specs = _workflow_specs(config, cwd)
    api = api or HfApi()
    jobs: list[str] = []
    webhooks: list[str] = []
    try:
        for index, spec in enumerate(workflow_specs, start=1):
            matrix_suffix = "-".join(str(value) for value in spec["matrix"].values())
            display = f"{spec['job_name']}-{matrix_suffix}".strip("-")
            source_job = api.run_job(
                image=spec["image"],
                command=_runner_command(config.space_repo),
                flavor=spec["flavor"],
                name=f"hh-{display}"[:63],
                labels={
                    "hughub-role": "workflow",
                    "hughub-repo": _safe_label(config.hf_repo.replace("/", "--")),
                    "hughub-workflow": _safe_label(spec["workflow_file"]),
                    "hughub-slot": str(index),
                },
                env={"HH_JOB_SPEC": json.dumps(spec, separators=(",", ":"))},
                timeout=spec["timeout"],
            )
            jobs.append(source_job.id)
            webhook = api.create_webhook(
                job_id=source_job.id,
                watched=[{"type": "model", "name": config.hf_repo}],
                domains=["repo"],
            )
            webhooks.append(webhook.id)
            api.disable_webhook(webhook.id)
    except Exception as exc:
        for webhook_id in webhooks:
            try:
                api.delete_webhook(webhook_id)
            except Exception:
                pass
        raise HugHubError(f"Could not provision HF Job automation: {exc}") from exc
    config.automation_jobs = jobs
    config.automation_webhooks = webhooks
    config.automation_revision = _revision(workflow_specs)
    config.automation_enabled = False
    config.version = 3


def reprovision_automation(
    config: RepoConfig, *, cwd: Path | None = None, api: HfApi | None = None
) -> None:
    api = api or HfApi()
    _delete_webhooks(config, api)
    provision_automation(config, cwd=cwd, api=api)


def refresh_automation(
    config: RepoConfig, *, cwd: Path | None = None, api: HfApi | None = None
) -> bool:
    """Replace standby jobs when committed workflow configuration changed."""
    if config.automation_revision is None:
        return False
    cwd = cwd or Path.cwd()
    specs = _workflow_specs(config, cwd)
    if _revision(specs) == config.automation_revision:
        return False
    was_enabled = config.automation_enabled
    reprovision_automation(config, cwd=cwd, api=api)
    if was_enabled:
        set_automation(config, enabled=True, api=api)
    return True


def set_automation(config: RepoConfig, *, enabled: bool, api: HfApi | None = None) -> None:
    webhook_ids = list(config.automation_webhooks)
    if config.webhook_id and config.webhook_id not in webhook_ids:
        webhook_ids.append(config.webhook_id)
    if not webhook_ids:
        return
    api = api or HfApi()
    try:
        for webhook_id in webhook_ids:
            if enabled:
                api.enable_webhook(webhook_id)
            else:
                api.disable_webhook(webhook_id)
    except Exception as exc:
        action = "enable" if enabled else "disable"
        raise HugHubError(f"Could not {action} HugHub webhooks: {exc}") from exc
    config.automation_enabled = enabled
