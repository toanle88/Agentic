# ADR 0005: Fail-closed reviewer staging and durable artifact bounds

- Status: accepted
- Date: 2026-10-24

## Context

OpenRouter interpreter commands and native read-only provider staging both
consume untrusted model or worktree input. Durable review, investigation,
approval, result, and provider artifacts can also outlive the process that
wrote them and may be malformed or unexpectedly large.

## Decision

- OpenRouter interpreter evaluation flags are rejected in both separate and
  attached forms. Module execution is rejected unless it is an exact
  configured check, with the compatibility-safe `python -m unittest` module
  retained. Script-file execution remains available to the existing developer
  command runner; it does not make evaluation flags or arbitrary modules safe.
- Native reviewer staging treats protected-file filtering and non-regular
  entry filtering as intentional, but raises on traversal, opening, or copy
  failures. A partial staged tree is never passed to a reviewer.
- Complete JSON artifacts are read only up to a 256,000-byte budget. Bounded
  tail reads are used for text evidence, provider streams are capped at
  2,000,000 bytes, provider messages at 50,000 characters, test reports at
  50,000 characters, and investigation records at bounded field/count limits.

## Compatibility

Existing artifacts remain readable when they fit the relevant limit. Oversized
approval state fails closed, while oversized optional blocked, dashboard, or
cached investigation state is ignored. Protected files and symlinks continue
to be filtered rather than treated as staging failures.

## Consequences

Configured checks remain the explicit escape hatch for repository test
commands that use an interpreter. A changed or unreadable file during native
review now fails the review invocation instead of permitting an incomplete
view. Very large legacy artifacts may no longer be parsed; their complete
replacement is the responsibility of a later workflow operation.
