"""Shared read-only Git option classification."""
from __future__ import annotations


GIT_LOG_CONTENT_OPTIONS = frozenset({
    "-p", "-u", "--patch", "--full-diff", "-c", "--cc",
    "--patch-with-stat", "--patch-with-raw", "-L", "--follow",
    "--word-diff", "--word-diff-regex", "--color-words",
    "--stat", "--numstat", "--shortstat", "--dirstat", "--summary",
    "--raw", "--patch-with-raw", "--name-only", "--name-status",
    "--full-index", "--binary", "--check", "--diff-filter",
    "--diff-merges", "--no-diff-merges", "--find-renames", "--find-copies",
    "--irreversible-delete", "--ext-diff", "--textconv", "--output",
})

# Git adds diff presentation options over time.  OpenRouter therefore uses a
# positive metadata-only allowlist rather than relying on this content-option
# inventory to remain complete.
GIT_LOG_METADATA_OPTIONS = frozenset({
    "--abbrev-commit", "--all", "--author-date-order", "--boundary",
    "--date-order", "--decorate", "--dense", "--first-parent",
    "--full-history", "--graph", "--left-right", "--no-abbrev-commit",
    "--no-decorate", "--no-merges", "--no-walk", "--oneline",
    "--parents", "--reverse", "--show-pulls", "--sparse",
    "--simplify-by-decoration", "--simplify-merges", "--source",
    "--topo-order", "--use-mailmap", "--cherry", "--cherry-mark",
    "--cherry-pick", "--all-match", "--invert-grep",
    "--regexp-ignore-case", "--extended-regexp", "--fixed-strings",
    "--ignore-case", "--ignore-missing", "--reflog", "--single-worktree",
})
GIT_LOG_METADATA_PREFIXES = (
    "--author=", "--committer=", "--date=", "--decorate=", "--format=", "--grep=",
    "--max-count=", "--max-parents=", "--min-parents=", "--pretty=",
    "--skip=", "--since=", "--after=", "--until=", "--before=",
)


def git_log_option_exposes_history(part: str) -> bool:
    """Return whether a ``git log`` option may expose historical file data.

    Unknown options are unsafe by default.  Git's log mode shares a large
    diff-option surface, and rejecting new or unclassified options avoids
    turning a future diff-format option into a protected-history bypass.
    """
    if not part.startswith("-") or part == "--":
        return False
    option = part.split("=", 1)[0]
    if option in GIT_LOG_METADATA_OPTIONS or any(
        part.startswith(prefix) for prefix in GIT_LOG_METADATA_PREFIXES
    ):
        return False
    # Git accepts combined short options such as ``-pu`` and attached ``-L``
    # values.  The positive allowlist above means every other option is
    # rejected, including future diff-format forms.
    return True
