from __future__ import annotations

import os
import json
import shlex
from importlib.resources import files

from huggingface_hub import HfApi, get_token

from .config import RepoConfig
from .errors import HugHubError

DISPATCHER_IMAGE = "python:3.12"


def _dispatcher_command(space_repo: str) -> list[str]:
    loader = (
        "import json,os; from huggingface_hub import hf_hub_download; "
        "raw=os.environ.get('WEBHOOK_SECRET'); "
        "token=(json.loads(raw)['dispatcher_token'] if raw else os.environ.get('HF_TOKEN')); "
        f"p=hf_hub_download(repo_id={space_repo!r},repo_type='space',"
        "filename='dispatcher.py',token=token); "
        "exec(compile(open(p).read(),p,'exec'))"
    )
    shell = (
        "python -m pip install --quiet 'huggingface_hub>=1.26' 'PyYAML>=6' && "
        f"python -c {shlex.quote(loader)}"
    )
    return ["bash", "-lc", shell]


def dispatcher_source() -> str:
    return files("hughub.assets").joinpath("dispatcher.py").read_text()


def _credentials(config: RepoConfig) -> tuple[str, str | None, str]:
    token = get_token()
    if not token:
        raise HugHubError("Hugging Face authentication is required. Run `hf auth login`.")
    job_token = os.environ.get("HH_JOB_TOKEN")
    if config.private and not job_token:
        raise HugHubError(
            "Private mirrors require HH_JOB_TOKEN to contain a fine-grained, read-only HF "
            "token. Workflow code never receives your write-capable dispatcher token."
        )
    encoded = json.dumps({"dispatcher_token": token, "job_token": job_token or ""})
    return token, job_token, encoded


def provision_automation(config: RepoConfig, *, api: HfApi | None = None) -> None:
    if config.webhook_id:
        raise HugHubError(
            "HF automation is already provisioned. Use `hh continuity automation enable|disable`."
        )
    if not config.space_repo:
        raise HugHubError("Create the HugHub Static Space before provisioning automation.")
    token, job_token, webhook_secret = _credentials(config)
    api = api or HfApi(token=token)
    secrets = {"HF_TOKEN": token}
    if job_token:
        secrets["HH_JOB_TOKEN"] = job_token
    try:
        source_job = api.run_job(
            image=DISPATCHER_IMAGE,
            command=_dispatcher_command(config.space_repo),
            flavor="cpu-basic",
            name=f"hh-dispatcher-{config.hf_repo.replace('/', '--')}",
            labels={
                "hughub-role": "dispatcher",
                "hughub-repo": config.hf_repo.replace("/", "--"),
            },
            env={
                "HH_GITHUB_REPO": config.github_repo,
                "HH_HF_REPO": config.hf_repo,
                "HH_PRIVATE": "1" if config.private else "0",
            },
            secrets=secrets,
            timeout="10m",
        )
        webhook = api.create_webhook(
            job_id=source_job.id,
            watched=[{"type": "model", "name": config.hf_repo}],
            # The live webhook API uses the singular form even though some SDK releases
            # advertise `discussions` in their type alias.
            domains=["repo", "discussion"],  # type: ignore[list-item]
            secret=webhook_secret,
        )
        api.disable_webhook(webhook.id)
    except Exception as exc:
        raise HugHubError(f"Could not provision HF Job automation: {exc}") from exc
    config.dispatcher_job_id = source_job.id
    config.webhook_id = webhook.id
    config.automation_enabled = False


def reprovision_automation(config: RepoConfig, *, api: HfApi | None = None) -> None:
    token = get_token()
    api = api or HfApi(token=token)
    if config.webhook_id:
        try:
            api.delete_webhook(config.webhook_id)
        except Exception as exc:
            raise HugHubError(
                f"Could not remove old HF webhook {config.webhook_id}: {exc}"
            ) from exc
    config.webhook_id = None
    config.dispatcher_job_id = None
    config.automation_enabled = False
    provision_automation(config, api=api)


def set_automation(config: RepoConfig, *, enabled: bool, api: HfApi | None = None) -> None:
    if not config.webhook_id:
        return
    api = api or HfApi()
    try:
        if enabled:
            api.enable_webhook(config.webhook_id)
        else:
            api.disable_webhook(config.webhook_id)
    except Exception as exc:
        action = "enable" if enabled else "disable"
        raise HugHubError(f"Could not {action} HugHub webhook {config.webhook_id}: {exc}") from exc
    config.automation_enabled = enabled
