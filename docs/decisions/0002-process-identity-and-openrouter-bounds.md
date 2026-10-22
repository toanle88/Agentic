# ADR 0002: Persisted process identity and OpenRouter bounds

- Status: accepted
- Date: 2026-10-22

## Context

Interactive-shell job records can outlive the process that created them. A
persisted PID alone is not sufficient to identify the original child because
operating systems may reuse PIDs. OpenRouter responses and provider events are
also external, model-controlled input and must not grow durable artifacts or
memory without a bound.

## Decision

Running job records include an optional `identity` object. On procfs systems it
contains the process start tick, a SHA-256 hash of `/proc/<pid>/cmdline`, and
the process-group ID. On Windows it contains the native process creation time.
Restoration and cancellation require the recorded identity to match the live
process; records from older versions or platforms without a verifiable
identity remain readable but are treated as finished and cannot be cancelled.

OpenRouter applies these independent limits:

- HTTP response bodies: 2,000,000 bytes;
- one raw JSONL event: 256,000 bytes;
- one raw JSONL artifact: 2,000,000 bytes;
- human-readable OpenRouter log: 200,000 bytes; and
- final provider message: 50,000 characters.

Oversized HTTP responses fail closed. Oversized events, artifacts, logs, and
final messages receive an explicit `agentic_stream_truncated` marker. Raw
provider data is redacted before it is written, and every raw artifact line
remains valid JSON.

The protected-path policy is case-insensitive and covers common credential
filenames, SSH key types, container credentials, and private-key extensions for
both API reads and publication checks.

## Consequences

Old job records remain visible in `/ps --all` and their logs remain available,
but an old running record must be restarted rather than signalled. OpenRouter
failures are bounded and diagnosable without allowing an external response or
provider payload to exhaust process memory or durable state.
