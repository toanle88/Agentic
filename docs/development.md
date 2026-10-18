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

Compile all Python modules:

```bash
python -m py_compile agentic.py agentic_models.py agentic_security.py agentic_dashboard.py agentic_shell.py
```

Run the static checks used by CI:

```bash
python -m pip install -e '.[dev]'
ruff check agentic.py agentic_models.py agentic_security.py agentic_dashboard.py agentic_shell.py tests
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

CI tests Python 3.10 through 3.13 and builds the package artifacts. Configure those checks as required status checks in repository branch protection.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the repository contribution policy.

## Releases

The package version is defined by `agentic.__version__` and exposed by:

```bash
agentic --version
```

For a release, update the version in a pull request and merge it. Then run the
release helper from the repository checkout:

```bash
python scripts/release.py --dry-run
python scripts/release.py
```

The helper fetches `origin/main`, requires a clean and synchronized local
`main`, runs the offline test and compile checks, verifies that the generated
tag is unused, and asks for confirmation before creating and pushing it. The
current package version is read from `agentic.__version__`, so version `0.2.0`
produces tag `v0.2.0`.

The release workflow verifies that the tag matches the package version, builds
the source distribution and wheel, and creates a GitHub release. It does not
publish to PyPI. If pushing the tag fails after local tag creation, rerun the
printed `git push origin <tag>` command after fixing the remote issue.
