from __future__ import annotations

import json
from types import SimpleNamespace

from hughub import automation
from hughub.config import RepoConfig


class FakeApi:
    def __init__(self):
        self.run_calls = []
        self.webhook_calls = []
        self.disabled = []
        self.enabled = []
        self.deleted = []

    def run_job(self, **kwargs):
        self.run_calls.append(kwargs)
        return SimpleNamespace(id=f"job-{len(self.run_calls)}")

    def create_webhook(self, **kwargs):
        self.webhook_calls.append(kwargs)
        return SimpleNamespace(id=f"webhook-{len(self.webhook_calls)}")

    def disable_webhook(self, webhook_id):
        self.disabled.append(webhook_id)

    def enable_webhook(self, webhook_id):
        self.enabled.append(webhook_id)

    def delete_webhook(self, webhook_id):
        self.deleted.append(webhook_id)


def test_provision_creates_credential_free_webhook_per_workflow_job(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text(
        """name: Tests
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ['3.11', '3.12']
    steps:
      - uses: actions/checkout@v4
      - run: python --version
"""
    )
    config = RepoConfig(
        github_repo="acme/widget", hf_repo="hf-acme/widget", space_repo="hf-acme/widget"
    )
    api = FakeApi()

    automation.provision_automation(config, cwd=tmp_path, api=api)

    assert config.automation_jobs == ["job-1", "job-2"]
    assert config.automation_webhooks == ["webhook-1", "webhook-2"]
    assert config.automation_revision
    assert config.automation_enabled is False
    assert len(api.run_calls) == 2
    assert all("secrets" not in call for call in api.run_calls)
    assert json.loads(api.run_calls[0]["env"]["HH_JOB_SPEC"])["matrix"] == {
        "python": "3.11"
    }
    assert api.webhook_calls[0] == {
        "job_id": "job-1",
        "watched": [{"type": "model", "name": "hf-acme/widget"}],
        "domains": ["repo"],
    }
    assert api.disabled == ["webhook-1", "webhook-2"]


def test_webhook_lifecycle_updates_all_hooks():
    config = RepoConfig(
        "acme/widget", "acme/widget", automation_webhooks=["hook-1", "hook-2"]
    )
    api = FakeApi()

    automation.set_automation(config, enabled=True, api=api)
    assert config.automation_enabled is True
    assert api.enabled == ["hook-1", "hook-2"]

    automation.set_automation(config, enabled=False, api=api)
    assert config.automation_enabled is False
    assert api.disabled == ["hook-1", "hook-2"]


def test_private_repo_refuses_automatic_webhooks(tmp_path):
    config = RepoConfig("acme/widget", "acme/widget", space_repo="acme/widget", private=True)

    try:
        automation.provision_automation(config, cwd=tmp_path, api=FakeApi())
    except Exception as exc:
        assert "only safe for public mirrors" in str(exc)
    else:
        raise AssertionError("private automatic webhook should have been rejected")
