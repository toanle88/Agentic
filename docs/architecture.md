# Architecture

## Components

Agentic is intentionally small and uses the Python standard library at runtime.

| Component | Responsibility |
| --- | --- |
| `src/agentic_runner/cli.py` | Canonical CLI entry point and workflow orchestration. |
| `src/agentic_runner/worktrees.py` | Repository identity, locks, Git worktree creation, discovery, and issue intake. |
| `src/agentic_runner/workflow.py` | Developer execution, validation, investigation, review, and publication workflow. |
| `src/agentic_runner/workflow_orchestration.py` | Stateful developer retry loop, review feedback handoff, and bounded retry outcomes. |
| `src/agentic_runner/workflow_state.py` | Bounded workflow run markers, approvals, resume task materialization, and durable evidence helpers. |
| `src/agentic_runner/context.py` | Bounded issue, feedback, and provider-history context formatting. |
| `src/agentic_runner/prompts.py` | Bounded, delimited developer/reviewer prompt construction. |
| `src/agentic_runner/execution.py` | Compatibility facade for provider execution helpers. |
| `src/agentic_runner/execution_stream.py` | Safe argv process streaming, terminal rendering, progress, and usage recording. |
| `src/agentic_runner/execution_providers.py` | Codex and native provider command/message adapters. |
| `src/agentic_runner/output.py` | Shared TTY-only styling and ANSI stripping for terminal presentation and plain logs. |
| `src/agentic_runner/openrouter.py` | Compatibility facade for OpenRouter tools and API execution. |
| `src/agentic_runner/openrouter_tools.py` | Scoped file/command policy and bounded tool execution for review/development. |
| `src/agentic_runner/openrouter_loop.py` | OpenRouter request/response loop and tool-turn orchestration. |
| `src/agentic_runner/policy.py` | Protected-path and sensitive-file policy shared by the runner and API tools. |
| `src/agentic_runner/config.py` | Target-repository configuration loading, provider validation/preflight, test-command detection, and setup defaults. |
| `src/agentic_runner/test_discovery.py` | Deterministic metadata inspection, optional read-only candidate assistance, bounded argv validation, and fast/slow classification. |
| `src/agentic_runner/diagnostics.py` | Repository prerequisite checks and secure-sandbox/provider diagnostics. |
| `src/agentic_runner/shell.py` | Interactive supervisor for background jobs and terminal log tails. |
| `src/agentic_runner/shell_input.py` | TTY input editing, completion, key decoding, styling, and banner presentation. |
| `src/agentic_runner/shell_commands.py` | Pure interactive-shell command and option validation; returns argv fragments without executing them. |
| `src/agentic_runner/shell_jobs.py` | Shell job records, process-group cancellation, bounded log tails, and foreground input forwarding. |
| `src/agentic_runner/dashboard.py` | Read-only parser and renderer for durable run logs. |
| `src/agentic_runner/dashboard_data.py` | Read-only durable-log parsing and legacy/centralized snapshot merging for the dashboard. |
| `src/agentic_runner/persistence.py` | Crash-safe atomic writes for run metadata and durable artifacts. |
| `src/agentic_runner/models.py` | Immutable task, run, state, and provider configuration models. |
| `src/agentic_runner/security.py` | Shared credential redaction for logs and dashboard output. |
| `src/agentic_runner/telemetry.py` | Optional provider usage normalization and durable cost records. |
| `src/agentic_runner/github_comments.py` | Bounded, idempotent final comments for online GitHub issue runs. |
| `src/agentic_runner/sandbox.py` | Optional Docker/Podman command wrapping for providers, OpenRouter tool commands, and checks. |
| `scripts/agent_benchmark.py` | Compatibility-facing benchmark CLI and scenario aggregation orchestration. |
| `scripts/agent_benchmark_config.py` | Benchmark corpus schema, safe path validation, and argv placeholder rendering. |
| `scripts/agent_benchmark_scenarios.py` | Disposable fixture preparation, argv-only adapter execution, checks, and result normalization. |
| `scripts/agent_benchmark_reporting.py` | Provider-neutral aggregation of normalized benchmark scenario metrics. |
| `agent_config.json` | Target-repository checks and per-run policy settings. |
| Git worktrees | Isolate issue, local-task, or repository-audit changes from the base checkout and preserve unfinished work. |
| GitHub CLI | Read issues, post terminal comments, and publish an explicitly approved branch/PR. |
| Provider adapters | Run Codex, Gemini, Claude Code, GitHub Copilot, or OpenRouter for developer/reviewer turns. |

`agentic init` and `agentic doctor` are local setup tools. Initialization creates ignored, target-specific configuration; doctor performs prerequisite checks without running the full project test suite.

## Incremental modularization

The canonical runtime lives in `src/agentic_runner/`; the root command files
are thin compatibility wrappers. Cohesive boundaries are kept in focused
package modules. `config.py` owns configuration
parsing, provider preflight, test-command detection, and setup defaults;
`diagnostics.py` owns the `doctor` workflow; `worktrees.py` owns repository/
worktree lifecycle; and `workflow.py` owns the
developer-to-publication path. `cli.py` binds those components behind the
historical public and patchable helpers, so existing integrations keep working.
Prompt construction is implemented by `prompts.py` and exposed through
`cli.agent_prompt`; shell option parsing and TTY input are separate from
the job supervisor. Provider streaming, provider adapters, OpenRouter policy,
and the OpenRouter loop follow the same facade pattern. Durable artifact writes
use a shared, redacting persistence boundary so the dashboard cannot observe
partially written JSON after an interrupted process. Append-only streaming
logs may be incomplete after interruption and are treated as bounded,
untrusted evidence. The dashboard renderer
and run-log parser are separate so HTML presentation does not own durable-schema
compatibility.

