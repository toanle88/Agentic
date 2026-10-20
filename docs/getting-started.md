# Getting started

This guide takes you from installation to the first GitHub issue or local-task run.

## Prerequisites

Install or make available:

- Python 3.10 or newer
- Git
- [GitHub CLI](https://cli.github.com/) (`gh`)
- One supported provider: [Codex CLI](https://developers.openai.com/codex/cli/), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Claude Code](https://docs.anthropic.com/en/docs/claude-code/cli-usage), [GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-cli), or OpenRouter API access.
- The tools required by the target repository's test commands

The target must be an existing GitHub repository with an `origin` remote and a resolvable default branch.

Authenticate once:

```bash
gh auth login
# Authenticate the provider selected in agent_config.json, for example:
codex login       # Codex
# gemini           # Gemini CLI
# claude           # Claude Code
# copilot          # GitHub Copilot CLI
# export OPENROUTER_API_KEY='your-key'
```

## Install Agentic

The recommended installation method on Debian, Ubuntu, and WSL is `pipx`:

```bash
sudo apt update
sudo apt install -y pipx
pipx ensurepath
```

Restart the shell if necessary. To install a published GitHub release, choose
a version from the [releases page](https://github.com/toanle88/Agentic/releases)
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

To uninstall the release:

```bash
pipx uninstall agentic-runner
```

Check the installation:

```bash
agentic --version
agentic --help
```

For Agentic development, use an editable installation instead:

```bash
git clone https://github.com/toanle88/Agentic.git
cd Agentic
pipx install --editable .
```

## Configure a target repository

From the target repository root, let Agentic create the personal configuration:

```bash
agentic init
```

The command detects a conservative starter check, adds `agent_config.json` to the target repository's `.gitignore`, and never overwrites an existing config by default. Select a provider with `agentic init --provider gemini` (supported values are `codex`, `gemini`, `claude`, `copilot`, and `openrouter`). Review the generated file and replace the starter command with the target project's real checks. Use `agentic init --agents` if the repository also needs a starter `AGENTS.md`; existing files are preserved. Start with the [configuration guide](configuration.md).

For shared repository guidance, copy [`AGENTS.example.md`](../AGENTS.example.md) to the target repository as `AGENTS.md` and customize it. See the [agent instructions guide](agent-instructions.md).

## Check the environment

```bash
agentic doctor
```

Agentic stores worktrees, run logs, shell records, and dashboards in a user-level state directory. If both WSL Git and Windows Git must access the worktree, choose a shared mounted path:

```bash
agentic --state-dir /path/to/shared-agentic-state --issue 119
```

## Start a run

The default command opens the interactive supervisor:

```bash
cd /path/to/target-repository
agentic
```

Start a background job from its prompt:

```text
agentic> /run 119
agentic> /ps
agentic> /logs 1 80
```

For a foreground run, use:

```bash
agentic --issue 119
```

GitHub issue mode is the default. For manually pasted requirements, set
`"task_source": "local"` in `agent_config.json`, then use `/run` in the
interactive shell. Paste the task and finish with `/done`; local tasks can
still be published as GitHub draft pull requests.

To use a target repository without changing directories:

```bash
agentic --repo /path/to/target-repository --issue 119
```

## What happens next

Agentic creates or reuses an issue worktree, gives the issue to the developer, runs the configured checks, stages the proposed patch, and invokes an independent read-only reviewer. If the review rejects the patch, the worktree and evidence remain available for the next run.

See [Workflow](workflow.md) for resume, retry, publication, and monitoring details.
