# HugHub (`hh`)

**What if GitHub went down and you didn't even notice?**

HugHub is a warm Hugging Face standby for GitHub. You and your agents use the familiar
`gh` command shape through `hh`:

```console
hh pr create --fill
hh issue list
hh run watch 123
```

When GitHub is healthy, `hh` passes those commands straight through to `gh`. When GitHub's
PR, issue, or Actions APIs return an outage error, the same command continues on HugHub.
Commits and pull-request refs live in an already-git-backed Hugging Face repository, and
workflows run directly on Hugging Face Jobs.

There is no standby server fleet to pay for. A public HF repository can sit idle as the
standby, and compute is only used when you choose to run Actions-compatible jobs. Those jobs
are charged according to the selected HF Jobs hardware and runtime. In other words: the
continuity layer is designed to be free while idle and inexpensive when exercised.

> [!IMPORTANT]
> HugHub is currently an early, local-first MVP. Git mirroring, promotion, lightweight HF
> pull requests/issues, direct Jobs execution, and aggregate recovery PRs work. Automated
> server-side mirroring and broad GitHub Actions compatibility are still planned.

## The experience

Set up a repository while GitHub is healthy:

```console
hf auth login
gh auth login
uv tool install .

cd your-repository
hh continuity enable OWNER/REPO --hf-repo HF_OWNER/REPO
```

This creates or reuses the HF repository, adds `github` and `hughub` git remotes, mirrors
branches and tags, and records the last common commit locally under `.git/hughub/config.json`.
HugHub detects GitHub's visibility and defaults to a private HF repository if that check
fails; `--public` and `--private` can override it explicitly.

Then use `hh` anywhere you would normally use `gh`:

```console
hh pr create --fill
hh pr list --json number,title,state
hh issue create --title "A bug" --body "Details"
hh run list
```

In normal mode these are real `gh` commands, with `gh`'s flags, output, authentication, and
interactive behavior. For non-interactive agent calls, a recognized GitHub 5xx or network
failure automatically promotes collaboration to HugHub and retries the operation there.

No prompt rewrite. No new agent instructions. The agent just keeps working.

## Partial and complete outages

HugHub separates GitHub's Git data plane from its collaboration and Actions APIs.

### Overlay mode

Use this when `git push` still works but PRs, issues, or Actions do not:

```console
hh failover --overlay
```

Git commits can continue going to GitHub. `hh pr`, `hh issue`, `hh run`, and
`hh workflow run` use HugHub. Transient failures from supported agent commands enter this
mode automatically.

### Full HugHub mode

Use this when GitHub's Git transport is unavailable too:

```console
hh failover --all
```

HugHub first pushes all locally available branches and tags to HF, records the recovery
base, and changes `origin` to the HF repository. From then on, ordinary `git push` plus the
same `hh` commands are independent of GitHub.

The promotion is deliberately explicit for Git writes. Automatically making two git hosts
writable during a network partition would invite split-brain history.

## Coming back to GitHub

When GitHub is healthy again:

```console
hh recover --to github
```

HugHub pushes the current HEAD to a timestamped `hughub-recovery-*` branch, opens one GitHub
pull request describing the recovery epoch and base commit, restores `origin` to GitHub, and
returns to standby mode.

To push the recovery branch without opening a PR:

```console
hh recover --to github --no-pr
```

An aggregate patch PR is intentionally the default. Reconstructing every emergency issue,
review, and PR on GitHub is possible later, but it is not required to get the code safely
home.

## Keeping the standby fresh

Run this from a developer machine, agent host, cron, or another trusted scheduler:

```console
hh continuity sync
```

It fetches GitHub branches, mirrors them to HF, mirrors tags, and advances the recorded
recovery checkpoint. It does not use `git push --mirror`, because that could delete HF's
`refs/pr/*` pull-request refs.

Before a full promotion, HugHub also pushes every branch and tag present in the local clone.
This means a current developer or agent checkout remains useful even if GitHub can no longer
be fetched.

Server-side webhook plus polling reconciliation is the next production component. Until it
lands, run `hh continuity sync` periodically anywhere that needs a warm standby guarantee.

## Actions on Hugging Face Jobs

HugHub reads existing `.github/workflows/*.yml` files. `runs-on` labels are mapped to HF Jobs
hardware and images in an optional `.hughub.yml`:

```yaml
runners:
  ubuntu-latest:
    flavor: cpu-upgrade
    image: mcr.microsoft.com/playwright:v1.60.0-jammy

  gpu:
    flavor: a10g-small
    image: nvidia/cuda:12.4.0-runtime-ubuntu22.04
```

Check whether workflows fit the current runner before an outage:

```console
hh workflow doctor
```

In HugHub mode, launch the same workflow directly on HF Jobs:

```console
hh workflow run tests.yml
hh run list
hh run watch JOB_ID
```

The current manually launched runner supports:

- Parsing `push`, `pull_request`, and `workflow_dispatch` declarations
- Shell `run` steps
- `actions/checkout` (performed natively before the steps)
- Job and step environment variables
- Matrix expansion
- Mapping runner labels to an HF Jobs flavor and Docker image

It currently rejects job dependencies, services, job containers, and actions other than
`actions/checkout`. `hh workflow doctor` reports these before anything is launched. This
deliberately small subset makes emergency behavior predictable instead of silently claiming
compatibility that is not there.

## HugHub collaboration commands

The HF backend currently implements the agent-critical subset:

```text
hh pr list
hh pr create --fill
hh pr view NUMBER
hh pr checkout NUMBER
hh pr comment NUMBER --body TEXT
hh pr merge NUMBER
hh pr close NUMBER

hh issue list
hh issue create --title TEXT --body TEXT
hh issue view NUMBER
hh issue comment NUMBER --body TEXT
hh issue close NUMBER
hh issue reopen NUMBER

hh run list
hh run view JOB_ID
hh run watch JOB_ID
hh run cancel JOB_ID
```

These use Hugging Face discussions and the Hub's native `refs/pr/NUMBER` refs. Unsupported
commands fail explicitly rather than pretending to succeed.

## Status and configuration

```console
hh continuity status
hh continuity status --json
```

HugHub stores no repository state in a hosted HH control plane. The local file contains the
GitHub/HF pairing, mode, last mirrored commit, promotion time, and recovery base. Tokens stay
with the existing `gh` and `hf` authentication systems.

## Development

```console
uv sync
uv run ruff check .
uv run pytest
uv run hh --help
```

The test suite uses fake `gh`, `hf`, and git process boundaries; it does not create remote
repositories or Jobs.

## Design principles

1. **Invisible during healthy operation.** `hh` is `gh` until GitHub cannot do the work.
2. **Local failover authority.** Promotion must not require contacting a failed control plane.
3. **Git first.** Recovery is always possible from commits, even if collaboration metadata is
   incomplete.
4. **One writer after promotion.** Preserve divergent work; never force an automatic merge.
5. **No idle infrastructure bill.** Store the standby in HF Repos and allocate Jobs only for
   actual workflow runs.
6. **Honest compatibility.** Diagnose unsupported workflows before an outage.