The supported installed import surface is `agentic_runner` plus the three
historical command wrappers. Former top-level implementation modules are
internal package modules and are not installed as separate compatibility
imports.

Tests follow the same boundaries: provider behavior remains in
`tests/test_execution.py` and `tests/test_openrouter.py`; runner configuration
and prompting are split into `tests/test_agentic_configuration.py` and
`tests/test_agentic_prompting.py`; shell input is in `tests/test_shell_input.py`;
and worktree publication/workflow coverage is in
`tests/test_worktrees_publication.py` and `tests/test_worktrees_workflow.py`.
The interactive-shell facade delegates job/process primitives to
`src/agentic_runner/shell_jobs.py` while retaining command dispatch and terminal-mode
ownership. The workflow facade delegates durable run-state helpers to
`src/agentic_runner/workflow_state.py` and the stateful developer retry loop to
`src/agentic_runner/workflow_orchestration.py`, while retaining provider, validation,
investigation, review, and publication transitions. The orchestration module receives
a runtime adapter assembled from the facade's current globals, so historical patch
points remain compatible without moving workflow policy into the compatibility layer.
Both extracted modules are directly importable and
covered by import-boundary characterization tests; the historical
the three root command wrappers remain the compatibility surface.
Benchmark corpus validation, scenario execution, and reporting are split across
`scripts/agent_benchmark_config.py`, `scripts/agent_benchmark_scenarios.py`,
and `scripts/agent_benchmark_reporting.py`; `scripts/agent_benchmark.py`
retains the CLI facade and orchestration contract.
Future extractions should preserve the public facade, add characterization
coverage, and verify the full offline suite.

## Dependency direction

The canonical `src/agentic_runner/cli.py` workflow coordinates configuration,
worktrees, providers, checks, review, and publication. Focused modules may
depend on shared models, policy, security, context, guidance, and telemetry,
but provider adapters must not own workflow or publication decisions. The
dashboard and interactive shell consume run artifacts and do not call
write-capable workflow operations.

## Data flow

An issue, local task, or audit becomes a run and isolated worktree. An audit
has a stable first-class branch identity and a bounded `audit-report.json`
artifact; it does not require issue intake. The workflow builds bounded prompts,
the provider adapter executes in the configured security
boundary, and streamed output is redacted into run artifacts. Tests and the
read-only reviewer consume the worktree and recorded evidence; publication
consumes only an approved, revalidated patch. Repository guidance follows the
same prompt path as other bounded context, while its source files remain in
the worktree and only a sanitized inventory enters run metadata. Terminal
result state for online GitHub issue runs is converted into one allowlisted,
redacted issue comment after the result is recorded; local tasks, audits, and
offline resumes do not perform this external write.

## Extension points

Provider support belongs in an adapter and its command/API policy, with the
common execution and telemetry wrappers preserved. New context sources should
have explicit size limits, untrusted-data delimiters, and regression tests.
New durable metadata must remain optional when reading older runs. New
architecture choices belong in a Markdown ADR under `docs/decisions/`.

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
- The developer receives write access to its worktree while isolated containers overlay the worktree `.git` entry and Git metadata read-only; the reviewer is invoked in the provider's read-only or restricted review mode.
- Native provider CLIs receive provider-specific non-interactive permissions. Gemini and Claude use their read-only review modes; Copilot review turns expose only file-inspection tools and no shell. Agentic captures the staged Git diff with its argv-only runner and supplies it as bounded, untrusted evidence, so native reviewers do not need provider-specific shell permissions. OpenRouter uses an Agentic-owned scoped tool loop.
- The runner refuses protected changes including Agentic configuration, agent instructions, secrets, private keys, and `.codex/`. GitHub workflows are also protected for ordinary repositories; only a checkout sharing Agentic's Git common root may update its own CI workflows.
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
- Container mode mounts the task worktree, overlays its `.git` entry and required Git metadata read-only, and mounts a fresh per-invocation provider output directory. It uses a read-only container root and disables networking by default. Durable run state is never the provider's writable output mount: provider results and schemas are staged in the disposable directory and copied or validated by the host. Read-only filesystem access does not implicitly disable networking: when `sandbox.allow_network` is enabled, both developer and reviewer processes can use outbound networking while the reviewer worktree mount remains read-only. OpenRouter file inspection remains host-side but its command tools use the same container boundary.
- `agentic status --json` exposes stable worktree records for scripts without parsing terminal output.

## WSL and Windows Git compatibility

Git worktrees can store absolute metadata paths. WSL Git and Windows Git use different path formats, so a worktree created by one may be unreadable by the other.

When both installations are Git 2.48 or newer, Agentic enables relative worktree paths. When that is not possible, it uses absolute paths and reports the fallback. Run Agentic and inspect its worktrees from the same environment whenever possible.
