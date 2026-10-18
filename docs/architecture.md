# Architecture

## Components

Agentic is intentionally small and uses the Python standard library at runtime.

| Component | Responsibility |
| --- | --- |
| `agentic.py` | Repository validation, GitHub/local task intake, worktree lifecycle, developer/reviewer orchestration, checks, retries, and publication. |
| `agentic_shell.py` | Interactive supervisor for background jobs and terminal log tails. |
| `agentic_dashboard.py` | Read-only parser and renderer for durable run logs. |
| `agent_config.json` | Target-repository checks and per-run policy settings. |
| Git worktrees | Isolate issue or local-task changes from the base checkout and preserve unfinished work. |
| GitHub CLI | Read issues and publish an explicitly approved branch/PR. |
| Provider adapters | Run Codex, Gemini, Claude Code, GitHub Copilot, or OpenRouter for developer/reviewer turns. |

`agentic init` and `agentic doctor` are local setup tools. Initialization creates ignored, target-specific configuration; doctor performs prerequisite checks without running the full project test suite.

## Run state

Git worktrees are the source of truth for code state. Durable run logs are stored outside the worktree and contain:

- developer progress and final output;
- provider event traces;
- each test command's output;
- structured review results; and
- evidence used when resuming a rejected run.

The interactive shell keeps a separate terminal-output copy for each background job. The dashboard reads these artifacts without modifying the repository or running tests. Job records contain the PID, command, timestamps, and log path so a later shell can display or stop jobs from an earlier session with `/ps --all`.

## Filesystem layout

For a repository named `my-project`, Agentic uses a per-repository namespace under the user-level state root:

```text
target-repository/
├── .git/
├── agent_config.json
└── ...
~/.local/state/agentic/my-project-<identity>/
├── worktrees/issue-119-XXXXXXXX/        # GitHub issue
├── worktrees/task-add-export-XXXXXXXXXXXX/ # local task
├── runs/agent-issue-119-XXXXXXXX/
├── shell/job-001.log
├── shell/job-001.json
└── dashboards/dashboard.html
```

Set `AGENTIC_STATE_DIR` or pass `--state-dir PATH` to choose another root. A shared `/mnt` path is recommended when WSL and Windows Git both need to open the worktree. Keeping runtime data outside the checkout reduces the chance that logs or dashboard artifacts enter the proposed patch. Existing sibling state from older versions remains readable during resume.

## Safety boundaries

- Test commands run under the host account, not as isolated application code. Use a trusted or disposable checkout and do not expose production credentials.
- The developer receives write access to its worktree; the reviewer is invoked in the provider's read-only or restricted review mode.
- Native provider CLIs receive provider-specific non-interactive permissions. Gemini and Claude use their read-only review modes; Copilot is restricted to its available read/review tools and denied common Git mutations. OpenRouter uses an Agentic-owned scoped tool loop.
- The runner refuses protected changes including Agentic configuration, agent instructions, secrets, private keys, `.codex/`, and GitHub workflows.
- Issue text, repository files, and test output are untrusted input to the selected provider.
- GitHub issue intake is the default; local task intake is opt-in through `task_source: "local"`.
- The developer does not commit or push. Publication requires a separate validation step and explicit human confirmation.
- A repository lock prevents concurrent mutating runs from corrupting shared Git metadata.

## WSL and Windows Git compatibility

Git worktrees can store absolute metadata paths. WSL Git and Windows Git use different path formats, so a worktree created by one may be unreadable by the other.

When both installations are Git 2.48 or newer, Agentic enables relative worktree paths. When that is not possible, it uses absolute paths and reports the fallback. Run Agentic and inspect its worktrees from the same environment whenever possible.
