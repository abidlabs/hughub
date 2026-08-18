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
standby, its paired Static Space is free, and compute starts only for brief workflow-Job setup
or Actions-compatible runs. Jobs are charged according to their selected hardware and
runtime. In other words: you and your agents do not even need to know GitHub is down—you keep
pushing to HugHub, the continuity layer is free while idle, and it costs little unless you
choose substantial Job compute.

> [!IMPORTANT]
> HugHub is currently an early MVP. Git mirroring, a free read-only Static Space, native
> webhook-triggered Jobs, promotion, lightweight HF pull requests/issues, and aggregate
> recovery PRs work. Periodic GitHub-to-HF synchronization still runs through `hh continuity
> sync`, and broad GitHub Actions compatibility is still planned.

## The experience

Set up a repository while GitHub is healthy:

```console
hf auth login
gh auth login
uv tool install .

cd your-repository
hh continuity enable OWNER/REPO --hf-repo HF_OWNER/REPO
```

This creates or reuses two repositories with the same HF identifier:

- A normal HF repo containing the SHA-identical Git mirror and native HF PR refs.
- A free Static Space containing a read-only GitHub-like UI and credential-free runner.

For every compatible workflow job and matrix entry, it creates a dormant HF Job and attaches
a native repo webhook directly to that Job. The webhooks start disabled, so GitHub Actions
remain the only CI system during normal operation. No dispatcher service or control plane is
involved.
The command adds `github` and `hughub` git remotes, mirrors branches and tags, and records the
last common commit locally under `.git/hughub/config.json`.
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
mode automatically. Promotion enables the native HF Job webhook.

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

Once promoted, pushes to the HF repo and updates to `refs/pr/*` rerun the corresponding
workflow Jobs directly. Each Job checks the webhook event and branch filters, checks out the
exact pushed commit, and executes its supported steps. No credential-bearing dispatcher or
second child Job is involved.

## Coming back to GitHub

When GitHub is healthy again:

```console
hh recover --to github
```

HugHub pushes the current HEAD to a timestamped `hughub-recovery-*` branch, opens one GitHub
pull request describing the recovery epoch and base commit, restores `origin` to GitHub, and
returns to standby mode.

Recovery disables the native HF webhook before moving work back, preventing duplicate runs.

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
recovery checkpoint. It also regenerates the Static Space with the latest file tree, README,
and commit history. It does not use `git push --mirror`, because that could delete HF's
`refs/pr/*` pull-request refs.

Before a full promotion, HugHub also pushes every branch and tag present in the local clone.
This means a current developer or agent checkout remains useful even if GitHub can no longer
be fetched.

The native webhook covers HF-side events after promotion. It deliberately does not mirror
GitHub while GitHub is primary, so run `hh continuity sync` periodically from a developer
machine, agent host, cron, or another trusted scheduler.

## Read-only Static Space

The Space is a zero-compute snapshot rather than the Git backend itself. Keeping these
separate preserves original Git commit SHAs and avoids adding Space metadata to the project.
The generated UI includes:

- Repository file tree with links to HF's file browser
- README snapshot
- Recent commits and mirrored SHA
- Git remote and standby status
- A prominent arrow to HF's Community menu for issues and pull requests

The Space holds no credentials. Public automatic workflow Jobs also receive no HF token: they
clone the public mirror after the webhook has selected an exact SHA.

Native webhook automation is intentionally disabled for private mirrors. HF webhook-triggered
Jobs currently do not inherit source-Job secrets, and webhook secrets can appear as ordinary
Job environment metadata, so passing a repository token through either path would be unsafe.
Private mirrors can still run workflows manually with `hh workflow run` and a separate,
fine-grained read-only token in `HH_JOB_TOKEN`.

## Actions on Hugging Face Jobs

HugHub reads existing `.github/workflows/*.yml` files. `runs-on` labels are mapped to HF Jobs
hardware and images in an optional `.hughub.yml`:

```yaml
runners:
  ubuntu-latest:
    flavor: cpu-upgrade
    image: python:3.12

  gpu:
    flavor: a10g-small
    image: nvidia/cuda:12.4.0-runtime-ubuntu22.04
```

Check whether workflows fit the current runner before an outage:

```console
hh workflow doctor
```

After promotion, ordinary pushes launch matching workflows automatically:

```console
git push
hh run list
hh run watch JOB_ID
```

You can also launch a workflow manually:

```console
hh workflow run tests.yml
hh run list
hh run watch JOB_ID
```

The current native-webhook and manually launched runners support:

- Parsing `push`, `pull_request`, and `workflow_dispatch` declarations
- Automatic `push` branch filters and HF pull-request ref events
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
GitHub/HF pairing, Static Space, native webhook and source Job IDs, mode, last mirrored
commit, promotion time, and recovery base. Tokens stay with the existing `gh` and `hf`
authentication systems. Neither the public workflow Jobs nor the Static Space receive an HF
token.

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
5. **No idle infrastructure bill.** Use a Static Space plus native Job webhooks; allocate
   compute only for brief Job setup and workflow runs.
6. **Honest compatibility.** Diagnose unsupported workflows before an outage.
