# Security model

Agentic is a workflow orchestrator, not a host operating-system sandbox.
Issue descriptions, repository files, test output, provider responses, and
model-generated tool arguments are treated as untrusted input.

## Execution modes

By default, configured tests and native provider CLIs run under the account
that launched Agentic. Git worktrees isolate repository changes, but they do
not isolate host files, credentials, processes, or networking. Use a trusted
or disposable checkout and do not expose production credentials.

The developer is write-capable inside its assigned worktree. The reviewer is
read-only and uses restricted provider/tool policies. Agentic blocks commits,
pushes, publication, protected files, secret-like files, and GitHub workflow
changes during normal development.

Optional Docker/Podman sandboxing can isolate native provider and test
processes. It requires an image containing the provider CLI and project tools.
OpenRouter is not supported in sandbox mode because its API adapter runs in
the host Python process. See the configuration guide for the exact limits.

## Credentials and logs

Credentials must be supplied through environment variables. Do not put API
keys in `agent_config.json`, issue descriptions, prompts, or repository files.
Agentic redacts known environment values before writing provider output,
test output, and dashboards, but redaction cannot identify every possible
secret. Treat logs as sensitive and keep them out of public issue comments.

## Reporting a vulnerability

Please do not disclose exploitable security issues in a public issue. Report
the affected version, reproduction steps, impact, and a minimal safe proof to
the repository maintainers through the private security contact configured on
the GitHub repository. Do not include credentials or private repository data.
