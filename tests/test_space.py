from __future__ import annotations

from hughub.config import RepoConfig
from hughub.process import Result
from hughub.space import publish_space, render_index

from .conftest import FakeRunner


class FakeSpaceApi:
    def __init__(self):
        self.created = None
        self.uploaded = None

    def create_repo(self, **kwargs):
        self.created = kwargs

    def upload_folder(self, **kwargs):
        folder = kwargs["folder_path"]
        self.uploaded = {
            "kwargs": kwargs,
            "index": (folder / "index.html").read_text(),
            "readme": (folder / "README.md").read_text(),
            "dispatcher": (folder / "dispatcher.py").read_text(),
        }


def test_static_space_snapshot_is_generated_without_touching_source(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (tmp_path / "README.md").write_text("# Widget\n\nA useful project.\n")
    runner = FakeRunner(git_dir)
    revision = "a" * 40
    runner.scripted[("git", "ls-tree", "-r", "--name-only", revision)] = Result(
        ("git",), 0, "README.md\nsrc/widget.py\n", ""
    )
    runner.scripted[
        (
            "git",
            "log",
            "-12",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s%x1e",
        )
    ] = Result(("git",), 0, f"{revision}\x1faaaaaaa\x1fAda\x1f2026-01-01\x1fBuild\x1e", "")
    config = RepoConfig(
        "acme/widget",
        "hf-acme/widget",
        space_repo="hf-acme/widget",
        last_mirrored_sha=revision,
    )
    api = FakeSpaceApi()

    url = publish_space(config, runner, private=False, cwd=tmp_path, api=api)

    assert url == "https://huggingface.co/spaces/hf-acme/widget"
    assert api.created["space_sdk"] == "static"
    assert "src/widget.py" in api.uploaded["index"]
    assert "sdk: static" in api.uploaded["readme"]
    assert "WEBHOOK_PAYLOAD" in api.uploaded["dispatcher"]
    assert (tmp_path / "README.md").read_text() == "# Widget\n\nA useful project.\n"


def test_rendered_ui_escapes_embedded_html():
    rendered = render_index(
        {
            "githubRepo": "acme/widget",
            "hfRepo": "acme/widget",
            "spaceRepo": "acme/widget",
            "defaultBranch": "main",
            "sha": "abc",
            "files": [],
            "commits": [],
            "readme": "</script><script>alert(1)</script>",
            "private": False,
            "mode": "github",
        }
    )

    assert "</script><script>alert(1)</script>" not in rendered
    assert "\\u003c/script>" in rendered
