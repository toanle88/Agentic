# Agentic

[![CI](https://github.com/toanle88/Agentic/actions/workflows/ci.yml/badge.svg)](https://github.com/toanle88/Agentic/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Agentic is a resumable, issue-driven coding workflow for GitHub repositories. It gives a configurable coding agent an isolated Git worktree, investigates whether the issue is actionable, runs the target repository's checks, asks an independent reviewer to inspect the patch, and preserves evidence when work needs to be resumed.

Agentic is designed for human-supervised development. It does not silently commit, push, or open pull requests during development.

## Highlights

- One isolated Git worktree per GitHub issue or local task.
- Read-only issue investigation; findings are recorded before development continues automatically.
- Configurable quick/standard/strict workflow depth, with high-risk investigation always retained.
- Resume support for unfinished or rejected work.
- Configurable project checks using safe argument arrays.
- Independent read-only review before publication.
- Interactive background shell with `/ps` and `/logs`.
- Self-contained HTML dashboard for run history, per-invocation token usage, and a visual token chart.
- Centralized user-level state so target repositories stay clean.
- WSL/Windows Git worktree compatibility when supported by both Git installations.
- Secret redaction in provider, test, and dashboard output.
- Machine-readable diagnostics and worktree status for shell automation.

## Requirements

- Python 3.10 or newer
- Git
- [GitHub CLI](https://cli.github.com/) (`gh`)
- At least one supported coding provider: [Codex CLI](https://developers.openai.com/codex/cli/), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Claude Code](https://docs.anthropic.com/en/docs/claude-code/cli-usage), [GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-cli), or OpenRouter API access
- A GitHub repository with an `origin` remote and a resolvable default branch
- Tools required by the target repository's configured checks

Agentic itself has no third-party Python runtime dependencies.

Authenticate before the first run:

```bash
gh auth login
# Authenticate the provider selected in agent_config.json.
codex login       # Codex
# gemini           # Gemini CLI's authentication flow
# claude           # Claude Code's authentication flow
# copilot          # Copilot CLI's authentication flow
# export OPENROUTER_API_KEY='...'
```

## Installation

`main` is protected. Changes to Agentic itself are made through pull requests; CI runs on every pull request and the `main` branch.

On Debian, Ubuntu, or WSL, install the CLI once with `pipx`; target repositories
do not need copies of the Python files:

```bash
sudo apt update
sudo apt install -y pipx
pipx ensurepath
```

Restart the shell if `pipx` changed your `PATH`. To install a published
GitHub release, choose a version from the [releases page](https://github.com/toanle88/Agentic/releases)
and install its wheel. For example:

```bash
AGENTIC_VERSION=1.0.0
pipx install "https://github.com/toanle88/Agentic/releases/download/v${AGENTIC_VERSION}/agentic_runner-${AGENTIC_VERSION}-py3-none-any.whl"
```

To install the current default branch instead of a pinned release, use:

```bash
pipx install git+https://github.com/toanle88/Agentic.git
```

To update an existing release installation, change `AGENTIC_VERSION` to the
new release and run:

```bash
AGENTIC_VERSION=1.0.1
pipx install --force "https://github.com/toanle88/Agentic/releases/download/v${AGENTIC_VERSION}/agentic_runner-${AGENTIC_VERSION}-py3-none-any.whl"
agentic --version
```

Uninstall it with:

```bash
pipx uninstall agentic-runner
```

For local development, use an editable installation:

```bash
git clone https://github.com/toanle88/Agentic.git
cd Agentic
pipx install --editable .
```

## Quick start

From the root of the target repository, initialize the personal configuration. Agentic detects a conservative starter check and never overwrites an existing configuration unless `--force` is supplied:

```bash
cd /path/to/target-repository
agentic init
agentic init --agents  # optional starter AGENTS.md
```

Review `agent_config.json` and replace the detected command with the target project's real fast checks. See the [configuration guide](docs/configuration.md).

Initialization defaults to trusted host execution so it works with the
provider authentication already installed on the host. To use isolation,
provide an image containing the provider and project tools:

```bash
agentic init --security-mode isolated --sandbox-image your-project-agentic-runner:latest
# equivalent shorthand:
agentic init --secure --sandbox-image your-project-agentic-runner:latest
```

See the [sandbox image guide](examples/sandbox/README.md) for the required
provider and project tooling, including pinned language-specific Dockerfiles.

Provider-ready templates are in [`examples/provider-configs/`](examples/provider-configs/). Copy one to the target repository, or let initialization create it:

```bash
agentic init --provider codex
agentic init --provider gemini
agentic init --provider claude
agentic init --provider copilot
agentic init --provider openrouter
# Use another provider for review:
agentic init --provider codex --reviewer-provider openrouter
```

Provider selection is configured independently for the developer and reviewer:

```json
{
  "developer": {
    "provider": "codex",
    "model": null,
    "reasoning_effort": "max",
    "timeout_seconds": null
  },
  "reviewer": {
    "provider": "codex",
    "model": null,
    "reasoning_effort": "max",
    "timeout_seconds": null
  }
}
```

Supported providers are `codex`, `gemini`, `claude`, `copilot`, and
`openrouter`. Native CLI login remains the default. Any provider can instead
use an environment-backed credential with `auth: "api_key"` and
`api_key_env` (or by setting `api_key_env` alone):

```json
{
  "developer": {
    "provider": "claude",
    "api_key_env": "ANTHROPIC_API_KEY"
  }
}
```

Provider defaults are `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`, and `GH_TOKEN` (GitHub token) for Codex, Gemini, Claude,
and Copilot. OpenRouter uses `OPENROUTER_API_KEY` by default. Export the
selected variable before running Agentic; never put a credential value in
`agent_config.json`. For isolated mode, also list the selected variable in
`sandbox.pass_env`.

Run the environment diagnostic before the first issue:

```bash
agentic doctor
agentic doctor --json
agentic doctor --commands --worktree /path/to/worktree
```

By default, worktrees, logs, shell records, and dashboards live under the user-level Agentic state directory, not beside the target repository. In WSL, use a shared Windows-mounted location when you need to open worktrees with Windows Git:

```bash
agentic --state-dir /path/to/shared-agentic-state --issue 119
agentic --help                           # show all runner options
agentic --version                        # show the installed package version
agentic help                             # equivalent help command
agentic shell                            # open the background-job shell
agentic dashboard                        # generate the HTML dashboard
agentic --list                           # list open GitHub issues
agentic --runs                           # list existing worktrees
agentic status --json                     # machine-readable worktree status
agentic runs --json                       # alias for status
agentic --issue 119                      # create or resume issue 119
agentic --issue 119 --instruction "Bump the version when complete"
agentic --issue 119 --review              # rerun tests and review only
agentic --issue 119 --offline             # resume an existing issue run without GitHub API calls
agentic --issue 119 --mark-done                     # optional reason is recorded automatically
agentic --issue 119 --mark-done --reason "Human reviewed; remaining finding is out of scope"
agentic --issue 119 --publish             # offer draft-PR publication for the approved patch
agentic --issue 119 --new                 # explicitly create a second worktree
agentic audit                              # audit and improve the current repository
agentic audit --review                     # validate/review an existing audit worktree
agentic audit --publish                    # confirm and publish an approved audit patch
```

GitHub issues are the default task source. To use manually pasted requirements,
set `"task_source": "local"` in the target's ignored `agent_config.json`, then
run `/run` in the interactive shell and finish the pasted task with `/done`.
Local tasks use the same review and resume flow and can still be published as
GitHub draft pull requests.

`agentic audit` is independent of GitHub issue and local-task mode. It creates
or reuses a first-class `agent/audit-*` worktree, runs a read-only audit across
code quality, security, reliability, and performance, and passes a bounded
report to the developer. The normal configured checks, independent read-only
review, and safe default-no draft-PR confirmation still apply. Use `/audit` for
the equivalent interactive command.

Before development, an independent read-only investigator analyzes the issue and
records feedback about relevant code, tests, ambiguity, risks, and recommended
next steps. The summary and findings are passed to the developer automatically;
investigation does not produce a status or block development. Invalid
investigator output or investigator tooling failures still stop the run.

Start the interactive shell:

```bash
agentic
```

Then run and monitor an issue:

```text
› /run 119
› /list
› /ps
› /logs 1 80
```

Typing `/` opens an interactive autocomplete dropdown with command descriptions,
arrow key navigation, and live filtering.

The shell shows a compact welcome banner with the repository, state directory,
configuration, and job status. Interactive TTY output uses colors for provider
roles, commands, checks, warnings, and failures while keeping status words
visible. Set `AGENTIC_ASCII=1` for terminals that do not render box-drawing
characters, or `NO_COLOR=1` / `AGENTIC_PLAIN=1` for plain, uncolored output.
Redirected output and persisted logs are plain text.

Persisted provider logs are concise by default: routine session, tool, heartbeat,
and successful command progress stays as live terminal status, while failures,
timeouts, final results, and diagnostics remain in the human-readable log. Use
`agentic --issue 119 --verbose` or `/run 119 --verbose` to retain detailed
progress. Structured provider events remain in the adjacent `.jsonl` trace.
In the interactive shell, `/logs JOB|PID|ISSUE --verbose` replays the captured
detailed output; the default `/logs JOB|PID|ISSUE` view stays concise.

Interactive input keeps a two-line status prompt with the developer provider
and model, working directory, and active task. Up/Down recalls command history;
Ctrl+C cancels the current input, Esc dismisses completion, and bracketed
terminal paste or Shift+Enter inserts multiline text. The editable prompt stays
at the bottom of an interactive terminal; status and command output remain
above it, and completion suggestions appear immediately above the prompt. The
full editable area uses a subtle gray background with high-contrast prompt and
text colors; `NO_COLOR=1`, `AGENTIC_PLAIN=1`, and non-TTY output use the normal
terminal background without ANSI styling. The area normally has one blank row
above and below the input; on short terminals, padding collapses before the
prompt or available completion rows are removed.
Enter submits the complete buffer. PageUp/PageDown scroll the retained output
independently of the editable prompt; new output follows the live tail again
with PageDown.
The normal command input line is shown only when the shell is ready for a new
command. Publication confirmation uses its own prompt, while live
`/logs` follow output temporarily hides the command line and restores it after
following ends. Redirected or exiting shells do not render an interactive
prompt.

For direct foreground execution:

```bash
agentic --issue 119
```

Running the same issue again resumes its existing worktree. Use `--new` only when you intentionally want a separate worktree.

Interactive shell commands include `/audit`, `/run`, `/ps`, `/logs`, `/list`, `/runs`,
`/status`, `/dashboard`, `/init`, `/doctor`, `/cleanup`, `/version`, `/help`,
`/stop`, and `/quit`. Use `/run --task-file PATH` for a local task already saved
in a file. `/runs` lists Agentic worktrees; `/ps --all` lists persisted shell
jobs. Use `/stop --all` to request termination for every running Agentic job.

## Common commands

| Command | Purpose |
| --- | --- |
| `agentic` | Open the interactive background-job shell. |
| `agentic --issue N` | Create or resume issue `N` in the foreground. |
| `agentic --issue N --instruction "..."` | Preserve an optional per-run instruction for development and review. |
| `system_prompt` in `agent_config.json` | Apply persistent, bounded project guidance to developer and reviewer prompts. |
| `agentic --issue N --profile slow` | Run the fast checks plus configured slow checks. |
| `agentic --issue N --review` | Re-run checks and review an existing worktree without coding. |
| `agentic --issue N --mark-done [--reason "..."]` | Record explicit human completion for the current patch. |
| `agentic --issue N --publish` | Request confirmation, then publish the existing approved patch as a draft PR. |
| `agentic --task TEXT` | Start or resume a local task when `task_source` is `local`. |
| `agentic --task-file PATH` | Start or resume a local task from a file. |
| `agentic --task-file task.md --publish` | Publish a reviewed local task as a draft PR. |
| `agentic --issue N --new` | Create a separate worktree for the issue. |
| `agentic audit` | Audit and improve the current repository without an issue or task file. |
| `agentic audit --new` | Create a separate audit worktree instead of reusing the current one. |
| `agentic audit --review` | Re-run checks and the independent read-only review for an audit patch. |
| `agentic audit --publish` | Show the patch/audit summary, ask for confirmation, and create a draft PR. |
| `agentic --list` | List open GitHub issues. |
| `agentic --runs` | List existing Agentic worktrees. |
| `agentic status --json` | Emit machine-readable worktree status. |
| `agentic runs --json` | Alias for `status --json`. |
| `agentic init [--provider NAME] [--security-mode MODE] [--sandbox-image IMAGE] [--sandbox-dockerfile PATH]` | Create ignored target configuration and detect a starter check. |
| `agentic doctor` | Diagnose repository, GitHub task access, and developer/reviewer agent prerequisites. |
| `agentic --state-dir PATH ...` | Store worktrees and runtime artifacts in a chosen location. |
| `agentic cleanup --issue N` | Preview removal of a clean issue worktree; add `--yes` to confirm. |
| `agentic dashboard` | Generate the HTML run dashboard. Add `--serve` for a loopback server that regenerates it for live polling. Remote serving requires `--allow-remote`, `AGENTIC_DASHBOARD_TOKEN`, and TLS certificate/key files. |
| `agentic --help` | Show CLI help. |
| `agentic --version` | Show the installed version. |

## Documentation

| Guide | Contents |
| --- | --- |
| [Getting started](docs/getting-started.md) | Installation, authentication, first run, and target repository setup. |
| [Five-minute quickstart](docs/quickstart.md) | A disposable local-task walkthrough. |
| [Offline benchmarking](docs/benchmarking.md) | Deterministic argv-only benchmark cases and result format. |
| [Configuration](docs/configuration.md) | `agent_config.json`, test commands, model settings, retries, and timeouts. |
| [Agent instructions](docs/agent-instructions.md) | How to create and maintain a target repository's `AGENTS.md`. |
| [Development standards](docs/development-standards.md) | Testing, naming, security, compatibility, and review conventions for Agentic changes. |
| [Workflow](docs/workflow.md) | Worktrees, resume behavior, reviews, publication, logs, and dashboard usage. |
| [Troubleshooting](docs/troubleshooting.md) | Missing tools, failed checks, repeated rejections, and WSL/Git issues. |
| [Architecture](docs/architecture.md) | Components, lifecycle, filesystem layout, and safety boundaries. |
| [Architecture decisions](docs/decisions/) | Durable architecture decision records. |
| [Security policy](SECURITY.md) | Threat model, credentials, logs, and sandbox limitations. |
| [Development](docs/development.md) | Local development, tests, CI, releases, and contribution workflow. |
| [Issue #73 audit (2026-10-21)](docs/audits/2026-10-21-issue-73.md) | Current quality, security, performance findings, measurements, and follow-ups. |
| [`agent_config.schema.json`](agent_config.schema.json) | JSON Schema for editor-assisted configuration validation. |

## Development and releases

Run the local checks with:

```bash
python -m unittest discover -s tests -q
```

Release tags use semantic versioning, such as `v1.0.0`. From a clean,
up-to-date `main` checkout, prepare the version change and open its PR with:

```bash
python scripts/prepare_release.py          # prompt for the new version
# or: python scripts/prepare_release.py 1.0.1
```

The helper runs the local checks, creates a `codex/release-vVERSION` branch,
commits the version change, pushes it, and opens a normal GitHub PR. It
requires the GitHub CLI to be installed and authenticated with `gh auth login`.
After that PR is merged, the preferred path is the GitHub Actions workflow:

1. Open **Actions → Release → Run workflow** and select `main`.
2. Start the run and, when configured, approve the protected `release` environment.
3. Confirm the workflow succeeds and find the GitHub Release and verified
   `release-artifacts` in the run summary.

The workflow reads `agentic_runner.__version__`, runs the same compile, offline test,
and build checks as a tag release, then verifies that the tag and GitHub release
are unused before creating both from the checked commit. No version input is
needed. If the UI path is unavailable, use the local fallback from a
synchronized `main` checkout:

```bash
python scripts/release.py --dry-run  # check everything without creating a tag
python scripts/release.py            # check, confirm, tag, and push
```

The helper verifies that `main` is clean and synchronized with `origin/main`,
runs the compile checks, and prevents reuse of an existing tag. Use `--yes` only
when the release has already been reviewed and should run without a prompt. See
[Development](docs/development.md) for the complete release process.

## Safety summary

Existing and newly initialized configurations use trusted host execution by
default for compatibility with host-installed providers. Use isolated mode for
untrusted repositories or issue text by supplying a project-specific
Docker/Podman image; isolated runs fail closed when the engine, image, or
configured credentials are unavailable. Never expose production credentials.
The developer and reviewer are separate processes; the reviewer uses the
provider's read-only or restricted mode. Agentic does not replace CI, security
review, or human approval.

See the [workflow](docs/workflow.md), [architecture](docs/architecture.md), and [troubleshooting](docs/troubleshooting.md) guides for details.

## License

Agentic is released under the [MIT License](LICENSE).
