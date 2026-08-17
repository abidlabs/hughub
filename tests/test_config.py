from __future__ import annotations

from hughub.config import RepoConfig, load_config, save_config

from .conftest import FakeRunner


def test_config_round_trip_is_repo_local(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    runner = FakeRunner(git_dir)
    original = RepoConfig(
        github_repo="acme/widget",
        hf_repo="acme/widget",
        mode="overlay",
        recovery_base="abc123",
    )

    path = save_config(original, runner, tmp_path)
    restored = load_config(runner, tmp_path)

    assert path == git_dir / "hughub" / "config.json"
    assert restored == original


def test_optional_config_outside_git_returns_none(tmp_path):
    runner = FakeRunner(tmp_path / "missing")
    # Override the special-case response to look like git's failure.
    original_run = runner.run

    def failing_git(args, **kwargs):
        if tuple(args) == ("git", "rev-parse", "--absolute-git-dir"):
            from hughub.process import Result

            return Result(tuple(args), 128, "", "not a repository")
        return original_run(args, **kwargs)

    runner.run = failing_git  # type: ignore[method-assign]
    assert load_config(runner, tmp_path, required=False) is None
