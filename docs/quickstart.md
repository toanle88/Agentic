# Five-minute local quickstart

This walkthrough uses a local task and a normal Git repository. It does not
require GitHub issue access, but it does require a configured provider.

## 1. Create a disposable repository

```bash
mkdir agentic-demo && cd agentic-demo
git init --initial-branch=main
printf 'def add(a, b):\n    return a + b\n' > calculator.py
mkdir tests
printf 'import unittest\nfrom calculator import add\n\nclass AddTests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\nif __name__ == "__main__":\n    unittest.main()\n' > tests/test_calculator.py
git add .
git -c user.name=Demo -c user.email=demo@example.test commit -m initial
git clone --bare . ../agentic-demo-origin.git
git remote add origin ../agentic-demo-origin.git
git fetch origin main
```

## 2. Initialize Agentic

```bash
agentic init --provider codex
agentic doctor
```

Review the generated `agent_config.json`. The default configuration uses
finite test, provider, and task timeouts. Replace the detected test command if
the repository needs additional checks.

## 3. Run a local task

Set `task_source` to `local` in `agent_config.json`, then run:

```bash
agentic --task "Add a subtract(a, b) function and a regression test"
```

Agentic creates a worktree, runs the untouched-base checks, invokes the
developer, runs the configured checks, and invokes an independent reviewer.
Rejected work is retained for resumption.

## 4. Inspect and resume

```bash
agentic status --json
agentic dashboard
agentic --task "Add a subtract(a, b) function and a regression test"
```

Use the same task text with `--review` when you want to rerun checks and review
without invoking the developer. Publication is always a separate,
human-confirmed step and requires a GitHub origin:

```bash
agentic --task "Add a subtract(a, b) function and a regression test" --review
agentic --task "Add a subtract(a, b) function and a regression test" --publish
```

For a fully deterministic CI demonstration, use a fake provider in tests
rather than live provider credentials. The live workflow should be treated as
an integration test, not as a security boundary.
