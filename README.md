# Agentic

Agentic runs a Codex developer and an independent read-only reviewer against GitHub issues. It uses Git worktrees so unfinished work can be resumed safely.

The runner itself has no third-party Python dependencies. It requires Python 3.10+, Git, GitHub CLI (`gh`), and Codex CLI (`codex`). Authenticate first:

```bash
gh auth login
codex login
```

The target repository must have an `origin` remote and a GitHub default branch.

`main` is protected. Changes to Agentic itself are made through pull requests; CI runs on every pull request and the `main` branch.

## Install in WSL

Install the CLI once with `pipx`; the target repositories no longer need copies of the Python files:

```bash
sudo apt update
sudo apt install pipx
pipx ensurepath
pipx install git+https://github.com/toanle88/Agentic.git
```

After a new Agentic release:

```bash
pipx upgrade agentic-runner
```

Remove it with:

```bash
pipx uninstall agentic-runner
```

For local development, use an editable install instead:

```bash
pipx install --editable /path/to/agentic
```

## Quick start

```bash
python /path/to/agentic.py \
  --repo /path/to/project \
  --issue 119
```

After installation, run it from the target repository:

```bash
cd /path/to/project
agentic --issue 119
```

The runner reads `agent_config.json` from the target repository by default. To use another file:

```bash
python /path/to/agentic.py \
  --repo /path/to/project \
  --config /path/to/agent_config.json \
  --issue 119
```

If the target configuration is missing, the runner stops before starting Codex. It never silently falls back to the Agentic repository's test configuration.

## Configuration

Create `agent_config.json` in the target project. Replace the example test command with the project's actual checks:

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
- `max_files`: maximum number of distinct files in the proposed patch. It is checked before review/publication; it does not limit lines or files the developer may inspect. Set it to `null` to disable this limit.
- `codex_model`: model override. `null` inherits your Codex CLI setting.
- `codex_reasoning_effort`: reasoning override such as `max`. `null` inherits your Codex CLI setting.
- `codex_timeout_seconds`: developer/reviewer limit. `null` means unlimited; a positive integer sets seconds.
- `test_timeout_seconds`: test-process limit. `null` means unlimited; a positive integer sets seconds.

Commands are checked before development starts. Missing executables and missing Python `-m` modules fail immediately instead of consuming retries. Clearly environmental failures such as missing dependencies, unavailable commands, port conflicts, and missing files are not retried.

## Commands

```bash
agentic --help                           # show all runner options
agentic --version                        # show the installed package version
agentic help                             # equivalent help command
agentic shell                            # open the background-job shell
agentic dashboard                        # generate the HTML dashboard
agentic --list                           # list open GitHub issues
agentic --runs                           # list existing worktrees
agentic --issue 119                      # create or resume issue 119
agentic --issue 119 --review              # rerun tests and review only
agentic --issue 119 --publish             # validate, then offer draft-PR publication
agentic --issue 119 --new                 # explicitly create a second worktree
```

The direct source form remains available when running without installation: replace `agentic` with `python agentic.py`. `agentic-shell` and `agentic-dashboard` are also installed as compatibility aliases.

Running the same `--issue` command again resumes the existing worktree. Changes and commits are preserved. Use `--new` only when you intentionally want another worktree.

When resuming, the runner loads the latest failed review and test evidence from the run logs and gives it to Codex as untrusted feedback. This lets a new invocation continue fixing the previous rejection.

The developer does not commit or push. `--publish` reruns tests and review, checks that the patch has not changed, and asks you to type `publish` before committing, pushing, and opening a draft PR.

The terminal view shows a compact run banner, colored status labels when output is interactive, the top review findings, and a final next-action summary. Full JSON/JSONL details remain in the run logs.

## Interactive shell

Use the optional supervisor when you want one terminal for multiple background jobs:

```bash
agentic shell --repo /path/to/project
```

It keeps the main prompt responsive while each runner executes in a separate child process. Jobs have no additional timeout; configured runner timeouts still apply.

```text
agentic> /run 119
agentic> /ps
agentic> /logs 1 80
agentic> /stop 1
agentic> /dashboard
agentic> /help
agentic> /quit
```

Interactive-shell output is saved next to the project in `.project-agentic-shell/`; the runner's normal durable logs remain in `.project-agent-runs/`.

## HTML run dashboard

Generate a searchable, self-contained status page from the run logs. It shows each issue's attempts, test result, review summary/findings, developer result, and expandable log contents:

```bash
agentic dashboard --repo /path/to/project
```

Use either standard help form:

```bash
agentic dashboard --help
agentic dashboard help
```

By default, the dashboard is written next to the project as `.project-agent-dashboard.html`. Choose another location when needed:

```bash
agentic dashboard \
  --repo /path/to/project \
  --output /path/to/agentic-dashboard.html
```

The generator is read-only and uses only the Python standard library. Regenerate the page after a run to refresh its snapshot.

## Development and releases

Run the local checks with:

```bash
python -m unittest discover -s tests -q
```

Release tags use semantic versioning, such as `v0.1.1`. The release workflow verifies the tag against `agentic --version`, builds the package artifacts, and creates a GitHub release. See [CONTRIBUTING.md](CONTRIBUTING.md) for the pull-request and release process.

## Worktrees and logs

For a project named `my-project`, worktrees and logs are created next to the checkout:

```text
../.my-project-agent-worktrees/issue-119-XXXXXXXX/
../.my-project-agent-runs/agent-issue-119-XXXXXXXX/
```

The worktree is preserved after failure, timeout, or exhausted retries. Logs contain developer/reviewer progress, test output, and the final review result.

Only one mutating runner process can use a repository at a time. A lock prevents concurrent runs from corrupting shared Git metadata.

## WSL and Windows Git

When both WSL Git and Windows Git are version 2.48 or newer, Agentic uses relative worktree paths for cross-platform compatibility. Older installations use the compatible fallback behavior.

Manual repair from WSL, after upgrading both Git installations:

```bash
git config worktree.useRelativePaths true
git worktree repair --relative-paths /path/to/.my-project-agent-worktrees/issue-119-XXXXXXXX
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
