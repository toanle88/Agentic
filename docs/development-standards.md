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
- Keep the canonical CLI in `src/agentic_runner/cli.py`; keep the three root
  command files as thin compatibility wrappers and put cohesive boundaries in
  focused package modules.
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
- Reviewers remain read-only, and publication requires recorded approval; automatic publication is available only through the explicit opt-in configuration setting.

## Migrations and contracts

- Preserve backward compatibility for existing run metadata and target
  repositories unless a migration is explicitly required.
- When changing a durable schema or CLI contract, document the compatibility
  behavior and add coverage for old records and absent optional files.
- Keep architecture decisions in a numbered or dated ADR under `decisions/`.

## Durable artifact redaction and recovery

All durable run, review, telemetry, and resume text written through
`agentic_persistence` is redacted at the final write boundary. Provider
adapters also redact streamed raw traces and command output before appending
them, including the selected credential value when its configured environment
name is non-semantic. Codex final-message output is staged in a same-directory
temporary file until it has been redacted and atomically replaced. This is
bounded protection for common bearer, key/value, JSON, multiline, and
configured-environment credential forms; it is not arbitrary secret discovery.

Structured provider JSONL logs are additionally capped at 2,000,000 bytes and
their in-memory line queue is bounded; dropped or oversized output receives an
explicit `agentic_stream_truncated` marker. Test command progress and reports
describe configured argv values without persisting literal arguments, so
credentials belong in environment variables rather than test argv.
Dashboard review-history parsing reads at most 256,000 bytes, 50 entries, and
100 findings per entry, with finding text capped at 4,000 characters. Older
complete histories remain readable within those limits.

JSON and complete text state uses same-directory temporary files, `fsync`, and
replacement. If a process stops before replacement, the previous complete
artifact remains authoritative; if there is no previous artifact, readers
treat an empty, truncated, or invalid file as absent. Approval records fail
closed with an explicit unreadable/invalid error. Append-only provider and
test streaming logs may end mid-record after interruption; readers show only
bounded, redacted evidence and treat malformed structured output as invalid,
without using it as approval or publication state.

## Review and handoff

- Review the complete proposed diff, not only files changed in the current
  invocation.
- Keep findings actionable and tied to the requested behavior.
- Report changed files, checks actually run, skipped checks with reasons, and
  unresolved risks or blockers.
