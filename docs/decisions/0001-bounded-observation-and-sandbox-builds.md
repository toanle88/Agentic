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
- OpenRouter file reads have a fixed character budget and ranged reads consume
  fixed-size chunks. `list_files` returns at most 300 files per page and adds a
  continuation marker when another page exists. Callers request the next page
  with the 1-based `page` argument. Protected paths remain filtered on every
  page.
- Configured sandbox image builds use the engine's default network by default
  so dependency installation remains functional. `sandbox.build_network: false`
  explicitly passes `--network=none`. This is independent of runtime
  `sandbox.allow_network`; build operations remain a trusted-host/daemon
  boundary and do not receive configured pass-through environment variables.

## Alternatives considered

- Returning an unbounded tree or file contents was rejected because provider
  context and process memory are finite and attacker-controlled repository
  content must not cause avoidable exhaustion.
- Silently truncating `list_files` was rejected because reviewers could miss
  files beyond the first page. A cursor was considered, but a stable page
  number over deterministic sorted traversal keeps the tool schema small and
  preserves the existing newline-delimited output format.
- Disabling build networking unconditionally was rejected because ordinary
  Dockerfiles that install dependencies would fail. Runtime network policy is
  not weakened by the build default.

## Consequences

Existing callers that omit `page` retain the first-page behavior and output
format for trees with 300 or fewer readable files. Larger trees now include a
visible continuation marker, and callers can retrieve all subsequent pages
without changing the path or protected-file policy. Dashboard and file-read
limits can omit old content from a single response, so targeted range or tail
requests remain the compatibility path for detailed inspection. The cache is
an in-process optimization only; durable run records and Git worktrees remain
the source of truth.
