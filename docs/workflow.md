# Workflow

## Run lifecycle

Each issue or local-task run follows this sequence:

New issue and local-task worktrees use timestamped branches such as
`agent/issue-119-20261022T043125123456Z-a1b2c3d4` and
`agent/task-add-export-<task-key>-20261022T043125123456Z`. When multiple runs
exist for the same issue or task, resume and publication selection lists the
newest timestamp first. Legacy branch names remain supported.

1. Resolve the repository's base branch; GitHub mode also loads the GitHub issue.
2. Create or reuse an isolated worktree for the issue or deterministic local task.
3. Ask an independent read-only configured investigator to analyze the issue and repository. The default `standard` mode does this for every task; `quick` may skip it only for conservatively classified low-risk tasks, while `strict` always retains it. Security, credential, data, migration, publication, and protected-workflow tasks are never eligible for the quick skip.
4. Record the investigation feedback and pass it to the developer automatically.
5. Give the task and repository instructions to the configured developer provider.
6. Run every configured test command. Agentic removes extra final blank lines
   that Git reports as `new blank line at EOF`; other whitespace errors still
   fail validation.
7. Stage and inspect the proposed patch.
8. Ask an independent read-only configured reviewer for a structured verdict.
9. Approve, retry with evidence, or preserve the worktree for resume.

Approved, validated, and manually approved runs print a terminal-only execution
summary. It reports bounded total and per-stage token telemetry, prompt/output
breakdowns, provider cost when available, stage durations and counts, retries,
review findings, and the run-log path. Missing provider telemetry is shown as
`unknown`; local test commands always show `0 tokens`. ANSI styling is used
only for interactive terminal output and is never written to durable logs.

## Repository audits

Run a reusable repository audit without creating a GitHub issue or task file:

```bash
agentic audit
```

The audit gets a distinct `AUDIT` identity and timestamped
`agent/audit-<key>-<UTC timestamp>` branch. When multiple audit worktrees
exist, publish/resume selection lists the newest audit first. Every
plain invocation starts a fresh audit attempt. Use `--resume` to reuse an
existing audit worktree and cached report, or `--new` to force a separate
worktree even for the first attempt. `/audit` in the interactive shell has the
same behavior and runs in the background unless `--publish` is selected.

The audit provider is an independent read-only invocation using the configured
reviewer boundary. It does not edit files, run project code or tests, access the
network, or receive extra permissions. It examines four required categories:
code quality/architecture, security boundaries and secret handling, reliability
and error handling, and performance risks/measurements/benchmarks. Its bounded
structured report is stored outside the worktree as `audit-report.json` and is
also visible in the read-only dashboard.

The report schema is version 1 and contains `summary`, up to 40 findings,
`measurements`, and `skipped`. Findings have an id, one of `code_quality`,
`security`, `reliability`, or `performance`, a severity from `CRITICAL`,
`HIGH`, `MEDIUM`, `LOW`, or `INFO`, plus bounded description, evidence, and
recommendation fields. Findings are ordered with critical/high risks first.
Concrete critical and high security/correctness findings are highlighted first;
focused low-risk medium findings may be included, while large refactors and
uncertain performance hypotheses remain documented evidence.

The audit stops after the report is recorded: it does not run baseline checks,
start a developer, review a patch, commit, push, or modify repository code.
The dashboard and `/logs` can inspect the durable report and provider logs.
`audit --publish` asks for `y`, `yes`, or `publish`, then creates a GitHub issue
from the saved report without creating a commit or pull request. The issue title
includes the audit worktree's UTC timestamp so repeated audit publications are
easy to distinguish. Cancellation retains the worktree and report for resume.
When `automation.auto_publish` is `true`, a valid completed audit attempts this
publication without prompting. Existing issues are detected and recorded
locally; unavailable access or a publication failure leaves the report,
worktree, and logs intact for a later `--publish` retry. `audit --offline` can
resume an existing audit worktree without GitHub issue intake, and automatic
publication is skipped for offline runs.

