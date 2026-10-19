# Workflow

## Run lifecycle

Each issue or local-task run follows this sequence:

1. Resolve the repository's base branch; GitHub mode also loads the GitHub issue.
2. Create or reuse an isolated worktree for the issue or deterministic local task.
3. Ask an independent read-only configured investigator to analyze the issue and repository.
4. Record the investigation feedback and pass it to the developer automatically.
5. Give the task and repository instructions to the configured developer provider.
6. Run every configured test command.
7. Stage and inspect the proposed patch.
8. Ask an independent read-only configured reviewer for a structured verdict.
9. Approve, retry with evidence, or preserve the worktree for resume.

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
     [--review|--publish|--mark-done|--new]
     [--reason "why"] with --mark-done
/run [--profile fast|slow]             Local mode: paste task, finish with /done
     [--review|--publish|--mark-done|--new]
/ps [--all]                            Show current or persisted job status
/logs JOB|PID [LINES]                  Follow a running job or show recent output
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
`/logs` and `/stop` are completed dynamically. On interactive terminals, Agentic
renders the dropdown with live filtering and command descriptions using standard ANSI
escapes with zero third-party dependencies.

`/run` starts a child process and returns immediately. `/ps` shows jobs started in the current shell; `/ps --all` also discovers job records from previous shell sessions. The child runner's durable logs remain available after the shell exits, and `/logs JOB` works for persisted jobs too. Use `/logs JOB` or `/logs PID` on a running job to follow new output until it exits; press Ctrl-C to stop following without stopping the job. Add `LINES` to control how much existing output is shown first. Running jobs are listed before completed jobs so active work is immediately visible. Use `/stop JOB` to stop one job or `/stop --all` to request termination for every running Agentic job visible to the shell, including jobs from previous shell sessions.

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

GitHub issue mode is the default. If `task_source` is `local`, use `/run`
without an issue number, paste the requirements, and finish with `/done`.
Pasting the same task again produces the same task identity and resumes its
existing worktree. Local tasks can still use `/run --publish` to create a
GitHub draft pull request.

## Resume and retries

Run the same issue again to resume its existing worktree:

```bash
agentic --issue 119
```

Use `--profile slow` when the issue needs the configured integration, browser, or release checks:

```bash
agentic --issue 119 --profile slow
```

An optional per-run instruction can be supplied when starting or resuming an
issue. It is stored in the run metadata and passed to the developer and
read-only reviewer as bounded, explicitly delimited untrusted data:

```bash
agentic --issue 119 --instruction "Bump the version when the implementation is complete"
```

If a resume omits `--instruction`, Agentic reuses the stored value. Supplying
it again replaces the stored value; an empty value clears it. The interactive
shell accepts the same option, for example `/run 119 --instruction "..."`.

Agentic preserves code, commits, test output, reviewer output, and structured evidence after failures, timeouts, rejected reviews, and exhausted retries. On resume, the latest failed test and review evidence is supplied to the developer as untrusted feedback, regardless of provider.

If the same rejected review is returned without a changed proposed patch,
Agentic marks the run `blocked`, preserves the worktree and evidence, and skips
future developer attempts for that unchanged patch. A later resume is allowed
only after the proposed patch changes. A blocked run is not approved and cannot
be published.

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
that the current patch still matches the recorded approval, asks for the exact
`publish` confirmation, and then creates the draft PR.

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
approval, asks for the exact `publish` confirmation, and opens only a draft PR.
It does not run tests or an automated review. If the code or issue changes,
rerun development or `--review` first; for a manual completion, mark it done
again with a new reason.

In the interactive shell, `/run ... --publish` runs in the foreground so the
shell can receive the exact publication confirmation. Other `/run` operations
remain background jobs. If no valid approval is recorded, publication stops
and asks you to run development or `--review` first.

For a local task, use `agentic --task-file task.md --publish` or resume from
the saved task file created by the interactive shell.

Agentic verifies that the patch still matches its recorded approval and asks
for the exact confirmation word `publish`. It does not run tests or review
during publication. Only after confirmation does it commit, push the issue
branch, and open a draft pull request. The draft state leaves a final human
review step before merging.

When Agentic is run from a checkout of its own repository, changes to its
`agentic.py` runner are allowed through patch validation so Agentic can develop
itself. The protected-file policy remains active for every other repository and
still blocks `agent_config.json`, `AGENTS.md`, credentials, and other sensitive
paths.

## Logs and dashboard

Durable artifacts are stored in the centralized Agentic state directory:

```text
~/.local/state/agentic/my-project-<identity>/worktrees/issue-119-XXXXXXXX/
~/.local/state/agentic/my-project-<identity>/runs/agent-issue-119-XXXXXXXX/
~/.local/state/agentic/my-project-<identity>/shell/job-001.log
```

Run logs include developer and reviewer progress, provider event traces, test output, structured review results, and resume evidence. They are outside the worktree so they cannot accidentally become part of the patch.

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

In the interactive shell, `/dashboard` generates the snapshot and opens it with
the host operating system's default browser. Use `/dashboard PATH` to choose a
specific output file. Regenerate the dashboard after a run to refresh its
contents. The dashboard shows the recorded token total for each run and a
visual token-usage chart with one numbered bar per provider invocation when
available. Runs are ordered oldest to newest, and the highest number is marked
as latest. This keeps retries and separate developer/reviewer executions
visible instead of collapsing them into one issue bar. The run detail table
uses the same numbering and latest marker. Providers that do not report usage
remain marked as unknown rather than being estimated.

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
