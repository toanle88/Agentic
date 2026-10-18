# Configuration

Agentic reads the personal, target-specific `agent_config.json` from the target repository unless `--config` points to another file. The repository contains [`agent_config.example.json`](../agent_config.example.json) as a structural example; it is not a universal configuration.

The easiest setup is `agentic init`. It creates an ignored config with a detected starter check. Treat that check as a starting point and review it before running an issue.

Runtime state is controlled separately from project configuration. Use `--state-dir PATH` or `AGENTIC_STATE_DIR` for worktrees, logs, shell records, and dashboards. This is especially useful in WSL when Windows Git must open the worktree from a shared mounted drive.

## Example

```json
{
  "task_source": "github",
  "test_commands": [
    ["make", "check-fast"],
    ["pnpm", "run", "test:web:fast"],
    ["pnpm", "run", "build:web"],
    ["pnpm", "run", "contract:check"]
  ],
  "slow_test_commands": [
    ["make", "persistence-check"],
    ["pnpm", "run", "test:a11y"]
  ],
  "max_retries": 3,
  "max_files": null,
  "codex_model": null,
  "codex_reasoning_effort": null,
  "codex_timeout_seconds": null,
  "test_timeout_seconds": null
}
```

Use commands that are actually supported by the target repository. Agentic does not assume that the project uses Python, Go, Node, or a particular test framework.

## Settings

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `task_source` | `github` or `local` | `github` | Selects where work requirements come from. GitHub mode uses issue numbers; local mode accepts `--task`, `--task-file`, or pasted tasks from `/run`. |
| `test_commands` | array of argv arrays | required | Checks run inside the worktree. Each command must be an array such as `["go", "test", "./..."]`; shell strings are not accepted. |
| `slow_test_commands` | array of argv arrays | `[]` | Additional checks run after the fast checks when `--profile slow` is selected. |
| `max_retries` | integer `0..3` | `3` | Extra developer attempts after the first attempt. `3` permits up to four total attempts. |
| `max_files` | integer `1..100` or `null` | `null` | Maximum distinct files in the proposed patch. `null` disables this limit. |
| `codex_model` | string or `null` | `null` | Model passed to Codex. `null` inherits the Codex CLI configuration. |
| `codex_reasoning_effort` | string or `null` | `null` | Reasoning setting passed to Codex, such as `max`. `null` inherits the Codex CLI configuration. |
| `codex_timeout_seconds` | integer or `null` | `null` | Developer/reviewer timeout. `null` means unlimited. |
| `test_timeout_seconds` | integer or `null` | `null` | Per-test-command timeout. `null` means unlimited. |

Timeout values must be between 1 second and 24 hours when set. `max_retries` is limited to 0 through 3 by the configuration validator.

## Test command rules

Commands are represented as argument arrays rather than shell strings:

```json
{
  "test_commands": [
    ["go", "test", "./..."],
    ["pnpm", "run", "test:web:fast"]
  ]
}
```

This prevents task text or filenames from becoming shell syntax and makes command execution predictable across environments. Commands run with the issue or local-task worktree as their current directory.

Agentic validates executables and Python `-m` modules before development starts. Missing dependencies, unavailable commands, port conflicts, and missing files are environmental failures; they should be fixed in the environment or configuration rather than retried as code defects.

## Choosing checks

Use a fast, deterministic set for normal iteration. Include the checks needed to prove the issue, for example:

- unit and package tests;
- type checking and production builds;
- API or generated-artifact drift checks; and
- focused frontend tests when the issue changes the UI.

Keep Docker, PostgreSQL, browser, end-to-end, and release-only checks in CI or a separate opt-in command when they require external services or take substantially longer. For persistence or public API changes, run those broader gates before publication even if they are not part of the fast local configuration.

Select the slow profile explicitly:

```bash
agentic --issue 119 --profile slow
```

The slow profile runs every `test_commands` entry first, followed by every `slow_test_commands` entry. If no slow checks are configured, Agentic stops with a configuration error instead of pretending the slow profile ran.

## Per-developer configuration

Configuration is target-repository input. If developers need different models, checks, or limits, keep the personal `agent_config.json` untracked or ignored and commit a separate example file for the team. Never put credentials, API keys, or production connection strings in the configuration.

## Task source

GitHub issue mode is the default:

```json
{ "task_source": "github" }
```

To work from manually pasted requirements instead, set:

```json
{ "task_source": "local" }
```

Local tasks use the same worktrees, tests, reviews, retries, resume behavior,
logs, and dashboard. They can still be published as GitHub draft pull requests
with `--publish`; the PR contains the local task text instead of a `Closes #N`
reference.

For a direct run, use either:

```bash
agentic --task "Add CSV invoice export"
agentic --task-file task.md
```

In the interactive shell, use `/run` without an issue number. Paste the task
and finish with a line containing `/done`:

```text
agentic> /run
Paste the local task. Finish with a line containing /done; use /cancel to abort.
task> Add CSV invoice export.
task> Include regression tests.
task> /done
```

Use an explicit file when needed:

```bash
agentic --repo /path/to/target-repository \
  --config /path/to/agent_config.json \
  --issue 119
```