## Issue investigation

Investigation is a read-only analysis before development. The investigator
inspects the issue against the repository and records structured feedback in the
run logs as `investigation-*.json`. The feedback contains a summary and concise
findings covering relevant code and tests, ambiguity or risks, and recommended
next steps. It is passed to the developer as context for the first attempt; it
does not produce a status or block development. Invalid investigator output or
investigator tooling failures still stop the run.

The base checkout is not the development worktree. This keeps unfinished changes isolated from the user's main checkout.

## Interactive shell

Run `agentic` with no action flags to open the interactive supervisor:

```bash
agentic
```

Available commands:

```text
/run ISSUE [--profile fast|slow]       Start a background job
     [--review|--publish|--mark-done|--new|--verbose]
     [--reason "why"] with --mark-done
/run [--profile fast|slow]             Local mode: paste task, finish with /done
     [--review|--publish|--mark-done|--new|--verbose]
/ps [--all]                            Show current or persisted job status
/logs JOB|PID|ISSUE [LINES] [--verbose] Follow a running job or show recent output
/stop JOB|--all                        Stop one or all running jobs
/list                                  List open GitHub issues
/runs [--json]                         List existing Agentic worktrees
/dashboard [OUTPUT]                    Generate and open the HTML dashboard
/init [OPTIONS]                        Initialize target-repository configuration
/doctor [--json]                       Diagnose local prerequisites
/cleanup --issue N [OPTIONS]           Preview or remove clean worktrees
/version                               Show the Agentic version
/status [--json]                       Show worktree status
/help                                  Show command help
/quit                                  Leave the shell; jobs continue running
```

Type `/` to open an interactive autocomplete dropdown showing available commands
and their descriptions. Use the <kbd>↑</kbd> and <kbd>↓</kbd> arrow keys to navigate,
<kbd>Enter</kbd> or <kbd>Tab</kbd> to select the highlighted item, and <kbd>Esc</kbd>
to dismiss. Supported options such as `/run --profile` and known job IDs for
`/logs` and `/stop` are completed dynamically. `/logs` suggests the 20 newest
completed jobs plus every running job; older completed IDs remain valid when
entered explicitly. On interactive terminals, Agentic
renders the dropdown with live filtering and command descriptions using standard ANSI
escapes with zero third-party dependencies. The editable prompt is anchored to
the terminal's bottom row, with status, command output, and completion rows above
it. Its full-width padded editable area uses a subtle gray background and readable
foreground colors; `NO_COLOR=1` and `AGENTIC_PLAIN=1` fall back to the normal
terminal background. The area normally has one blank row above and below the
input; on short terminals, padding collapses before the prompt or available
completion rows are removed. Output is written through the editor redraw path and retained in an independently
scrollable viewport; PageUp/PageDown review older output without moving the prompt,
and PageDown returns to the live tail. A terminal resize notification also wakes an
otherwise idle prompt so it can be redrawn at the new bottom row.

Interactive terminals show a persistent two-line prompt with the configured
developer model, working directory, and current task. Use Up/Down for command
history, Ctrl+C to cancel the current input, Esc to dismiss completion, and
Shift+Enter or bracketed terminal paste for multiline input. Pasted newlines
are inserted into the current buffer and are submitted with Enter. Redirected
input and output continue to use the plain `input()` path without ANSI control
sequences. Completion rows are reduced automatically when the terminal is short;
`AGENTIC_PLAIN=1` keeps the platform's line-oriented plain-input fallback.

`/run` starts a child process and returns immediately. `/ps` shows jobs started in the current shell; `/ps --all` also discovers job records from previous shell sessions. The child runner's durable logs remain available after the shell exits, and `/logs JOB` works for persisted jobs too. Use `/logs JOB` or `/logs PID` on a running job to follow new output until it exits; press Ctrl-C to stop following without stopping the job. Add `LINES` to control how much existing output is shown first. Running jobs are listed before completed jobs so active work is immediately visible. Use `/stop JOB` to stop one job or `/stop --all` to request termination for every running Agentic job visible to the shell, including jobs from previous shell sessions.

