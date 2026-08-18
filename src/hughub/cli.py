from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import __version__
from .automation import provision_automation, set_automation
from .config import load_config, save_config
from .continuity import enable, promote, recover, render_status, sync, warn_auto_failover
from .errors import HugHubError
from .hub import HubBackend
from .process import Result, Runner
from .workflows import doctor, run_workflow

OUTAGE_PATTERN = re.compile(
    r"(?:HTTP\s+5\d\d|timed?\s*out|timeout|service unavailable|internal server error|"
    r"could not resolve host|failed to connect|connection refused|connection reset|"
    r"network is unreachable|TLS handshake|bad gateway)",
    re.IGNORECASE,
)
HUB_COMMANDS = {"pr", "issue", "run", "workflow"}


def _emit(result: Result) -> None:
    if result.stdout:
        print(result.stdout, end="", file=sys.stdout)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def _help() -> str:
    return f"""hh {__version__} — keep using GitHub, even when GitHub is down

Normally, every command is passed through to gh unchanged:
  hh pr create --fill
  hh issue list
  hh run watch 123

Continuity commands:
  hh continuity enable [OWNER/REPO] [--hf-repo OWNER/REPO] [--space-repo OWNER/REPO]
  hh continuity sync
  hh continuity status [--json]
  hh continuity automation setup|enable|disable
  hh failover [--overlay | --all]
  hh recover [--no-pr] [--branch NAME]
  hh workflow doctor [WORKFLOW]

When GitHub returns a transient outage error, supported agent commands automatically
continue on HugHub. Use `hh failover --all` when GitHub's Git transport is also down.
"""


def _continuity(args: list[str], runner: Runner, cwd: Path) -> int:
    parser = argparse.ArgumentParser(prog="hh continuity")
    subparsers = parser.add_subparsers(dest="action", required=True)
    enable_parser = subparsers.add_parser("enable")
    enable_parser.add_argument("github_repo", nargs="?")
    enable_parser.add_argument("--hf-repo")
    enable_parser.add_argument("--space-repo")
    visibility = enable_parser.add_mutually_exclusive_group()
    visibility.add_argument("--private", action="store_true", default=None)
    visibility.add_argument("--public", action="store_false", dest="private")
    enable_parser.add_argument("--no-push", action="store_true")
    enable_parser.add_argument("--no-automation", action="store_true")
    subparsers.add_parser("sync")
    automation_parser = subparsers.add_parser("automation")
    automation_parser.add_argument("automation_action", choices=["setup", "enable", "disable"])
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    options = parser.parse_args(args)
    if options.action == "enable":
        config = enable(
            runner,
            github_repo=options.github_repo,
            hf_repo=options.hf_repo,
            space_repo=options.space_repo,
            private=options.private,
            no_push=options.no_push,
            no_automation=options.no_automation,
            cwd=cwd,
        )
        print(f"Continuity enabled: GitHub {config.github_repo} → HugHub {config.hf_repo}")
        print(f"Static UI: https://huggingface.co/spaces/{config.space_repo}")
        if config.webhook_id:
            print(f"Native Job webhook: {config.webhook_id} (standby)")
        return 0
    if options.action == "sync":
        config = sync(runner, cwd)
        print(f"Mirror updated at {config.last_mirrored_sha}")
        return 0
    if options.action == "automation":
        config = load_config(runner, cwd)
        assert config is not None
        if options.automation_action == "setup":
            provision_automation(config)
        else:
            set_automation(config, enabled=options.automation_action == "enable")
        save_config(config, runner, cwd)
        print(f"HF automation is {'enabled' if config.automation_enabled else 'in standby'}.")
        return 0
    config = load_config(runner, cwd)
    assert config is not None
    print(render_status(config, as_json=options.json))
    return 0


def _failover(args: list[str], runner: Runner, cwd: Path) -> int:
    parser = argparse.ArgumentParser(prog="hh failover")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--overlay", action="store_true", help="Keep GitHub Git; move PRs and jobs")
    group.add_argument("--all", action="store_true", help="Move Git, PRs, issues, and jobs")
    options = parser.parse_args(args)
    mode = "hughub" if options.all else "overlay"
    config = promote(runner, mode, cwd)
    print(f"HugHub is active in {config.mode} mode. Keep working with the same `hh` commands.")
    return 0


def _recover(args: list[str], runner: Runner, cwd: Path) -> int:
    parser = argparse.ArgumentParser(prog="hh recover")
    parser.add_argument("--to", choices=["github"], default="github")
    parser.add_argument("--no-pr", action="store_true")
    parser.add_argument("--branch")
    options = parser.parse_args(args)
    branch = recover(runner, create_pr=not options.no_pr, branch_name=options.branch, cwd=cwd)
    print(f"Recovery branch `{branch}` pushed to GitHub; HugHub is back in standby mode.")
    return 0


def _workflow_custom(args: list[str], runner: Runner, cwd: Path) -> int | None:
    if not args or args[0] != "doctor":
        return None
    name = args[1] if len(args) > 1 else None
    return doctor(name, cwd)


def _hub_dispatch(args: list[str], runner: Runner, cwd: Path) -> int:
    config = load_config(runner, cwd)
    assert config is not None
    if args[0] == "workflow" and len(args) > 1 and args[1] == "run":
        parser = argparse.ArgumentParser(prog="hh workflow run")
        parser.add_argument("workflow")
        parser.add_argument("--ref")
        options, _unknown = parser.parse_known_args(args[2:])
        return run_workflow(config, runner, name=options.workflow, ref=options.ref, cwd=cwd)
    return HubBackend(config, runner, cwd=cwd).dispatch(args)


def run(
    argv: list[str] | None = None, *, runner: Runner | None = None, cwd: Path | None = None
) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    runner = runner or Runner()
    cwd = cwd or Path.cwd()
    if not args or args[0] in {"help", "--help", "-h"}:
        print(_help())
        return 0
    if args[0] in {"--version", "version"}:
        print(f"hh version {__version__}")
        return 0
    if args[0] == "continuity":
        return _continuity(args[1:], runner, cwd)
    if args[0] == "failover":
        return _failover(args[1:], runner, cwd)
    if args[0] == "recover":
        return _recover(args[1:], runner, cwd)
    if args[0] == "workflow":
        custom = _workflow_custom(args[1:], runner, cwd)
        if custom is not None:
            return custom

    config = load_config(runner, cwd, required=False)
    if config and config.mode != "github" and args[0] in HUB_COMMANDS:
        return _hub_dispatch(args, runner, cwd)

    # Interactive use stays byte-for-byte gh-compatible. Agent/non-interactive use captures
    # transient errors so it can continue on HugHub without a second instruction.
    if sys.stdin.isatty() and not os.environ.get("HH_AUTO_FAILOVER"):
        runner.exec(["gh", *args])
        return 0
    result = runner.run(["gh", *args], cwd=cwd)
    if result.returncode == 0:
        _emit(result)
        return 0
    if config and args[0] in HUB_COMMANDS and OUTAGE_PATTERN.search(result.stderr + result.stdout):
        warn_auto_failover(args)
        config.mode = "overlay"
        try:
            set_automation(config, enabled=True)
        except HugHubError as exc:
            print(
                f"hh: warning: {exc}; automatic push-triggered Jobs remain disabled.",
                file=sys.stderr,
            )
        save_config(config, runner, cwd)
        return _hub_dispatch(args, runner, cwd)
    _emit(result)
    return result.returncode


def main() -> None:
    try:
        raise SystemExit(run())
    except HugHubError as exc:
        print(f"hh: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
