# Contributing

`main` is protected. All changes must go through a pull request; do not push directly to `main`.

## Pull requests

1. Create a branch such as `codex/<short-description>`.
2. Make focused changes and add or update tests.
3. Run `python -m unittest discover -s tests -q` locally.
4. Open a pull request against `main` and wait for the CI checks.
5. Merge only after the required checks and review pass.

The CI workflow tests Python 3.10 through 3.13 and builds the package. Configure those checks as required status checks in the repository branch protection settings.

## Releases

The package version is defined by `agentic.__version__` and exposed by:

```bash
agentic --version
```

For a release, update the version in a pull request, merge it, then create and push a semantic-version tag from the resulting `main` commit:

```bash
git tag v0.1.1
git push origin v0.1.1
```

The release workflow verifies that the tag matches the package version, builds the source distribution and wheel, and attaches them to a GitHub release.