When a background job exits unsuccessfully, the shell prints its existing
failure notification followed by the default bounded concise log tail. This
keeps retry, test, provider, and final-result summaries visible without dumping
the full transcript; use `/logs JOB --verbose` for complete output.

When a background job completes, the shell also prints the durable final result,
test and review statuses, run duration, total token usage, estimated cost, and
the full shell log path. Missing values are shown as `unknown` (or `not-run`
when the durable run explicitly has no test or review result). This summary is
emitted after the shell log has finished draining; `/logs JOB` and
`/logs JOB --verbose` remain the paths to the complete transcript.

`/list` synchronously lists the target repository's open GitHub issues. It uses
the same GitHub CLI-backed issue listing as `agentic --list` and does not create
a background job.

`/runs` lists Agentic worktrees, matching `agentic --runs`; `/ps --all` remains
the command for inspecting persisted interactive-shell jobs. Use
`/run --task-file PATH` to start a local task from an existing task file. Setup
and maintenance commands such as `/init`, `/doctor`, and `/cleanup` run
synchronously so their output and confirmation errors are shown immediately.
Use `/doctor --commands --worktree PATH` when you want doctor to execute the
configured checks in a specific worktree; this is opt-in because checks may
install dependencies or modify generated files.

The default `fast` profile runs the configured `test_commands`. Use `/run 119 --profile slow` to run those checks followed by the configured `slow_test_commands`. Profiles select which checks run; they do not select the provider model or reasoning setting.

Workflow depth is independent of the test profile. Set `"workflow_mode"` in
`agent_config.json` or pass `--workflow-mode quick|standard|strict` for one
run. Quick mode is an explicitly conservative optimization, not a way to
disable investigation for high-risk work.

## Context and usage efficiency

Guidance selection is phase-specific while `AGENTS.md` remains mandatory:
investigation uses architecture, development standards, and ADRs;
development additionally uses the development and agent-instructions guides;
review uses architecture, development standards, and ADRs. Each load records
only relative path, source size, digest, selected character count, phase, and
prompt-size statistics.

OpenRouter budgets are character-based because Agentic has no tokenizer. The
request budget is 48,000 characters: reserve 8,000 for the response, reserve
up to 8,000 for serialized tool schemas, cap system instructions at 6,000 and
the issue/task context at 12,000, then compact history in this order: old tool
results, oversized recent tool results, and older textual context. The final
serialized messages plus tools are checked against the budget. Assistant tool
calls and their matching results remain in the history.

Large review patches are those over 24,000 characters. They include a bounded
patch-stat section and changed-file name/status manifest, then try to select
one representative file section per changed file and prioritize sections by
changed-line count (ties are lexical) within the hunk budget. Omitted middle
sections are called out so the read-only reviewer can inspect affected files.
Smaller patches retain the complete diff.

Review findings use `F-<12 lowercase hex>` IDs derived from the normalized
requirement; duplicate fingerprints receive `-2`, `-3`, and so on. Each
transmitted finding retains its ID, a concise requirement, and an action target.
If their minimum representations cannot fit the normal feedback bound, the
optional summary and skipped-validation text is omitted rather than dropping a
finding.

Provider usage is kept per invocation and by investigator, developer, and
reviewer stage. Prompt, completion, total, cache-read, cache-write, and
reasoning fields remain unknown when a provider does not report them.
Explicitly cumulative JSONL usage is counted from its final snapshot; events
without that provider declaration remain additive per event. Run metadata
also exposes retries, first-pass approval, review findings, and tokens per
approved task.

For an A/B measurement, use the same 10–20 representative tasks, including
low-, medium-, and high-risk examples, with `workflow_mode=standard` and
`workflow_mode=quick`. Compare the durable `by_stage`, `workflow_metrics`,
and `tokens_per_approved_task` fields together with first-pass approval,
failed attempts, review findings, human intervention, and completion time.
Treat the change as successful only when median tokens per approved task falls
by at least 10% with no more than a five-point first-pass-approval decrease,
no decrease in review-defect recall, and no increase in human intervention.
Prompt size alone is not a success criterion; report the raw task-level data
and any unavailable provider fields.

