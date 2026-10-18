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
     [--review|--publish|--new]
/run [--profile fast|slow]             Local mode: paste task, finish with /done
     [--review|--publish|--new]
/ps [--all]                            Show current or persisted job status
/logs JOB|PID [LINES]                  Follow a running job or show recent output
/stop JOB                              Stop a running job
/list                                  List open GitHub issues
/dashboard [OUTPUT]                    Generate and open the HTML dashboard
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

`/run` starts a child process and returns immediately. `/ps` shows jobs started in the current shell; `/ps --all` also discovers job records from previous shell sessions. The child runner's durable logs remain available after the shell exits, and `/logs JOB` works for persisted jobs too. Use `/logs JOB` or `/logs PID` on a running job to follow new output until it exits; press Ctrl-C to stop following without stopping the job. Add `LINES` to control how much existing output is shown first.

`/list` synchronously lists the target repository's open GitHub issues. It uses
the same GitHub CLI-backed issue listing as `agentic --list` and does not create
a background job.

The default `fast` profile uses `test_commands`. Use `/run 119 --profile slow` to run the fast checks followed by `slow_test_commands`.

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

Agentic preserves code, commits, test output, reviewer output, and structured evidence after failures, timeouts, rejected reviews, and exhausted retries. On resume, the latest failed test and review evidence is supplied to the developer as untrusted feedback, regardless of provider.

Use `--new` only when you intentionally want a second worktree:

```bash
agentic --issue 119 --new
```

`--new` does not continue the existing worktree's repair history.

## Review behavior

The reviewer is a separate read-only process. It sees the current staged patch, issue requirements, and test evidence, then returns an approval or rejection with findings. A rejection does not discard the worktree. The developer receives the findings on the next retry, up to the configured retry limit.

Repeated rejection usually indicates an unresolved requirement, missing integration coverage, or an issue that is too broad. Increasing retries does not lower the review standard.

## Publication

The developer does not commit, push, or open pull requests. After tests and review approve the current patch, publish explicitly:

```bash
agentic --issue 119 --publish
```

For a local task, use `agentic --task-file task.md --publish` or resume from
the saved task file created by the interactive shell.

Agentic revalidates the patch, checks that it did not change, and asks for the exact confirmation word `publish`. Only after confirmation does it commit, push the issue branch, and open a draft pull request. The draft state leaves a final human review step before merging.

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
contents.

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
