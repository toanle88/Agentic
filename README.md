# Agentic v3.2 — live-progress, no-label, resumable GitHub coding runner

**Place `agentic.py` and `agent_config.json` in the root of your Git repository.** It detects that project's Git root from the script location. A Codex CLI login via your existing ChatGPT subscription works; you do **not** need to supply an OpenAI API key.

### Requirements

- Python 3.10+, Git, GitHub CLI (`gh`), Codex CLI (`codex`). This runner has no third-party Python dependencies.
- `gh auth login` and `codex login` completed.
- An `origin` remote targeting a GitHub repo; the default branch must be available on `origin`.
- Issue is **open** (GitHub issue status, not a label). There are **no required or excluded labels**. The task list is limited to the first 100 open issues returned by `gh`.
- Appropriate test tools and project dependencies installed for a **separate Git worktree**. For example, use a shared project-independent virtual environment or a local dependency manager (`uv run`) configured to install/respect that worktree. Avoid an editable install that imports your original checkout.
- Start in a trusted/disposable repository without production credentials. Worktrees isolate Git changes but **do not sandbox arbitrary execution**.

### Usage

```bash
python agentic.py --list           # list open issues, oldest first, with [RESUME] markers
python agentic.py --runs           # show existing agent worktrees (no GitHub request)
python agentic.py                  # choose an open issue interactively
python agentic.py --issue 42       # create worktree or RESUME prior worktree for #42
python agentic.py --issue 42 --review  # rerun tests and independent read-only review
python agentic.py --issue 42 --publish # rerun checks and request approval for draft PR
python agentic.py --issue 42 --new # explicitly start a SECOND worktree for #42
```

> `--publish` is **not** a development command. It only accepts an **existing** worktree, runs fresh validation and a fresh review, and asks you to type `publish`. It never merges or marks the PR ready. If publication fails after the commit/push, rerun `--publish`: it can detect the committed branch patch and retry PR creation. If a PR already exists for the branch, it prints its link rather than creating a duplicate.

### Live progress, heartbeat, and logging (new in v3.2)

Codex output is no longer hidden until completion. The runner uses `codex exec --json`,
reads structured JSONL events as they arrive, and renders readable progress messages.
Review runs use a separate read-only Codex process and stream their own status.

Example (illustrative, not an actual run against your repository):

```text
[RESUME] Using existing worktree: .../.Tally-agent-worktrees/issue-119-5802a6ff
[DEVELOPER] Working on issue #119, attempt 1
[07:50:02] [DEVELOPER] Started (PID 12345); progress log: .../developer-002.log
[07:50:05] [DEVELOPER] Codex session started
[07:50:11] [DEVELOPER] Running command: ls src
[07:50:13] [DEVELOPER] Command finished (exit 0)
[07:50:28] [DEVELOPER] Still running (26s elapsed)...
[07:51:02] [DEVELOPER] Modified files: src/billing/invoices.py
[07:51:20] [DEVELOPER] Finished in 78s (exit 0)
[TESTS] Running: python -m pytest -q
[TESTS] PASSED
[REVIEW] Launching independent read-only Codex process
[REVIEW] { "approved": true, ... }
```

Each agent session writes two logs under `../.<project>-agent-runs/<branch>/`:

- `developer-001.log` / `review-001.log`: human-readable progress, ideal for `tail -f`.
- `developer-001.jsonl` / `review-001.jsonl`: raw Codex event stream for debugging.
- `developer-001.txt` / `review-001.json`: Codex final message / structured verdict.
- `test-command-001.txt`, `tests-001.txt`: test process output and verification summary.

New invocations automatically increment the log number. Test commands print their
output live. When an agent is quiet, a **15-second heartbeat** confirms that the
subprocess has not terminated; a heartbeat does *not* prove forward progress.
Codex defaults to a 20-minute per-invocation timeout, and each test command
defaults to a 6-minute timeout. These limits are configurable and intentionally
independent of retries.

**Upgrading while an old run is active:** stop the old invocation normally
(e.g. Ctrl+C), ensure it has exited, replace `agentic.py` with the updated script,
and run `python agentic.py --issue 119` again. It reuses the existing worktree
and refreshes `origin/<base>` before validation. A per-repository lock rejects
simultaneous mutating runs.

### Workflow (no stages to track)

1. Load open GitHub issues with no label requirements; list them by issue number (oldest first). Select an issue.
2. Discover an existing worktree from `git worktree list --porcelain`, or create a new worktree based on the remote default branch. **No run-state JSON, DB or checkpoint stage machine is required.**
3. The developer runs in a Codex process with `workspace-write`. Its prompt explicitly says to inspect `git status`, both staged/unstaged diffs, recent commits, source and tests, then decide what remains.
4. Python runs configured test commands. It stages the *proposed Git diff* (ordinary Git staging, **not** a workflow stage), limits changed files and prevents sensitive/config paths from being included.
5. A **new** Codex process runs with `--sandbox read-only`, independently inspecting the proposed change and returning a structured JSON review.
6. If tests or review fail, Python supplies the observed feedback to the developer on another run. On error or after bounded retries, the worktree stays in place so the next `--issue 42` can resume.
7. `--publish` always runs fresh tests and review. A SHA-256 fingerprint of the complete proposed patch must remain the same between verification and explicit approval. A draft PR is created only after you type `publish`.