GitHub issue mode is the default. If `task_source` is `local`, use `/run`
without an issue number, paste the requirements, and finish with `/done`.
Pasting the same task again produces the same task identity and resumes its
existing worktree. Local tasks can still use `/run --publish` to create a
GitHub draft pull request.

On an interactive terminal, workflow prefixes such as `[DEVELOPER]`, `[TESTS]`, `[REVIEW]`, `[PATCH]`,
`[GIT]`, `[RETRY]`, `[ERROR]`, and `[RESULT]` are styled without coloring their
message bodies. The `[PATCH]` Git diff-stat summary is the intentional exception:
added graph markers and insertion totals are green, while removed markers and
deletion totals are red. System-generated prefixes use the shared formatter, including
new categories supplied through its explicit system-prefix boundary; generic
provider and user text remains unclassified. Repeated provider progress and elapsed-time updates use one
replaceable status row; important events remain in scrollback. Redirected output,
`NO_COLOR`, and persisted run logs retain plain, line-oriented text.

## Resume and retries

Run the same issue again to resume its existing worktree:

```bash
agentic --issue 119
```

When an online resume finds that the GitHub issue title, body, or state changed,
Agentic prints a bounded change summary and starts a new investigation,
development, test, and review pass. Previous manual or automated publication
approvals are removed before any publication decision and an invalidation event
is retained in the run logs. The previous and current bounded issue snapshots,
their non-secret digests, and the changed fields are retained in
`issue-history.json`; the current snapshot remains the source for offline
resume. A changed issue's terminal completion comment uses a new marker and
includes the concise change summary, so the update is visible on GitHub.

If GitHub is temporarily unavailable, resume an existing GitHub run from the
bounded issue snapshot recorded at the start of an earlier run:

```bash
agentic --issue 119 --offline
```

Offline mode does not contact GitHub or refresh the base branch. It is limited
to an existing issue worktree, and provider or project checks may still use
their configured network access. Run the issue once online before relying on
offline resume so the snapshot is available.

Use `--profile slow` when the issue needs the configured integration, browser, or release checks:

```bash
agentic --issue 119 --profile slow
```

### Repair failed CI on an existing pull request

CI repair is explicitly opt-in:

```bash
agentic --pr 123 --repair-ci
# Start fresh while preserving the current dirty patch in a Git stash:
agentic --pr 123 --repair-ci --clean
```

The interactive shell provides `/repair-pr 123 [--clean]` and runs it in the foreground
so confirmation prompts remain available. Agentic reads failed check summaries
and bounded failed-run log excerpts as untrusted evidence, classifies
the failure, and uses only the exact existing worktree for the PR branch. A
new repair requires a clean worktree and `HEAD` equal to the current PR head.
If a previous Agentic repair exhausted its developer attempts, a later repair
can continue its preserved patch only when durable PR, branch, head, worktree,
and failed-attempt records match; unrelated dirty, missing, ambiguous, or
stale worktrees still stop with an actionable blocker. Pass `--clean` to
stash the current dirty changes, including untracked files, and start a fresh
repair; the stash remains recoverable with Git. Repair records from older
Agentic versions without content fingerprints require `--clean` once.
Before reading the PR, Agentic verifies the active `gh` authentication using
the same `GH_TOKEN` and `GH_HOST` environment that repair commands receive.
Authentication failures and GitHub API connectivity failures are reported
separately so a transient TLS or network problem is not mistaken for a bad
credential. CI log text is never treated as commands.

