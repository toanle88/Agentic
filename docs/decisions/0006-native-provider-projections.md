# ADR 0006: Native provider worktree projections

- Status: accepted
- Date: 2026-10-26

## Context

Native provider CLIs do not share Agentic's protected-path policy. Giving a
developer provider the real worktree allows it to read or write credentials,
Git metadata, Agentic configuration, instructions, and protected workflows.
Provider-specific permission flags are not equivalent across Codex, Gemini,
Claude, and Copilot.

## Decision

Every native provider turn runs from a disposable projection. The projection
contains only bounded, regular, single-link files that do not match the shared
`forbidden` policy. Developer projections are writable; read-only projections
are mode-restricted. After a developer turn, only changed safe files are
synchronized back. The source file's original identity, mode, and digest must
still match before an update or deletion, and writes revalidate the digest on
the anchored descriptor immediately before truncating it. Synchronization also
applies provider mode-only changes and enforces the same cumulative file and
byte bounds as initial staging. Protected paths and provider-created links or
special files are never synchronized.

Native provider subprocess environments are also built from a sanitized copy
of the host environment. Only the selected credential is restored under its
configured name and the provider's canonical name.

## Consequences

Developers can still create, edit, and delete ordinary source files, while
protected files remain outside the provider's filesystem view. Git operations
inside a native provider projection do not have the checkout metadata; Agentic
retains ownership of Git validation and publication. A concurrent source
change causes synchronization to fail closed rather than overwrite it.
