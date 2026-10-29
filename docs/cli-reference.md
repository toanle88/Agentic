# CLI and interactive shell

The `agentic` command is the foreground runner and the interactive shell. Use
`agentic --help` for the complete option list; the commands below cover the
normal workflow.

## Common commands

| Command | Purpose |
| --- | --- |
| `agentic init` | Create an ignored target-repository configuration. |
| `agentic doctor` | Check repository, GitHub, provider, and sandbox prerequisites. |
| `agentic --issue N` | Create or resume issue `N` in an isolated worktree. |
| `agentic --task TEXT` | Start or resume a local task when `task_source` is `local`. |
| `agentic audit` | Run a read-only repository audit. |
| `agentic --issue N --review` | Re-run checks and review an existing worktree. |
| `agentic --issue N --publish` | Request confirmation before publishing an approved patch. |
| `agentic status --json` | Emit machine-readable worktree status. |
| `agentic dashboard` | Generate the read-only HTML run dashboard. |
| `agentic --help` | Show all runner options. |
| `agentic --version` | Show the installed version. |

## Interactive shell

Run `agentic` without a workflow argument to open the background-job shell.
Useful commands include `/run`, `/audit`, `/ps`, `/logs`, `/status`,
`/dashboard`, `/init`, `/doctor`, `/cleanup`, `/version`, `/help`, `/stop`,
and `/quit`. Use `/run --task-file PATH` for a local task saved in a file.

The shell supervises jobs and tails their bounded logs. It does not change the
developer/reviewer permissions: development can write its worktree, while
investigation and review remain read-only.

## Safety-related options

- `--offline` resumes an existing run without GitHub API calls.
- `--new` creates a separate worktree instead of reusing the selected run.
- `--secure` selects isolated execution; it requires a configured image.
- `--state-dir PATH` places worktrees and durable state under a chosen root.
- `--verbose` retains more terminal progress in the local run log.

See [Workflow](workflow.md) for lifecycle details and
[Troubleshooting](troubleshooting.md) when a command or check fails.