Code/test and identified flaky failures may be repaired automatically.
Infrastructure, permission, and ambiguous failures require explicit
confirmation unless `ci_repair.auto_confirm_non_code_failures` is `true`.
That setting is intended only for explicitly unattended repair workflows. The
developer remains write-capable and the reviewer remains read-only. Local
checks and review must approve a patch before publication.
With `automation.auto_publish` false (the default), Agentic asks before
committing and pushing; true permits the guarded same-branch push. The
`ci_repair.max_attempts` and polling settings bound repeated repair cycles and
post-push monitoring. Evidence, attempts, review, push, and CI outcomes are
retained in the durable run logs.

An optional per-run instruction can be supplied when starting or resuming an
issue. It is stored in the run metadata and passed to the developer and
read-only reviewer as bounded, explicitly delimited untrusted data:

```bash
agentic --issue 119 --instruction "Bump the version when the implementation is complete"
```

If a resume omits `--instruction`, Agentic reuses the stored value. Supplying
it again replaces the stored value; an empty value clears it. The interactive
shell accepts the same option, for example `/run 119 --instruction "..."`.

Persistent project guidance belongs in the target repository's
`agent_config.json` as `system_prompt`. It is bounded, escaped, and included
as actionable guidance in the developer prompt and as read-only review context:

```json
{
  "system_prompt": "Follow our development standards. Prefer small focused changes and add tests for behavior changes."
}
```

`system_prompt` is reread on each invocation, including resumes. The developer
follows it when compatible with repository safety rules; the reviewer and
investigator evaluate it without executing it. Repository safety rules,
`AGENTS.md`, and read-only review boundaries are authoritative over both prompt
values. Unlike the per-run
instruction, the configured prompt is not stored in run metadata or shown in
the dashboard.

Agentic preserves code, commits, test output, reviewer output, and structured evidence after failures, timeouts, rejected reviews, and exhausted retries. On resume, the latest failed test and review evidence is supplied to the developer as untrusted feedback, regardless of provider.

If the same rejected review is returned without a changed proposed patch,
Agentic marks the run `blocked`, preserves the worktree and evidence, and skips
future developer attempts for that unchanged patch and issue. The blocked
marker stores the issue fingerprint (including its title and body), so a later
resume after the issue changes clears the terminal marker and invokes the
developer again even when the patch is unchanged. The terminal summary says
when this issue change caused the resume. A blocked run is not approved and
cannot be published.

If configured checks pass but the developer reports a blocker without producing
any code changes, Agentic also marks the run `blocked` without invoking the
reviewer. The result includes the bounded developer blocker summary and the
durable run-log directory; resolve the blocker, add the required patch, and
resume the run. This marker also stores the issue fingerprint and resumes when
the issue changes, even if no code patch was produced previously.

Final development results are explicit: `RETRY_EXHAUSTED` means every
configured attempt was used and another repair may help; `BLOCKED` means the
requirements, contract, or decision needed to proceed is missing; and
`NO_CHANGES` means checks passed but the developer produced no patch. The
terminal summary reports attempts used, the configured retry limit, the latest
failure or blocker summary, the preserved worktree and logs, and the next
resume action. These results are also retained in the run's `result.json`
artifact; older `blocked.json` markers remain readable. For local tasks, the
resume action uses a durable `resume-task.md` copy in the run log directory
and emits a shell-safe `--task-file` invocation, so task text is not copied
into terminal summaries or `result.json`.

Failures caused by unavailable infrastructure, missing dependencies, or invalid
Git paths are not retried as code repairs. They stop the developer loop with
the original diagnostic so the environment or preserved worktree can be fixed;
the reviewer is not invoked because validation did not pass.
If the same retryable test failure is reported on consecutive validation
attempts, the run is marked `BLOCKED` immediately with its failure digest and
diagnostic, instead of consuming the remaining retries. Test diagnostics are
rendered with a `[TESTS]` prefix, including when their content contains text
such as `[REVIEW]`, so nested test output is distinct from the outer review
stage.

