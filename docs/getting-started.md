# Getting started

This guide takes you from installation to the first issue run.

## Prerequisites

Install or make available:

- Python 3.10 or newer
- Git
- [GitHub CLI](https://cli.github.com/) (`gh`)
- [Codex CLI](https://developers.openai.com/codex/cli/)
- The tools required by the target repository's test commands

The target must be an existing GitHub repository with an `origin` remote and a resolvable default branch.

Authenticate once:

```bash
gh auth login
codex login
```

## Install Agentic

The recommended installation method on Debian, Ubuntu, and WSL is `pipx`:

```bash
sudo apt update
sudo apt install -y pipx
pipx ensurepath
```

Restart the shell if necessary, then install:

```bash
pipx install git+https://github.com/toanle88/Agentic.git
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

The command detects a conservative starter check, adds `agent_config.json` to the target repository's `.gitignore`, and never overwrites an existing config by default. Review the generated file and replace the starter command with the target project's real checks. Use `agentic init --agents` if the repository also needs a starter `AGENTS.md`; existing files are preserved. Start with the [configuration guide](configuration.md).

For shared repository guidance, copy [`AGENTS.example.md`](../AGENTS.example.md) to the target repository as `AGENTS.md` and customize it. See the [agent instructions guide](agent-instructions.md).

## Check the environment

```bash
agentic doctor
```

Agentic stores worktrees, run logs, shell records, and dashboards in a user-level state directory. If both WSL Git and Windows Git must access the worktree, choose a shared mounted path:

```bash
agentic --state-dir /mnt/d/Dev/AgenticState --issue 119
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

To use a target repository without changing directories:

```bash
agentic --repo /path/to/target-repository --issue 119
```

## What happens next

Agentic creates or reuses an issue worktree, gives the issue to the developer, runs the configured checks, stages the proposed patch, and invokes an independent read-only reviewer. If the review rejects the patch, the worktree and evidence remain available for the next run.

See [Workflow](workflow.md) for resume, retry, publication, and monitoring details.
