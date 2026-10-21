# Contributing

`main` is protected. All changes must go through a pull request; do not push directly to `main`.

## Pull requests

1. Create a branch such as `codex/<short-description>`.
2. Make focused changes and add or update tests.
3. Run `python -m unittest discover -s tests -q` locally.
4. Open a pull request against `main` and wait for the CI checks.
5. Merge only after the required checks and review pass.

The CI workflow tests Python 3.10 through 3.14, runs static checks, coverage, Docker sandbox integration tests, and builds the package. Configure those checks as required status checks in the repository branch protection settings.

## Releases

The package version is defined by `agentic_runner.__version__` and exposed by:

```bash
agentic --version
```

For a release, prepare the version pull request from a clean, synchronized
`main` checkout:

```bash
python scripts/prepare_release.py
```

The helper prompts for a new semantic version, runs local checks, pushes a
release branch, and opens a normal pull request. After that pull request is
merged, use the preferred GitHub Actions path:

1. Open **Actions → Release → Run workflow** and select `main`.
2. Start the run and approve the protected `release` environment if required.
3. Confirm the workflow succeeds and inspect the GitHub Release and
   `release-artifacts` in the run summary.

The workflow derives the tag from `agentic_runner.__version__`, runs the shared
compile, offline test, and build checks, and refuses an existing tag or release.
If the UI path is unavailable, use the local tag helper from synchronized
`main` as a fallback:

```bash
python scripts/release.py --dry-run
python scripts/release.py
```

The helper verifies the clean, synchronized branch, runs the compile
checks, prevents reuse of an existing tag, and asks for confirmation before
creating and pushing it. The release workflow verifies that the tag matches the
package version, builds the source distribution and wheel, and attaches them to
a GitHub release.