For an online GitHub issue run, Agentic automatically posts one concise final
comment after a terminal `result.json` is recorded. The GitHub-native Markdown
uses a heading, compact result/test/review/duration status table, collapsible
usage and per-stage token/cost table, prominent next action, and a secondary
branch footer. Status-specific icons and labels make approved, passed, failed,
blocked, and unknown states easy to scan. Missing metrics are shown as
`unknown`; Agentic does not estimate them. The comment contains no provider
transcript, issue text, local path, or credential. A deterministic marker and a
local delivery record make retries idempotent, and a GitHub delivery failure
leaves the terminal result intact for later retry.

This behavior is enabled by default and needs no flag. It requires the
authenticated GitHub CLI identity to have permission to read issue comments
and write issue comments (the repository's Issues: write permission, or an
equivalent token scope). `--offline`, local tasks, and read-only audits do not
contact GitHub for completion comments.

Use `--new` only when you intentionally want a second worktree:

```bash
agentic --issue 119 --new
```

`--new` does not continue the existing worktree's repair history.

## Review behavior

The reviewer is a separate read-only process. It sees the current staged patch,
issue requirements, test evidence, and the developer's latest handoff. The
handoff is untrusted evidence, but it lets the reviewer check the developer's
claims about implemented, verified, unresolved, blocked, and skipped work. The
developer must also provide one `Review finding actions` entry per prior
reviewer finding, stating the action and evidence; the reviewer checks that
list against the patch rather than trusting it. The review returns an approval
or rejection with findings. If a validation step
cannot be performed because a dependency, service, or tool is unavailable, the
reviewer records it in `skipped` with a reason; skipped validation is visible in
the review log and dashboard and is not itself an approval or rejection. A
rejection does not discard the worktree. The developer receives the findings
and skipped-validation context on the next retry, up to the configured retry
limit, and is instructed to reconcile each item explicitly.

The approval threshold is limited to blockers: concrete implementation defects,
unmet explicit requirements, regressions, security problems, or required
coverage gaps tied directly to the work item. Style preferences, optional tests,
speculative risks, future enhancements, and unavailable validation do not justify
rejection. When no blocker remains, the reviewer approves with an empty
`findings` list.

Scope is enforced independently of severity: findings must concern behavior
introduced by the patch and required by the issue. Pre-existing defects,
unrelated cleanup, refactoring opportunities, and optional improvements are
omitted from the review rather than turned into rejection reasons.

Repeated rejection usually indicates an unresolved requirement, missing integration coverage, or an issue that is too broad. Increasing retries does not lower the review standard.

## Publication

The developer does not commit, push, or open pull requests. After tests and review approve the current patch, publish explicitly:

```bash
agentic --issue 119 --publish
```

`--review` establishes the approval by running the configured checks and the
independent reviewer. `--publish` does not run checks or review; it verifies
that the current patch still matches the recorded approval, then asks for a
safe default-no confirmation: enter `y` or `yes` to continue, or `n`, `no`,
`q`, or an empty response to cancel. The legacy `publish` approval alias
remains accepted for compatibility.

If the automated review is repeatedly rejecting a finding that a human has
explicitly judged out of scope, mark the existing worktree complete with an
auditable reason, then publish normally:

```bash
agentic --issue 119 --mark-done --reason "Human reviewed; finding is outside this issue"
agentic --issue 119 --publish
```

`--mark-done` does not close the GitHub issue and does not publish anything. It
records the current patch digest outside the repository. The `--reason` option
is optional; when omitted, Agentic records `Manually marked done by user`.
Publication uses the recorded approval, refuses any patch changed after the
approval, asks for the safe default-no confirmation described above, and opens
only a draft PR.
It does not run tests or an automated review. If the code or issue changes,
rerun development or `--review` first; for a manual completion, mark it done
again with a new reason.

