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

The developer is write-capable inside its assigned worktree. The reviewer is
read-only and uses restricted provider/tool policies. Agentic blocks commits,
pushes, publication, protected files, secret-like files, and GitHub workflow
changes during normal development.

Docker/Podman sandboxing mounts the task worktree, the required linked-worktree
Git metadata, and run-output directory, uses a read-only container root, drops
Linux capabilities, limits resources, and disables networking by default.
Native providers and configured checks run inside it. Credentials are passed
only for environment names explicitly listed in `sandbox.pass_env`.
OpenRouter's API request and scoped file inspection remain in the host Python
process, but OpenRouter command tools are also routed through the configured
sandbox.

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

## Reporting a vulnerability

Please do not disclose exploitable security issues in a public issue. Report
the affected version, reproduction steps, impact, and a minimal safe proof to
the repository maintainers through the private security contact configured on
the GitHub repository. Do not include credentials or private repository data.
