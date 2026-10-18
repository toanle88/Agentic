# Agentic

[![CI](https://github.com/toanle88/Agentic/actions/workflows/ci.yml/badge.svg)](https://github.com/toanle88/Agentic/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Agentic is a resumable, issue-driven coding workflow for GitHub repositories. It gives a Codex developer an isolated Git worktree, runs the target repository's checks, asks an independent read-only reviewer to inspect the patch, and preserves evidence when work needs to be resumed.

Agentic is designed for human-supervised development. It does not silently commit, push, or open pull requests during development.

## Highlights

- One isolated Git worktree per GitHub issue.
- Resume support for unfinished or rejected work.
- Configurable project checks using safe argument arrays.
- Independent read-only review before publication.
- Interactive background shell with `/ps` and `/logs`.
- Self-contained HTML dashboard for run history.
- Centralized user-level state so target repositories stay clean.
- WSL/Windows Git worktree compatibility when supported by both Git installations.

## Requirements

- Python 3.10 or newer
- Git
- [GitHub CLI](https://cli.github.com/) (`gh`)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- A GitHub repository with an `origin` remote and a resolvable default branch
- Tools required by the target repository's configured checks

Agentic itself has no third-party Python runtime dependencies.

Authenticate before the first run:

```bash
gh auth login
codex login
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

Restart the shell if `pipx` changed your `PATH`, then install Agentic:

```bash
pipx install git+https://github.com/toanle88/Agentic.git
```

Upgrade or uninstall it with:

```bash
pipx upgrade agentic-runner
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

Run the environment diagnostic before the first issue:

```bash
agentic doctor
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
agentic --issue 119                      # create or resume issue 119
agentic --issue 119 --review              # rerun tests and review only
agentic --issue 119 --publish             # validate, then offer draft-PR publication
agentic --issue 119 --new                 # explicitly create a second worktree
```

Start the interactive shell:

```bash
agentic
```

Then run and monitor an issue:

```text
agentic> /run 119
agentic> /ps
agentic> /logs 1 80
```

The shell shows a compact welcome banner with the repository, state directory,
configuration, and job status. Set `AGENTIC_ASCII=1` for terminals that do not
render box-drawing characters, or `AGENTIC_PLAIN=1` for plain, uncolored output.

For direct foreground execution:

```bash
agentic --issue 119
```

Running the same issue again resumes its existing worktree. Use `--new` only when you intentionally want a separate worktree.

## Common commands

| Command | Purpose |
| --- | --- |
| `agentic` | Open the interactive background-job shell. |
| `agentic --issue N` | Create or resume issue `N` in the foreground. |
| `agentic --issue N --profile slow` | Run the fast checks plus configured slow checks. |
| `agentic --issue N --review` | Re-run checks and review an existing worktree without coding. |
| `agentic --issue N --publish` | Revalidate, request confirmation, then publish a draft PR. |
| `agentic --issue N --new` | Create a separate worktree for the issue. |
| `agentic --list` | List open GitHub issues. |
| `agentic --runs` | List existing Agentic worktrees. |
| `agentic init` | Create ignored target configuration and detect a starter check. |
| `agentic doctor` | Diagnose Git, GitHub CLI, Codex CLI, config, and check prerequisites. |
| `agentic --state-dir PATH ...` | Store worktrees and runtime artifacts in a chosen location. |
| `agentic cleanup --issue N` | Preview removal of a clean issue worktree; add `--yes` to confirm. |
| `agentic dashboard` | Generate the HTML run dashboard. |
| `agentic --help` | Show CLI help. |
| `agentic --version` | Show the installed version. |

## Documentation

| Guide | Contents |
| --- | --- |
| [Getting started](docs/getting-started.md) | Installation, authentication, first run, and target repository setup. |
| [Configuration](docs/configuration.md) | `agent_config.json`, test commands, model settings, retries, and timeouts. |
| [Agent instructions](docs/agent-instructions.md) | How to create and maintain a target repository's `AGENTS.md`. |
| [Workflow](docs/workflow.md) | Worktrees, resume behavior, reviews, publication, logs, and dashboard usage. |
| [Troubleshooting](docs/troubleshooting.md) | Missing tools, failed checks, repeated rejections, and WSL/Git issues. |
| [Architecture](docs/architecture.md) | Components, lifecycle, filesystem layout, and safety boundaries. |
| [Development](docs/development.md) | Local development, tests, CI, releases, and contribution workflow. |

## Development and releases

Run the local checks with:

```bash
python -m unittest discover -s tests -q
```

Release tags use semantic versioning, such as `v0.1.1`. The release workflow
verifies the tag against `agentic --version`, builds the package artifacts, and
creates a GitHub release. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
pull-request and release process.

## Safety summary

Agentic runs configured checks under the host account, so use a trusted or disposable checkout and never expose production credentials. The developer and reviewer are separate processes; the reviewer is read-only. Agentic does not replace CI, security review, or human approval.

See the [workflow](docs/workflow.md), [architecture](docs/architecture.md), and [troubleshooting](docs/troubleshooting.md) guides for details.

## License

Agentic is released under the [MIT License](LICENSE).
