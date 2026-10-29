# Agentic documentation

Agentic is a resumable, issue-driven coding workflow for GitHub repositories.
It gives a configurable coding agent an isolated worktree, runs the target
repository's checks, asks an independent reviewer to inspect the patch, and
preserves evidence when work needs to be resumed.

Use the guides in this site to install Agentic, configure providers and
sandbox boundaries, run and resume work, and troubleshoot validation failures.
The [configuration reference](configuration.md) is based on the canonical
[`agent_config.schema.json`](https://github.com/toanle88/Agentic/blob/main/agent_config.schema.json).

## Start here

- [Getting started](getting-started.md) — install Agentic and run a first task.
- [Five-minute quickstart](quickstart.md) — try a disposable local task.
- [CLI and interactive shell](cli-reference.md) — find common commands.
- [Configuration](configuration.md) — set checks, providers, retries, and policy.
- [Workflow](workflow.md) — understand worktrees, review, resume, and publication.

For contributor-oriented material, see [Development](development.md), the
[development standards](development-standards.md), and the [architecture
decisions](decisions/README.md).
