# ADR 0004: Bounded run metadata

- Status: accepted
- Date: 2026-10-23

## Context

Repeated resumes append provider invocations, operation timings, guidance
inventories, and security-policy history to `run.json`. Without retention and
read limits, a long-lived run or a legacy artifact can consume avoidable
memory and disk space when the dashboard or a later resume loads it.

## Decision

The telemetry write boundary retains only the newest bounded history entries
and enforces a 256,000-byte serialized `run.json` budget. Readers refuse to
parse older records above that byte limit and return the normal empty usage
aggregate; the next write creates a bounded record. Aggregate usage and
workflow fields remain compatible, while oversized unknown legacy fields are
discarded during compaction.

## Consequences

Dashboards and resumes remain bounded even when reading older artifacts. The
newest retained history is available for normal records, but very old entries
may no longer appear after compaction. An oversized legacy record is treated
as untrusted evidence rather than parsed in full, and its required state must
be recreated by a subsequent workflow operation.
