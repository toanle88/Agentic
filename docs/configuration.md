# Configuration

Agentic reads the personal, target-specific `agent_config.json` from the target repository unless `--config` points to another file. The repository contains [`agent_config.example.json`](../agent_config.example.json) as a structural example; it is not a universal configuration.

For editor validation, use the checked-in [`agent_config.schema.json`](../agent_config.schema.json) as the JSON Schema. The schema is a convenience for editors; Agentic remains the source of truth and validates configuration before a run.

The easiest setup is `agentic init`. It creates an ignored config with a detected starter check. Choose a provider during initialization when needed:

```bash
agentic init --provider codex
agentic init --provider gemini
agentic init --provider claude
agentic init --provider copilot
agentic init --provider openrouter
```

The same complete templates are available under
[`examples/provider-configs/`](../examples/provider-configs/). Treat the
detected `test_commands` as a starting point and replace them with the target
project's real checks before running an issue.

Runtime state is controlled separately from project configuration. Use `--state-dir PATH` or `AGENTIC_STATE_DIR` for worktrees, logs, shell records, and dashboards. This is especially useful in WSL when Windows Git must open the worktree from a shared mounted drive.

## Example

```json
{
  "task_source": "github",
  "test_commands": [
    ["make", "check-fast"],
    ["pnpm", "run", "test:web:fast"],
    ["pnpm", "run", "build:web"],
    ["pnpm", "run", "contract:check"]
  ],
  "slow_test_commands": [
    ["make", "persistence-check"],
    ["pnpm", "run", "test:a11y"]
  ],
  "max_retries": 3,
  "max_files": null,
  "test_timeout_seconds": 300,
  "task_timeout_seconds": 1800,
  "developer": {
    "provider": "codex",
    "model": null,
    "reasoning_effort": null,
    "timeout_seconds": 600
  },
  "reviewer": {
    "provider": "codex",
    "model": null,
    "reasoning_effort": null,
    "timeout_seconds": 300
  }
}
```

Use commands that are actually supported by the target repository. Agentic does not assume that the project uses Python, Go, Node, or a particular test framework.

## Settings

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `task_source` | `github` or `local` | `github` | Selects where work requirements come from. GitHub mode uses issue numbers; local mode accepts `--task`, `--task-file`, or pasted tasks from `/run`. |
| `test_commands` | array of argv arrays | required | Checks run inside the worktree. Each command must be an array such as `["go", "test", "./..."]`; shell strings are not accepted. |
| `slow_test_commands` | array of argv arrays | `[]` | Additional checks run after the fast checks when `--profile slow` is selected. |
| `max_retries` | integer `0..3` | `3` | Extra developer attempts after the first attempt. `3` permits up to four total attempts. |
| `max_files` | integer `1..100` or `null` | `null` | Maximum distinct files in the proposed patch. `null` disables this limit. |
| `codex_model` | string or `null` | `null` | Model passed to Codex. `null` inherits the Codex CLI configuration. |
| `codex_reasoning_effort` | string or `null` | `null` | Reasoning setting passed to Codex, such as `max`. `null` inherits the Codex CLI configuration. |
| `codex_timeout_seconds` | integer or `null` | `null` | Legacy developer/reviewer timeout. `null` uses the safe provider default. |
| `test_timeout_seconds` | integer or `null` | `300` | Per-test-command timeout. `null` uses 300 seconds at runtime. |
| `task_timeout_seconds` | integer | `1800` | Maximum wall-clock time for one task invocation, including checks and review. |
| `security_mode` | `trusted` or `isolated` | `trusted` | `isolated` requires the configured Docker/Podman engine and image and fails closed when either is unavailable. |
| `sandbox` | object | disabled | Optional Docker/Podman isolation for provider, OpenRouter tool, and test processes. `pass_env` names explicit credential variables to pass into the container. |
| `developer` | object | Codex defaults | Provider settings for the coding agent. |
| `reviewer` | object | Codex defaults | Provider settings for the independent reviewer. |

Each provider object supports these fields:

| Field | Type | Description |
| --- | --- | --- |
| `provider` | string | `codex`, `gemini`, `claude`, `copilot`, or `openrouter`. |
| `model` | string or `null` | Provider model; `null` uses the native CLI default. OpenRouter requires a model. |
| `reasoning_effort` | string or `null` | Optional reasoning setting used by Codex/OpenRouter, such as `max`; native CLIs use their own options. |
| `timeout_seconds` | integer or `null` | Per developer/reviewer invocation limit; `null` uses 600 seconds for developers or 300 seconds for reviewers. |
| `api_key_env` | string | OpenRouter only: environment variable containing the API key. |
| `base_url` | URL | OpenRouter only: API base URL, defaulting to the public OpenRouter endpoint. |
| `max_tool_turns` | integer or `null` | OpenRouter only: maximum number of model/tool rounds; default `80`. |
| `command_timeout_seconds` | integer or `null` | OpenRouter only: maximum duration of one tool command; `null` uses 300 seconds. |
| `allow_network` | boolean | OpenRouter only: allow explicitly network-capable commands. Defaults to `false`. This is not an OS-level network sandbox. |

### Providers

`developer.provider` and `reviewer.provider` may be `codex`, `gemini`,
`claude`, `copilot`, or `openrouter`. The developer and reviewer can use
different providers. Native providers use their own CLI installation and
authentication; Agentic never stores their credentials.

For OpenRouter, provide a model and an environment-variable name, never the
secret itself:

