# Development

## Local setup

Clone the repository and install it in editable mode:

```bash
git clone https://github.com/toanle88/Agentic.git
cd Agentic
pipx install --editable .
```

Agentic has no third-party runtime dependencies. The package build uses `setuptools`.

## Tests and checks

Run the test suite:

```bash
python -m unittest discover -s tests -q
```

Compile all runtime Python modules:

```bash
python -m py_compile agentic.py agentic_chat.py agentic_config.py agentic_context.py agentic_diagnostics.py agentic_dashboard.py agentic_dashboard_data.py agentic_execution.py agentic_execution_guard.py agentic_execution_providers.py agentic_execution_stream.py agentic_guidance.py agentic_openrouter.py agentic_openrouter_loop.py agentic_openrouter_tools.py agentic_output.py agentic_policy.py agentic_prompts.py agentic_shell.py agentic_shell_commands.py agentic_shell_input.py agentic_models.py agentic_security.py agentic_telemetry.py agentic_sandbox.py agentic_providers.py agentic_review.py agentic_workflow.py agentic_worktrees.py
git diff --check
```

Run the static checks used by CI:

```bash
python -m pip install -e '.[dev]'
ruff check .
pyright
```

Build the source distribution and wheel:

```bash
python -m pip install --upgrade build
python -m build
```

## Pull requests

The `main` branch is protected. Changes must go through a pull request.

1. Create a focused branch, for example `codex/<short-description>`.
2. Make the smallest change that solves the problem.
3. Add or update tests for behavior changes.
4. Run the local test and compile checks.
5. Open a pull request against `main`.
6. Merge only after required CI checks and review pass.

CI tests Python 3.10 through 3.14, runs static checks, coverage, Docker sandbox integration tests, and builds the package artifacts. Configure those checks as required status checks in repository branch protection.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the repository contribution policy.

## Releases

The package version is defined by `agentic.__version__` and exposed by:

```bash
agentic --version
```

To prepare a release, start from a clean, synchronized `main` checkout and run:

```bash
python scripts/prepare_release.py
# Or provide the version directly:
python scripts/prepare_release.py 1.0.1
```

The preparation helper requires a new `MAJOR.MINOR.PATCH` version greater than
the current one. It updates `agentic.__version__`, runs the compile
checks, creates `codex/release-vVERSION`, commits the change, pushes the branch,
and opens a normal GitHub pull request using the `gh` CLI. Authenticate first
with `gh auth login`. Use `--dry-run` to validate the starting branch and run
checks without making changes.

After the version PR is merged, update local `main` and run the tag helper:

```bash
python scripts/release.py --dry-run
python scripts/release.py
```

The tag helper fetches `origin/main`, requires a clean and synchronized local
`main`, runs the compile checks, verifies that the generated tag is
unused, and asks for confirmation before creating and pushing it. The
current package version is read from `agentic.__version__`, so version `1.0.1`
produces tag `v1.0.1`.

The release workflow verifies that the tag matches the package version, builds
the source distribution and wheel, and creates a GitHub release. It does not
publish to PyPI. If pushing the tag fails after local tag creation, rerun the
printed `git push origin <tag>` command after fixing the remote issue.
