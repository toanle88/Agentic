# ADR 0003: Audit publication and provider boundaries

- Status: accepted
- Date: 2026-10-22

## Context

Publication, sandbox setup, and OpenRouter tools consume repository paths and
provider responses that can contain symlinks, special files, unrelated Git
metadata, or provider-specific fields. These inputs must not expand the
publication or container boundary or grow provider request history without a
bound. The compatibility CLI also needs to expose the same durable state
behavior as the extracted workflow module.

## Decision

- Publication rejects every staged proposed entry whose new Git mode is not a
  regular blob mode (`100644` or `100755`). Deletions remain allowed. Symlinks,
  submodules, and other non-regular entries are rejected regardless of their
  names or targets; publication does not attempt to resolve them.
- Linked-worktree sandbox metadata is mounted only when the resolved
  `commondir` matches `git rev-parse --git-common-dir` for the selected root.
  A mismatch or unavailable authoritative check omits all Git metadata mounts.
- OpenRouter assistant history retains only the chat protocol fields needed by
  later turns: `role`, `content`, and normalized `tool_calls` containing
  `id`, `type`, and `function.name`/`function.arguments`. Unknown provider
  metadata is dropped before the existing character-based request budget is
  applied.
- OpenRouter `read_file` accepts only regular files. Symlink path components
  are rejected, and reads use a non-following, nonblocking descriptor followed
  by an `fstat` regular-file check so FIFOs and other special files cannot
  block the tool loop.
- `workflow_state.py` is the canonical implementation for compatibility state
  helpers. Existing blocked and developer log files remain readable; loading
  applies redaction, while new records use the shared bounded redaction path.
  Older records are not rewritten automatically.

## Consequences

Some repositories that intentionally publish symlinks or use symlink aliases
with OpenRouter file tools require a different explicit workflow outside these
boundaries. A linked-worktree pointer that cannot be verified is treated as
unsafe rather than mounted. Provider extensions are not available in later
OpenRouter turns, which keeps request history predictable and provider-neutral.