```json
{
  "developer": {
    "provider": "openrouter",
    "model": "openai/gpt-5.4",
    "reasoning_effort": "max",
    "timeout_seconds": null,
    "api_key_env": "OPENROUTER_API_KEY",
    "base_url": "https://openrouter.ai/api/v1"
  },
  "reviewer": {
    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-4.5",
    "api_key_env": "OPENROUTER_API_KEY",
    "base_url": "https://openrouter.ai/api/v1"
  }
}
```

Then export the key in the shell that runs Agentic:

```bash
export OPENROUTER_API_KEY='your-key'
```

### Container sandbox and security modes

Existing configurations default to `trusted` for compatibility. New
configurations generated by `agentic init` also default to `trusted`, so
host-installed provider authentication continues to work. Trusted mode allows
native providers and configured checks to execute under the host account; a
worktree is not an operating-system sandbox.

To generate the isolated profile explicitly, provide an image containing the
selected provider CLI and target-repository tools:

```bash
agentic init --security-mode isolated --sandbox-image your-project-agentic-runner:latest
```

The image can also be supplied through `AGENTIC_SANDBOX_IMAGE`. Use trusted
mode only when host execution is appropriate for the repository.

For untrusted repositories or issue text, use isolated mode:

For native CLI providers and configured test commands, Agentic can run child
processes in a hardened Docker or Podman container:

```json
{
  "security_mode": "isolated",
  "sandbox": {
    "enabled": true,
    "engine": "docker",
    "image": "your-project-agentic-runner:latest",
    "allow_network": false,
    "pass_env": ["OPENAI_API_KEY"]
  }
}
```

The image must be built and contain the selected provider CLI and all tools
needed by the configured checks. Agentic mounts the task worktree, linked
worktree Git metadata, and the run-output directory, makes the container root
filesystem read-only, drops Linux capabilities, applies process/resource
limits, and disables networking unless explicitly enabled. If the engine or
image is unavailable, isolated runs stop instead of falling back to host
execution.

`sandbox.pass_env` contains environment variable names, never secret values.
Only names listed there and present in the host environment are passed to the
container. For example, native providers commonly use `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `GH_TOKEN`, or `GITHUB_TOKEN`. The
provider may instead be authenticated inside the image; `agentic doctor`
reports that arrangement as an explicit warning when no pass-through names are
configured.

OpenRouter receives scoped function tools for listing and reading files,
running argv commands, and (for the developer only) writing files. Commits,
pushes, shell wrappers, interpreter code-evaluation flags, unsafe Git options,
and path escapes are blocked by the adapter; command execution is routed
through the configured sandbox when enabled. File-tool paths stay inside the
worktree. Native developer CLIs run in the isolated Agentic worktree; their
provider approval modes are set to allow autonomous edits, while reviewer
modes are read-only where the provider supports that capability.

OpenRouter tool commands have a per-command timeout and network-command deny
list by default. Set `allow_network` only for a trusted repository and model;
the setting permits selected network-capable executables but cannot prevent a
programming language runtime from opening a socket when host execution is in
use. In isolated mode, the container network policy is the enforcement point.

The legacy `codex_*` settings remain supported for existing configurations.
When a `developer` or `reviewer` object supplies the same setting, the nested
value wins. New configurations should use the nested provider objects.

Timeout values must be between 1 second and 24 hours when set. `max_retries` is limited to 0 through 3 by the configuration validator.

## Test command rules

Commands are represented as argument arrays rather than shell strings:

```json
{
  "test_commands": [
    ["go", "test", "./..."],
    ["pnpm", "run", "test:web:fast"]
  ]
}
```

This prevents task text or filenames from becoming shell syntax and makes command execution predictable across environments. Commands run with the issue or local-task worktree as their current directory.

Agentic validates executables and Python `-m` modules before development starts. Missing dependencies, unavailable commands, port conflicts, and missing files are environmental failures; they should be fixed in the environment or configuration rather than retried as code defects.

## Choosing checks

Use a fast, deterministic set for normal iteration. Include the checks needed to prove the issue, for example:

- unit and package tests;
- type checking and production builds;
- API or generated-artifact drift checks; and
- focused frontend tests when the issue changes the UI.

Keep Docker, PostgreSQL, browser, end-to-end, and release-only checks in CI or a separate opt-in command when they require external services or take substantially longer. For persistence or public API changes, run those broader gates before publication even if they are not part of the fast local configuration.

Select the slow profile explicitly:

```bash
agentic --issue 119 --profile slow
```

The slow profile runs every `test_commands` entry first, followed by every `slow_test_commands` entry. If no slow checks are configured, Agentic stops with a configuration error instead of pretending the slow profile ran.

## Per-developer configuration

Configuration is target-repository input. If developers need different models, checks, or limits, keep the personal `agent_config.json` untracked or ignored and commit a separate example file for the team. Never put credentials, API keys, or production connection strings in the configuration.

## Task source

GitHub issue mode is the default:

```json
{ "task_source": "github" }
```

To work from manually pasted requirements instead, set:

```json
{ "task_source": "local" }
```

Local tasks use the same worktrees, tests, reviews, retries, resume behavior,
logs, and dashboard. They can still be published as GitHub draft pull requests
with `--publish`; the PR contains the local task text instead of a `Closes #N`
reference.

For a direct run, use either:

```bash
agentic --task "Add CSV invoice export"
agentic --task-file task.md
```

In the interactive shell, use `/run` without an issue number. Paste the task
and finish with a line containing `/done`:

```text
agentic> /run
Paste the local task. Finish with a line containing /done; use /cancel to abort.
task> Add CSV invoice export.
task> Include regression tests.
task> /done
```

Use an explicit file when needed:

```bash
agentic --repo /path/to/target-repository \
  --config /path/to/agent_config.json \
  --issue 119
```
