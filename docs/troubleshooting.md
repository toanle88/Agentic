# Troubleshooting

## `agentic: command not found`

Refresh the shell environment and verify the `pipx` installation:

```bash
pipx ensurepath
pipx list
command -v agentic
```

If the command still cannot be found, start a new WSL or terminal session.

## Missing `agent_config.json`

Run the initializer from the target repository root, review its detected command, or pass an explicit configuration path:

```bash
agentic init
agentic doctor
```

You can also pass an explicit configuration path:

```bash
agentic --repo /path/to/target-repository \
  --config /path/to/agent_config.json \
  --issue 119
```

Agentic stops instead of silently using a configuration from another repository.

## A test command fails before development starts

Run each configured command manually from the target repository. Install the target project's dependencies and update `test_commands` to match its actual toolchain. Agentic does not assume that Python or `pytest` is installed.

For frontend checks, install the repository's documented Node and package-manager dependencies. For Docker or PostgreSQL checks, confirm that the required service is running.

## Tests pass but review rejects the patch

Read the latest review JSON and test log in the run directory. A passing unit suite does not prove production wiring, persistence behavior, API contracts, UI behavior, or integration requirements. Add the missing implementation or verification, then resume the same issue.

If the same findings repeat, check whether:

- the issue contains multiple unrelated backend, persistence, and frontend slices;
- the configured checks omit a required test category;
- the developer is addressing symptoms instead of the production path; or
- an environment limitation is being reported as a code defect.

Agentic fingerprints review findings and reports repeated severities in the terminal and resume feedback. Treat a repeated finding as unresolved until the production path or reviewer evidence changes.

Do not use `--new` when you want the prior review evidence; use the same `--issue N` command.

## A run appears stuck

In the interactive shell:

```text
/ps
/ps --all
/logs 1 80
```

The shell has no additional supervisor timeout. Configured developer, reviewer, and test timeouts still apply. A `null` timeout means unlimited. Use `/stop JOB` only when you intentionally want to stop the child process.

For a direct foreground run, inspect the durable run log from another terminal. The current terminal is occupied by the foreground runner.

## WSL/Windows Git reports “not a git repository”

Git worktrees contain metadata paths. A worktree created with WSL Git may not be readable by Windows Git when absolute Linux paths are stored in its metadata.

Use WSL Git consistently, or upgrade both Git installations to 2.48 or newer so Agentic can use relative worktree paths. To repair an existing worktree from the main repository in WSL:

```bash
git config worktree.useRelativePaths true
git worktree repair --relative-paths /shared/agentic-state/my-project-<identity>/worktrees/issue-119-XXXXXXXX
```

For new runs that must be opened by both environments, keep the Agentic state on a shared mounted drive:

```bash
agentic --state-dir /path/to/shared-agentic-state --issue 119
```

## `--publish` refuses to publish

Publication requires current tests and review to approve the exact staged patch. Resolve any rejection, rerun:

```bash
agentic --issue 119 --review
```

Then retry `--publish`. If the patch changed after validation, Agentic refuses to publish and requires a fresh validation pass.

## Local task mode does not start

Confirm the target repository's ignored `agent_config.json` contains:

```json
{ "task_source": "local" }
```

Then start the interactive shell and use `/run` without an issue number. Paste
the task and finish with `/done`. The same task text produces the same task
identity, so repeating it resumes the existing worktree. Local tasks still need
an `origin` GitHub remote and `gh` authentication when you use `--publish`.
