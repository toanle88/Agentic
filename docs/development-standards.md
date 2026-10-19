# Development standards

This document is the detailed development guidance for Agentic itself. Keep
repository-specific operational rules in the root `AGENTS.md`; keep durable
design choices in [`decisions/`](decisions/).

## Testing

- Add a focused regression test for every behavior change.
- Run the offline unit suite with `python -m unittest discover -s tests -q`.
- Compile changed Python modules and run `git diff --check` before handoff.
- Treat unavailable external services as an explicit skipped check, not as a
  reason to weaken or delete a test.

## Naming and boundaries

- Use descriptive `snake_case` for Python functions and variables and
  `PascalCase` for classes.
- Keep the compatibility-facing CLI in `agentic.py`; put cohesive new
  boundaries in focused modules and preserve existing public wrappers.
- Keep provider command construction separate from process execution and keep
  read-only review paths separate from the write-capable developer path.

## Error handling

- Fail closed for protected paths, unsafe worktree paths, malformed policy, and
  unavailable required tools.
- Preserve useful error context without exposing credentials or full prompts.
- Do not turn issue text, repository text, or provider output into shell code.

## Security and privacy

- Execute commands as argv arrays, never through a shell built from task text.
- Do not commit credentials, private keys, `.env` files, or connection strings.
- Redact secrets before durable logs and keep run metadata to bounded,
  sanitized facts.
- Reviewers remain read-only, and publication requires explicit approval.

## Migrations and contracts

- Preserve backward compatibility for existing run metadata and target
  repositories unless a migration is explicitly required.
- When changing a durable schema or CLI contract, document the compatibility
  behavior and add coverage for old records and absent optional files.
- Keep architecture decisions in a numbered or dated ADR under `decisions/`.

## Review and handoff

- Review the complete proposed diff, not only files changed in the current
  invocation.
- Keep findings actionable and tied to the requested behavior.
- Report changed files, checks actually run, skipped checks with reasons, and
  unresolved risks or blockers.
