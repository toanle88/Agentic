# Agentic

Agentic runs a Codex developer and an independent read-only reviewer against GitHub issues. It uses Git worktrees so unfinished work can be resumed safely.

The runner itself has no third-party Python dependencies. It requires Python 3.10+, Git, GitHub CLI (`gh`), and Codex CLI (`codex`). Authenticate first:

```bash
gh auth login
codex login
```

The target repository must have an `origin` remote and a GitHub default branch.

## Quick start

The commands below use WSL paths. Windows `D:\Dev\Projects\Tally` is `/mnt/d/Dev/Projects/Tally` in WSL.

```bash
python /mnt/d/dev/projects/agentic/agentic.py \
  --repo /mnt/d/Dev/Projects/Tally \
  --issue 119
```

The runner reads `/mnt/d/Dev/Projects/Tally/agent_config.json` by default. To use another file:

```bash
python /mnt/d/dev/projects/agentic/agentic.py \
  --repo /mnt/d/Dev/Projects/Tally \
  --config /path/to/agent_config.json \
  --issue 119
```

If the target configuration is missing, the runner stops before starting Codex. It never silently falls back to the Agentic repository's test configuration.

## Configuration

Create `agent_config.json` in the target project. For Tally, use the project's actual Go checks:

```json
{
  "test_commands": [
    ["go", "test", "./..."]
  ],
  "max_retries": 3,
  "max_files": 30,
  "codex_model": null,
  "codex_reasoning_effort": null,
  "codex_timeout_seconds": null,
  "test_timeout_seconds": null
}
```

Settings:

- `test_commands`: required list of argument arrays. Commands run inside the worktree. Do not use shell strings.
- `max_retries`: extra developer attempts after the first. `3` means up to 4 total attempts.
- `max_files`: maximum number of distinct files in the proposed patch. It is checked before review/publication; it does not limit lines or files the developer may inspect.
- `codex_model`: model override. `null` inherits your Codex CLI setting.
- `codex_reasoning_effort`: reasoning override such as `max`. `null` inherits your Codex CLI setting.
- `codex_timeout_seconds`: developer/reviewer limit. `null` means unlimited; a positive integer sets seconds.
- `test_timeout_seconds`: test-process limit. `null` means unlimited; a positive integer sets seconds.

Commands are checked before development starts. Missing executables and missing Python `-m` modules fail immediately instead of consuming retries. Clearly environmental failures such as missing dependencies, unavailable commands, port conflicts, and missing files are not retried.

## Commands

```bash
python agentic.py --list                 # list open GitHub issues
python agentic.py --runs                 # list existing worktrees
python agentic.py --issue 119            # create or resume issue 119
python agentic.py --issue 119 --review   # rerun tests and review only
python agentic.py --issue 119 --publish  # validate, then offer draft-PR publication
python agentic.py --issue 119 --new      # explicitly create a second worktree
```

Running the same `--issue` command again resumes the existing worktree. Changes and commits are preserved. Use `--new` only when you intentionally want another worktree.

The developer does not commit or push. `--publish` reruns tests and review, checks that the patch has not changed, and asks you to type `publish` before committing, pushing, and opening a draft PR.

## Worktrees and logs

For a project named `Tally`, worktrees and logs are created next to the checkout:

```text
../.Tally-agent-worktrees/issue-119-XXXXXXXX/
../.Tally-agent-runs/agent-issue-119-XXXXXXXX/
```

The worktree is preserved after failure, timeout, or exhausted retries. Logs contain developer/reviewer progress, test output, and the final review result.

Only one mutating runner process can use a repository at a time. A lock prevents concurrent runs from corrupting shared Git metadata.

## WSL and Windows Git

When both WSL Git and Windows Git are version 2.48 or newer, Agentic uses relative worktree paths for cross-platform compatibility. Older installations use the compatible fallback behavior.

Manual repair from WSL, after upgrading both Git installations:

```bash
git config worktree.useRelativePaths true
git worktree repair --relative-paths /mnt/d/Dev/Projects/.Tally-agent-worktrees/issue-119-5802a6ff
```

## Safety boundaries

- Test commands run under the host account, outside the Codex sandbox. Use a trusted or disposable checkout without production credentials.
- The runner blocks changes to `agentic.py`, `agent_config.json`, `AGENTS.md`, secrets, private keys, `.codex/`, and GitHub workflows.
- GitHub issue text, repository files, and test output are treated as untrusted input to Codex.
- A reviewer is useful but cannot replace CI or human review.

## Test the runner itself

```bash
python -m unittest discover -s tests -q
```
