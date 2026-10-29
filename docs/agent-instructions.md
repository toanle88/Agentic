# Agent instructions

`AGENTS.md` is the target repository's shared instruction file for coding agents. It gives the developer context that is difficult to infer reliably from source code alone: project conventions, required checks, architectural boundaries, and safety rules.

Agentic provides a copy-ready starting point in [`AGENTS.example.md`](https://github.com/toanle88/Agentic/blob/main/AGENTS.example.md).

## Create the file

From an Agentic checkout, copy the template to the target repository:

```bash
cp AGENTS.example.md /path/to/target-repository/AGENTS.md
```

Then customize it with project-specific information. A useful file is concise, current, and actionable.

## What to include

Good repository instructions usually cover:

- supported runtime and package-manager versions;
- important directories and ownership boundaries;
- the normal fast test command;
- slower integration or release checks and when to run them;
- formatting, linting, type-checking, and generated-file rules;
- database migration and API contract expectations;
- security, privacy, and credential boundaries; and
- the expected handoff format for changes and verification.

Prefer commands that work from the repository root. Explain prerequisites for commands that require Docker, databases, browser binaries, or external services.

## What not to include

Do not put these in `AGENTS.md`:

- passwords, API keys, tokens, certificates, or connection strings;
- personal machine paths or user-specific preferences;
- instructions to bypass tests, security controls, or review;
- temporary issue-specific notes that will become stale; or
- large copied documentation that belongs in `docs/`.

Keep personal configuration in an ignored local file. Commit `AGENTS.md` only when its guidance is intended for the whole team; otherwise commit a safe example and document how developers create their local file.

## How Agentic uses it

Before each provider turn, Agentic discovers `AGENTS.md`, the standardized
development and architecture documents, compatible legacy development guidance,
and Markdown ADRs under `docs/decisions/`. It adds bounded copies to the
developer, investigator, and reviewer context inside explicit untrusted-data
delimiters. The source documents remain in the worktree for direct inspection.

Each loaded file is recorded in the run metadata by relative path, role, phase,
byte size, and SHA-256 digest. Guidance contents are not copied into
`run.json`, and a changed file is reread on the next provider turn. Missing,
symlinked, escaped, and non-regular guidance paths are ignored, so existing
repositories without the standard documents continue to work unchanged.

Agentic treats `AGENTS.md` as protected and will reject a patch that tries to
modify it as part of normal issue work.

Instructions guide the agent but do not replace tests, CI, code review, or human judgment. If the file conflicts with the issue's explicit requirements or higher-priority system policy, the higher-priority requirement wins.
