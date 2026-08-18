from __future__ import annotations

from types import SimpleNamespace

from hughub import automation
from hughub.config import RepoConfig


class FakeApi:
    def __init__(self):
        self.run_kwargs = None
        self.webhook_kwargs = None
        self.disabled = []
        self.enabled = []

    def run_job(self, **kwargs):
        self.run_kwargs = kwargs
        return SimpleNamespace(id="job-123")

    def create_webhook(self, **kwargs):
        self.webhook_kwargs = kwargs
        return SimpleNamespace(id="webhook-456")

    def disable_webhook(self, webhook_id):
        self.disabled.append(webhook_id)

    def enable_webhook(self, webhook_id):
        self.enabled.append(webhook_id)


def test_provision_creates_disabled_native_job_webhook(monkeypatch):
    monkeypatch.setattr(automation, "get_token", lambda: "hf_secret")
    config = RepoConfig(
        github_repo="acme/widget", hf_repo="hf-acme/widget", space_repo="hf-acme/widget"
    )
    api = FakeApi()

    automation.provision_automation(config, api=api)

    assert config.dispatcher_job_id == "job-123"
    assert config.webhook_id == "webhook-456"
    assert config.automation_enabled is False
    assert api.webhook_kwargs["job_id"] == "job-123"
    assert api.webhook_kwargs["watched"] == [{"type": "model", "name": "hf-acme/widget"}]
    assert api.webhook_kwargs["domains"] == ["repo", "discussions"]
    assert api.run_kwargs["secrets"] == {"HF_TOKEN": "hf_secret"}
    assert api.disabled == ["webhook-456"]


def test_webhook_lifecycle_updates_config():
    config = RepoConfig("acme/widget", "acme/widget", webhook_id="hook")
    api = FakeApi()

    automation.set_automation(config, enabled=True, api=api)
    assert config.automation_enabled is True
    assert api.enabled == ["hook"]

    automation.set_automation(config, enabled=False, api=api)
    assert config.automation_enabled is False
    assert api.disabled == ["hook"]
