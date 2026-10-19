# Architecture

## Components

Agentic is intentionally small and uses the Python standard library at runtime.

| Component | Responsibility |
| --- | --- |
| `agentic.py` | Compatibility-facing CLI entry point and workflow orchestration. |
| `agentic_context.py` | Bounded issue, feedback, and provider-history context formatting. |
| `agentic_execution.py` | Provider CLI execution, streamed output, console rendering, and usage recording. |
| `agentic_openrouter.py` | OpenRouter tool policy, scoped file/command tools, and API agent loop. |
| `agentic_policy.py` | Protected-path and sensitive-file policy shared by the runner and API tools. |
| `agentic_config.py` | Target-repository configuration loading, provider validation/preflight, test-command detection, and setup defaults. |
| `agentic_shell.py` | Interactive supervisor for background jobs and terminal log tails. |
| `agentic_dashboard.py` | Read-only parser and renderer for durable run logs. |
| `agentic_models.py` | Immutable task, run, state, and provider configuration models. |
| `agentic_security.py` | Shared credential redaction for logs and dashboard output. |
| `agentic_telemetry.py` | Optional provider usage normalization and durable cost records. |
| `agentic_sandbox.py` | Optional Docker/Podman command wrapping for providers, OpenRouter tool commands, and checks. |
| `agent_config.json` | Target-repository checks and per-run policy settings. |
| Git worktrees | Isolate issue or local-task changes from the base checkout and preserve unfinished work. |
| GitHub CLI | Read issues and publish an explicitly approved branch/PR. |
| Provider adapters | Run Codex, Gemini, Claude Code, GitHub Copilot, or OpenRouter for developer/reviewer turns. |

`agentic init` and `agentic doctor` are local setup tools. Initialization creates ignored, target-specific configuration; doctor performs prerequisite checks without running the full project test suite.

## Incremental modularization

The main runner remains the compatibility-facing entry point, but cohesive
boundaries are extracted one at a time. `agentic_config.py` owns configuration
parsing, provider preflight, test-command detection, and setup defaults;
`agentic.py` re-exports extracted names so existing integrations keep working.
Provider and context tests live in `tests/test_execution.py`,
`tests/test_openrouter.py`, and `tests/test_worktrees.py`; `tests/test_agentic.py`
retains workflow and CLI coverage. Future extractions should follow the same
rule: move one boundary, preserve the public entry point, add characterization
coverage, and verify the full offline suite before changing another subsystem.

## Run state

Git worktrees are the source of truth for code state. Durable run logs are stored outside the worktree and contain:

- developer progress and final output;
- provider event traces;
- each test command's output;
- structured review results; and
- evidence used when resuming a rejected run.

The interactive shell keeps a separate terminal-output copy for each background job. The dashboard reads these artifacts without modifying the repository or running tests. Job records contain the PID, command, timestamps, and log path so a later shell can display or stop jobs from an earlier session with `/ps --all`. `/logs` can identify a job by number or PID and follows appended output while the job is running.

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
├── locks/                               # repository/worktree coordination locks
├── shell/job-001.log
├── shell/job-001.json
└── dashboards/dashboard.html
```

Set `AGENTIC_STATE_DIR` or pass `--state-dir PATH` to choose another root. A shared `/mnt` path is recommended when WSL and Windows Git both need to open the worktree. Keeping runtime data outside the checkout reduces the chance that logs or dashboard artifacts enter the proposed patch. Existing sibling state from older versions remains readable during resume.

## Safety boundaries

- Test commands run under the host account by default. When `security_mode` is `isolated`, provider and test processes run in the configured container instead; use explicit `sandbox.pass_env` names for credentials.
- The developer receives write access to its worktree; the reviewer is invoked in the provider's read-only or restricted review mode.
- Native provider CLIs receive provider-specific non-interactive permissions. Gemini and Claude use their read-only review modes; Copilot is restricted to its available read/review tools and denied common Git mutations. OpenRouter uses an Agentic-owned scoped tool loop.
- The runner refuses protected changes including Agentic configuration, agent instructions, secrets, private keys, `.codex/`, and GitHub workflows.
- Issue text, repository files, and test output are untrusted input to the selected provider.
- Provider and test output is redacted before it is persisted or shown in the dashboard; use a trusted checkout because redaction cannot identify every possible secret.
- GitHub issue intake is the default; local task intake is opt-in through `task_source: "local"`.
- The developer does not commit or push. Publication requires a separate validation step and explicit human confirmation.
- A repository lock prevents concurrent mutating runs from corrupting shared Git metadata.
- Repository metadata operations use a shared lock while independent worktrees use separate locks.
- Provider usage and cost are recorded only when the provider reports them; missing usage remains unknown. Each run also records operation-level status, exit code, timestamps, and duration, plus an aggregate elapsed duration. Command arguments and prompt text are not persisted.
- Prompt context is bounded before provider calls. Issue descriptions, handoffs,
  test reports, and review feedback are compacted without persisting their full
  text. OpenRouter file and command results are capped, support line-range file
  reads, and older tool output is replaced with a reread hint before the next
  request. `run.json` keeps aggregate usage plus per-invocation metadata such as
  prompt/completion character counts and tool-turn counts; it also keeps bounded
  operation timing/status metadata for provider and test commands. It never
  stores full prompts or command arguments.
- `security_mode: "isolated"` requires the optional container mode and fails closed when its engine is unavailable.
- Container mode mounts the task worktree, required linked-worktree Git metadata, and output directory, uses a read-only container root, and disables networking by default. Read-only filesystem access does not implicitly disable networking: when `sandbox.allow_network` is enabled, both developer and reviewer processes can use outbound networking while the reviewer mounts remain read-only. OpenRouter file inspection remains host-side but its command tools use the same container boundary.
- `agentic status --json` exposes stable worktree records for scripts without parsing terminal output.

## WSL and Windows Git compatibility

Git worktrees can store absolute metadata paths. WSL Git and Windows Git use different path formats, so a worktree created by one may be unreadable by the other.

When both installations are Git 2.48 or newer, Agentic enables relative worktree paths. When that is not possible, it uses absolute paths and reports the fallback. Run Agentic and inspect its worktrees from the same environment whenever possible.
