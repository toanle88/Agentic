# Security model

Agentic is a workflow orchestrator, not a host operating-system sandbox.
Issue descriptions, repository files, test output, provider responses, and
model-generated tool arguments are treated as untrusted input.

## Execution modes

Existing and newly initialized configurations default to
`security_mode: "trusted"` for compatibility with host-installed providers.
Trusted mode runs configured tests and native
provider CLIs under the account that launched Agentic. Git worktrees isolate
repository changes, but they do not isolate host files, credentials,
processes, or networking. Use trusted mode only for a trusted or disposable
checkout and do not expose production credentials.

Set `security_mode: "isolated"`, configure a project-specific image, and
enable the Docker/Podman sandbox for untrusted repositories or issue text.
Isolated runs fail closed when the configured container engine, image, or
explicitly configured credential environment is unavailable; they do not
silently fall back to host execution.

The developer is write-capable inside its assigned worktree, while Git
metadata is mounted read-only inside isolated containers. The reviewer is
read-only and uses restricted provider/tool policies. Agentic blocks commits,
pushes, publication, protected files, secret-like files, and GitHub workflow
changes during normal development. Trusted host execution cannot provide the
same OS-level Git or network enforcement.

Agentic applies one provider-independent permission policy before provider
execution. Developers may read/search, edit/create worktree files, inspect
Git, and run configured checks by default. Reviewers may read/search and use
read-only Git inspection only. Network-capable tool commands and dangerous
provider permissions are opt-in. Native provider flags are translations of
this policy, not a replacement for the optional container boundary. Native
developer turns receive exact allow rules for configured check argv and
read-only Git commands; native reviewer turns receive file-inspection tools
only, while Agentic captures the staged patch with its own argv-only Git
runner. Gemini's CLI receives command-scoped `ShellTool(git ...)` rules for
developer turns, while configured checks are still run by Agentic itself.
Gemini's native
network-capable shell tool is unrestricted, so `permission_policy.network=true`
is rejected rather than translated into a wildcard permission; a sandbox does
not make arbitrary shell commands equivalent to network-only access.
`run_checks: false` is available for edit-only trusted turns;
reviewers receive no native shell access. The `gh` command is
not included in native network allowlists, so GitHub publication remains an
Agentic approval boundary. OpenRouter uses the scoped Agentic
file and argv tools directly. Claude file tools are rooted at the assigned
worktree, and Claude/Copilot network access is limited to explicit
network-tool patterns when `network: true` is enabled; neither provider
receives a wildcard shell grant from that opt-in.

The granular `permission_policy` fields are `read_files`, `search_files`,
`edit_files`, `create_files`, `git_read`, `run_checks`, `network`, and
`allow_dangerous`. `allow_dangerous` is rejected outside isolated mode,
warned about prominently, and recorded in `run.json`. It is never permitted
for reviewers and does not authorize protected-path writes, commits, pushes,
or publication.

Docker/Podman sandboxing mounts the task worktree while overlaying its `.git`
entry and all required Git metadata read-only, and mounts the run-output
directory, uses a read-only
container root, drops Linux capabilities, limits resources, and disables
networking by default. Native providers and configured checks run inside it.
Credentials are passed only for environment names explicitly listed in
`sandbox.pass_env`.
OpenRouter's API request and scoped file inspection remain in the host Python
process, but OpenRouter command tools are also routed through the configured
sandbox. Copilot developer turns receive no native shell access because its
persisted approvals cannot be closed by exact shell allow rules; Agentic runs
configured checks and Git inspection with its own argv-only runner.

Read-only filesystem access and networking are separate controls. If
`sandbox.allow_network` is enabled, outbound networking is available to both
developer and reviewer processes, including a read-only reviewer that needs to
contact its native provider. This is a broad capability that can allow source
data to leave the container, so keep it disabled unless the repository,
provider, and credentials are trusted. `agentic doctor` reports when this
network policy is enabled.

## Credentials and logs

Credentials passed from the host must be supplied through environment
variables. Do not put API keys in `agent_config.json`, issue descriptions,
prompts, or repository files. A sandbox image may contain its own provider
authentication, but that image must be treated as sensitive and project-owned.
Agentic redacts known environment values before writing provider output,
test output, and dashboards, but redaction cannot identify every possible
secret. Treat logs as sensitive and keep them out of public issue comments.

When `sandbox.dockerfile` is configured, Agentic builds it before an isolated
run. Dockerfile `RUN` steps execute with the container engine's build
privileges, so only build trusted files and review changes to the Dockerfile
and build context. Runtime network access remains disabled by default;
`sandbox.allow_network` is a separate, explicit capability.

## Reporting a vulnerability

Please do not disclose exploitable security issues in a public issue. Report
the affected version, reproduction steps, impact, and a minimal safe proof to
the repository maintainers through the private security contact configured on
the GitHub repository. Do not include credentials or private repository data.