In the interactive shell, `/run ... --publish` runs in the foreground so the
shell can receive the exact publication confirmation. Other `/run` operations
remain background jobs. If no valid approval is recorded, publication stops
and asks you to run development or `--review` first.
When multiple worktrees are available, Agentic prints a waiting message and
asks for a number from the displayed list; enter `q` (or press Ctrl-C) to
cancel. It confirms the selected worktree before continuing. Before any
commit or push, it prints `[PUBLISH] Publish this patch? [y/N]:`; enter `y` or
`yes` to approve, or press Enter, `n`, `no`, or `q` to cancel. The legacy
`publish` approval alias is also accepted. Invalid responses explain the
accepted values and repeat the prompt. While a foreground child is running, its output and progress stay
above a reserved bottom input area; these prompts remain visible there and are
redrawn on terminal resize. At a publication prompt, enter `/stop` to cancel the
foreground operation; Agentic also records its foreground job ID so `/stop JOB`
from another shell can terminate it.

Generated draft PR bodies include a `What changed` section immediately after
the issue or task context, followed by the existing validation and
independent-review sections, then `Notes` and the draft status reminder. The
change summary combines the approved final diff statistics with
up to a few lines from the latest developer handoff's implemented/summary
section. Handoff text is bounded, redacted, stripped of terminal controls, and
escaped before it is inserted. If the handoff or review/telemetry details are
unavailable, the body retains the final-patch fallback and the validation,
review-status, and run-log notes.

For a local task, use `agentic --task-file task.md --publish` or resume from
the saved task file created by the interactive shell.

Agentic verifies that the patch still matches its recorded approval and asks
for the safe default-no confirmation. It does not run tests or review during
publication. Only after confirmation does it commit, push the issue branch,
and open a draft pull request. The draft state leaves a final human
review step before merging.

When Agentic is run from a checkout of its own repository, changes to its
`agentic.py` runner and `.github/workflows/` CI files are allowed through patch
validation so Agentic can develop and verify itself. The protected-file policy
remains active for every other repository and still blocks `agent_config.json`,
`AGENTS.md`, credentials, and other sensitive paths.

## Logs and dashboard

Durable artifacts are stored in the centralized Agentic state directory:

```text
~/.local/state/agentic/my-project-<identity>/worktrees/issue-119-<timestamp>-XXXXXXXX/
~/.local/state/agentic/my-project-<identity>/runs/agent-issue-119-<timestamp>-XXXXXXXX/
~/.local/state/agentic/my-project-<identity>/shell/job-001.log
~/.local/state/agentic/my-project-<identity>/diagnostics/agentic.log
```

Run logs include developer and reviewer progress, provider event traces, test output, structured review results, and resume evidence. They are outside the worktree so they cannot accidentally become part of the patch.

Application failures are recorded separately in the JSON-lines diagnostic log.
Handled CLI and interactive-shell errors, uncaught exceptions, and interrupts
include a timestamp, Agentic version, sanitized command shape, runtime details,
exception type, and bounded stack structure. Argument values, issue and
repository text, environment variables, credentials, and provider output are
not recorded. The active file is capped at 256,000 bytes and rotates through
three numbered backups, so inspect the active file and `agentic.log.1` through
`agentic.log.3` when looking for older events. Its location is shown in the
state-directory layout above; `--state-dir PATH` changes the root.

Human-readable provider logs are concise by default. Routine session, tool,
heartbeat, and successful command-completion messages are kept as transient
terminal status or summarized, while errors, non-zero exits, timeouts,
cancellations, test failures, review findings, final responses, and duration
remain durable. Pass `--verbose` to the regular CLI, for example
`agentic --issue 119 --verbose`, or use `/run 119 --verbose` in the interactive
shell to retain the detailed progress output. `/logs JOB|PID|ISSUE --verbose`
shows the captured shell output without concise replay filtering;
`/logs JOB|PID|ISSUE` keeps routine progress out of the human-readable view.
For test commands, the concise view keeps the command, pass/fail or timeout
result, and duration but omits successful test-case/output lines. Failed and
timed-out commands include a bounded diagnostic tail of at most 80 lines and
12,000 characters, followed by the full-log pointer; use `--verbose` to replay
the complete captured output. The same filtering applies while following a
running job; when that follow is non-interactive, the latest transient status
is emitted once so the output still shows that the job is active.
Structured providers keep the complete redacted event stream in the adjacent
`.jsonl` file; the human log is not a replacement for that trace.