### Project files

```text
my-project/
├── .git/
├── src/
├── tests/
├── AGENTS.md           # commit this project-specific guidance (optional)
├── agentic.py          # the orchestration script
└── agent_config.json   # test commands and bounded retry limits
```

Worktrees and logs are created **next to** the root checkout:

```text
../.my-project-agent-worktrees/issue-42-XXXXXXXX/
../.my-project-agent-runs/agent-issue-42-XXXXXXXX/
```

Those directories are deliberately outside the main checkout. Logs are for troubleshooting only; task progress comes from inspecting the repository and Git. If several worktrees exist for one issue, you are prompted which one to resume.

### Configuration

```json
{
  "test_commands": [["python", "-m", "pytest", "-q"]],
  "max_retries": 2,
  "max_files": 30,
  "codex_model": null,
  "codex_reasoning_effort": null,
  "codex_timeout_seconds": 1200,
  "test_timeout_seconds": 360
}
```

Test commands are argv arrays, not shell strings. They run in the worktree. Change them for your project, for example:

```json
{
  "test_commands": [
    ["uv", "run", "--no-sync", "pytest", "-q"],
    ["uv", "run", "--no-sync", "ruff", "check", "."]
  ],
  "max_retries": 2,
  "max_files": 30,
  "codex_model": null,
  "codex_reasoning_effort": null,
  "codex_timeout_seconds": 1200,
  "test_timeout_seconds": 360
}
```

These `uv` examples require packages already installed/available in that worktree environment; adjust for your actual project setup.

Leave `codex_model` and `codex_reasoning_effort` as `null` to inherit the
Codex CLI configuration already selected for your account, such as a
configured Luna model and Max reasoning effort. Set them explicitly when a
repository needs reproducible model settings. Timeout values are in seconds.

### No-label behavior

The only issue-selection filter is GitHub's built-in **Open** status. You can select any open issue, with no extra setup or labels. The script does not request label metadata, does not score issues from labels, and does not block tasks because they carry particular labels. It presents up to 100 open issues by issue number (oldest first); use `--issue N` to choose an issue directly.

The normal checks still apply before publishing: deterministic tests, an independent code review, change-file limits, and your explicit confirmation. A closed issue cannot start a new run or be published.

### What separate review means

Developer invocation (can modify workspace, within Codex permission settings):

```bash
codex exec --sandbox workspace-write --output-last-message /path/out.txt "Implement issue..."
```

Reviewer invocation (fresh session with read-only permissions):

```bash
codex exec --sandbox read-only --output-schema /path/review-schema.json \
  --output-last-message /path/review-001.json "Review the proposed PR diff..."
```

The two calls share the worktree files but **not the developer conversation**. They use the same subscription login and usage allowance. The reviewer receives the full issue description and test report as untrusted context. A review is helpful but cannot prove a patch is bug-free.

### Important boundaries and trade-offs

- An AI agent can infer much of the engineering state from Git but **cannot infer trustworthy facts such as whether tests really passed or a human approved publication**. Python must enforce those rules. Without label gates, be deliberate about which issue you choose.
- Project root's uncommitted changes do **not** automatically appear in the remote-based worktree. Commit/push relevant project instructions (`AGENTS.md`) and dependencies to the target base branch before starting an issue. The script files themselves can remain untracked locally in the *runner* checkout.
- `AGENTS.md`, `agentic.py`, `agent_config.json`, `.env*`, private keys, `.gitmodules`, `.codex/`, and `.github/workflows/` are blocked from automated proposed patches. Inspect and apply such changes manually if needed.
- The runner runs unit tests **outside the Codex sandbox** under the current operating-system account; a malicious test can access that account's secrets. For untrusted issues or contributors, use a locked-down container or disposable VM with minimal credentials. Even a read-only agent is not a complete security boundary.
- `codex exec` options may evolve; check `codex exec --help` for your installed version. Windows users may prefer WSL. Codex availability and subscription limits depend on your plan.
- This is a supervised single-user prototype, not a queue/multi-worker service. A per-repository lock prevents concurrent mutating runner processes, but separate worktrees for the same issue still require deliberate coordination. PR creation isn't the same as CI passing. CI and human code review are still required.
- The runner **does not** automatically delete worktrees. Manage them with `git worktree list` and, when truly finished, `git worktree remove /path/to/worktree` after reviewing/preserving any changes.

### WSL and Windows Git

Git worktree metadata can contain absolute paths. If the runner creates a worktree with WSL Git and it is later opened with Windows Git, a Linux path such as `/mnt/d/...` may not be recognized by Windows Git. The runner checks the WSL Git and, when running under WSL, the available `git.exe`; when both are Git 2.48 or newer, it enables relative worktree paths for new worktrees and repairs the selected worktree when resuming it. If either installation is older or unavailable, it keeps the compatible absolute-path behavior.

To repair an existing worktree manually after upgrading both Git installations, run this from the main repository in WSL:

```bash
git config worktree.useRelativePaths true
git worktree repair --relative-paths /mnt/d/Dev/Projects/.Tally-agent-worktrees/issue-119-5802a6ff
```

### Verify the runner offline

```bash
python -m unittest discover -s tests -v
```

The included tests use temporary Git repositories, test worktree discovery and patch hashing, and mock Codex/GitHub calls where necessary. They don't authenticate or run Codex for real.
