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
python -m py_compile agentic.py agentic_shell.py agentic_dashboard.py src/agentic_runner/*.py
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

CI tests Python 3.10 through 3.14 on Ubuntu, and runs a native
`windows-latest` job with Python 3.12. The Windows job compiles the runtime,
checks whitespace, runs the offline suite and the Windows-sensitive worktree,
process-tree, path, environment, and shell-input tests, and smoke-tests the
installed CLI. It does not use WSL, Docker, provider CLIs, credentials, or
GitHub write access. Static checks, coverage, Docker sandbox integration tests,
and package artifact builds remain on Linux. Configure those checks as required
status checks in repository branch protection.

The native Windows job validates Windows Python and Windows Git behavior only;
it does not emulate a WSL environment. When a worktree must be opened by both
WSL Git and Windows Git, use the shared-state and Git 2.48+ guidance in
[Getting started](getting-started.md#check-the-environment) and
[Troubleshooting](troubleshooting.md#wslwindows-git-reports-not-a-git-repository).

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the repository contribution policy.

## Releases

The package version is defined by `agentic_runner.__version__` and exposed by:

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
the current one. It updates `agentic_runner.__version__`, runs the compile
checks, creates `codex/release-vVERSION`, commits the change, pushes the branch,
and opens a normal GitHub pull request using the `gh` CLI. Authenticate first
with `gh auth login`. Use `--dry-run` to validate the starting branch and run
checks without making changes.

After the version PR is merged, the preferred release path is GitHub Actions:

1. Open **Actions → Release → Run workflow** and select `main`.
2. Start the run and approve the protected `release` environment if required.
3. Confirm the workflow succeeds and inspect the GitHub Release and
   `release-artifacts` in the run summary.

The workflow reads `agentic_runner.__version__`, validates the supported
`MAJOR.MINOR.PATCH` form, runs the compile, offline test, and build checks, and
refuses an existing tag or release. If the UI path is unavailable, update local
`main` and use the local tag helper as a fallback:

```bash
python scripts/release.py --dry-run
python scripts/release.py
```

The tag helper fetches `origin/main`, requires a clean and synchronized local
`main`, runs the compile checks, verifies that the generated tag is
unused, and asks for confirmation before creating and pushing it. The
current package version is read from `agentic_runner.__version__`, so version `1.0.1`
produces tag `v1.0.1`.

The release workflow verifies that the tag matches the package version, builds
the source distribution and wheel, and creates a GitHub release. It does not
publish to PyPI. If pushing the tag fails after local tag creation, rerun the
printed `git push origin <tag>` command after fixing the remote issue.
