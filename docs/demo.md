# End-to-end demonstration

Agentic’s primary workflow is the controlled path from a development request
to a reviewable draft pull request:

```text
issue or local task
    -> read-only investigation
    -> isolated worktree
    -> developer attempt
    -> configured checks
    -> independent read-only review
    -> resume after rejected work
    -> fresh validation and human-approved draft PR
```

## Reproduce the workflow safely

Use a disposable repository and a provider-ready configuration. For untrusted
issue text or source code, initialize an isolated profile with a project-owned
container image:

```bash
agentic init --provider codex \
  --security-mode isolated \
  --sandbox-image your-project-agentic-runner:latest
agentic doctor
```

Then run a local task so the demonstration does not require GitHub issue
access:

```bash
agentic --task-file task.md
agentic status --json
agentic dashboard
agentic --task-file task.md --review
agentic --task-file task.md --publish
```

The developer never publishes. A rejected review leaves the worktree and
evidence in the Agentic state directory; rerunning the same task resumes from
that state. `--publish` performs fresh checks and review, then requires the
human to type `publish` before committing, pushing, and opening a draft PR.

For a deterministic provider-free check of the benchmark corpus, use:

```bash
python scripts/agent_benchmark.py benchmarks/agent-quality.example.json \
  --validate-only
```

Live benchmark adapters require explicit provider commands and credentials;
they should be run only against disposable fixtures and with network and
sandbox policy chosen deliberately.
