# ADR 0001: Bounded observation and sandbox image builds

- Status: accepted
- Date: 2026-10-20

## Context

Agentic reads provider output, run artifacts, and repository trees that can be
much larger than a single provider context or dashboard response. Unbounded
reads and repeated snapshot construction increase memory use and make normal
large inputs unreliable. At the same time, a file listing that silently stops
at a fixed limit can make a review incomplete. Sandbox images may also need to
download dependencies during a configured Dockerfile build, while runtime
network access should remain separately controlled.

## Decision

Agentic uses bounded observation with explicit continuation:

- Dashboard snapshots read bounded log tails, index each run's numbered files
  once, and use a bounded metadata-keyed cache. Changed artifact metadata
  invalidates the affected snapshot; old run folders remain readable.
- OpenRouter file reads have a fixed character budget. Ranged reads consume
  fixed-size chunks and stop after at most 256,000 scanned characters or
  50,000 scanned lines, independently of the requested output range. An
  incomplete distant range receives a scan-limit marker. `list_files` returns
  at most 300 files per page, accepts page numbers from 1 through 100, and adds
  a continuation marker when another page exists. Calls beyond page 100 are
  rejected before repository traversal. Protected paths remain filtered on
  every page.
- Configured sandbox image builds use a bounded temporary context containing
  only safe regular files that pass the protected-path policy. Symlinks,
  special files, and hard-linked files cannot cross the context boundary.
  Build networking is disabled by default; `sandbox.build_network: true` is an
  explicit opt-in that passes the engine's default network. This is independent
  of runtime `sandbox.allow_network`; build operations do not receive
  configured pass-through environment variables.

## Alternatives considered

- Returning an unbounded tree or file contents was rejected because provider
  context and process memory are finite and attacker-controlled repository
  content must not cause avoidable exhaustion.
- Silently truncating `list_files` was rejected because reviewers could miss
  files beyond the first page. A cursor was considered, but a stable page
  number over deterministic sorted traversal keeps the tool schema small and
  preserves the existing newline-delimited output format.
- Passing the repository directly as context was rejected because a malicious
  Dockerfile could copy protected files during a networked build. Disabling
  build networking by default preserves a fail-closed boundary while allowing
  trusted repositories to opt in when dependency installation requires it.

## Consequences

Existing callers that omit `page` retain the first-page behavior and output
format for trees with 300 or fewer readable files. Larger trees now include a
visible continuation marker, and callers can retrieve subsequent pages through
page 100 without changing the path or protected-file policy. The page bound
prevents an untrusted page number from forcing unbounded lazy traversal;
repositories requiring more than 30,000 readable files in one directory tree
need a separate bounded listing mechanism. Dashboard and file-read limits can
omit old content from a single response, so targeted range or tail requests
remain the compatibility path for detailed inspection; distant ranges that
exhaust the scan budget return an explicit marker rather than scanning the
whole file. The cache is an in-process optimization only; durable run records
and Git worktrees remain the source of truth.