Generate a read-only HTML snapshot:

```bash
agentic dashboard --repo /path/to/target-repository
```

The default output is `dashboards/dashboard.html` in that state namespace. Choose another location with `--output`:

```bash
agentic dashboard \
  --repo /path/to/target-repository \
  --output /path/to/agentic-dashboard.html
```

For live updates without regenerating the page, run the built-in read-only
server instead:

```bash
agentic dashboard --repo /path/to/target-repository --serve --port 8765
```

Open the printed `http://127.0.0.1:8765/dashboard.html` URL. Each dashboard
request rescans the run logs, and the page polls that endpoint every 30 seconds;
the browser updates its views in place. The server binds to loopback by
default and exposes no write or publication actions.

To serve the dashboard on a non-loopback interface, opt in explicitly, provide
a TLS certificate and key, and set a random token through an environment
variable:

```bash
export AGENTIC_DASHBOARD_TOKEN='use-a-long-random-value'
agentic dashboard --serve --host 0.0.0.0 --allow-remote \
  --tls-certfile server.crt --tls-keyfile server.key
```

Remote serving uses HTTPS with TLS 1.2 or newer and refuses to start without both TLS files,
`--allow-remote`, and the token environment variable. API clients may send
`Authorization: Bearer <token>`. Browsers can use HTTP Basic authentication
with username `agentic` and the token as the password; this also allows live
refreshes to continue working.

In the interactive shell, `/dashboard` generates the snapshot and opens it with
the host operating system's default browser. Use `/dashboard PATH` to choose a
specific output file. Regenerate the dashboard after a run to refresh its
contents. The dashboard is organized into Dashboard, Runs, Logs, Token usage,
Issues, and Settings views. Global search covers bounded run metadata, findings,
errors, and embedded log text. Status, issue number, stage, severity, model, and
provider filters apply across the views. Severity options are BLOCKER, CRITICAL,
MAJOR, HIGH, MEDIUM, MINOR, LOW, UNSPECIFIED, and no finding. Token usage
additionally supports all-time, 24-hour, 7-day, and 30-day periods.

Runs, Logs, Token usage, and Issues use bounded client-side pages of 25
matching records. Each view initially renders its newest page and shows a
Load more control with the current/total count when older matches exist.
Global filters and search apply before paging and reset the view to its first
page, so older matching records remain reachable without rendering the entire
history. The generated HTML still contains the bounded,
self-contained snapshot required for file:// dashboards; --serve continues
to regenerate that snapshot on refresh.

The Issues view groups the locally recorded issue/task runs, shows the persisted
GitHub issue title and state when available, plus latest derived run status,
branch, and linked Agentic worktree. New runs persist bounded issue metadata at
startup; older logs without that metadata show `Title unavailable` and
`UNKNOWN` rather than contacting GitHub from the read-only dashboard.

Logs remain expandable and have Copy and Download actions. The dashboard is
read-only: these actions only copy or download already-rendered text and do
not modify a worktree, run record, or pull request. A `file://` dashboard
cannot poll local files, and a generic static HTTP server serves the same
snapshot; use `--serve` for the regenerating endpoint. The dashboard shows the
recorded token total for each run and a visual token-usage chart with one
numbered bar per provider invocation when available. Token usage is ordered
newest to oldest by each invocation timestamp by default; the Token usage view
also provides an order control for switching to oldest first. Older records
without an invocation timestamp fall back to their matching operation timestamp
and then the run's last activity. Providers that do not report usage remain
marked as unknown rather than being estimated.

For scripts and external dashboards, use the read-only status command instead
of parsing terminal output:

```bash
agentic status --json
```

## Cleanup

Preview clean, no-longer-needed issue worktrees before removing them:

```bash
agentic cleanup --issue 119
agentic cleanup --issue 119 --yes
```

Cleanup skips worktrees with uncommitted changes and keeps run logs by default. Add `--remove-logs` only when the evidence is no longer needed. Use `--all` carefully to select every clean Agentic worktree.
