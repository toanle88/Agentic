# Configuration

Agentic reads the personal, target-specific `agent_config.json` from the target repository unless `--config` points to another file. The repository contains [`agent_config.example.json`](../agent_config.example.json) as a structural example; it is not a universal configuration.

For editor validation, use the checked-in [`agent_config.schema.json`](../agent_config.schema.json) as the JSON Schema. The schema is a convenience for editors; Agentic remains the source of truth and validates configuration before a run.

The easiest setup is `agentic init`. It creates an ignored config with a detected starter check by adding `agent_config.json` to Git's local `.git/info/exclude`; the tracked `.gitignore` is not changed. Local excludes are specific to one clone, so run `agentic init` separately in each clone. Choose a provider during initialization when needed:

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

## Assisted test discovery

For a new repository, `agentic init --discover-tests` inspects common project
metadata such as `pyproject.toml`, `package.json`, `Makefile`, `go.mod`, and
`Cargo.toml`. Before running any candidate derived from that metadata, it asks
for explicit confirmation because project-defined scripts can have side
effects. After confirmation it proposes direct argv-array checks, runs each
candidate from the repository with a bounded per-candidate timeout, and labels missing tools
or external dependencies as skipped rather than code failures. Passing checks
are classified as fast or slow using explicit metadata evidence and measured
duration: a check taking at least 30 seconds is slow, as is a check whose
metadata identifies integration, end-to-end, build, release, or benchmark
work.

Discovery never creates an installer command, runs a shell, or parses package
scripts into shell commands. When multiple checks make interpretation useful,
the selected provider may suggest additional argv candidates from the bounded,
safe metadata inventory (including project-specific package scripts and Make
targets) through its read-only interface; unsafe, malformed, or unsupported
suggestions are discarded. Provider failure does not make deterministic
discovery fail.

Only candidates that start successfully and exit zero are eligible to be
saved. In trusted mode, candidate processes receive a restricted copy of the
host environment: credential-like variables and configured provider or
`sandbox.pass_env` names are removed. This reduces accidental credential
exposure but is not operating-system isolation, so use isolated mode for
untrusted repositories or issue text. Missing executables, unavailable
modules, dependency-related exit statuses, failed checks, and timeouts are
never saved. The proposed configuration is printed and requires a second
explicit `y` confirmation before `agent_config.json` is written; any other
response leaves the repository unchanged. Use isolated mode when candidate
execution should run inside the
configured sandbox. An existing config is preserved without running discovery unless
`--force` is supplied. Fast candidates become `test_commands`; slow candidates
become `slow_test_commands` and run only with the slow test profile. Use
`--discovery-timeout SECONDS` to change the per-candidate limit (1 through
86,400 seconds).
With `--security-mode isolated` (or `--secure`), the sandbox is preflighted
before discovery and both provider assistance and candidate checks run inside
that sandbox; discovery fails closed if it cannot be applied.

Runtime state is controlled separately from project configuration. Use `--state-dir PATH` or `AGENTIC_STATE_DIR` for worktrees, logs, shell records, and dashboards. This is especially useful in WSL when Windows Git must open the worktree from a shared mounted drive. On POSIX systems, Agentic rejects symlinked, non-directory, or attacker-writable state ancestors and requires the configured state root to be private; writers create missing state hierarchy through an anchored, race-resistant path. Choose a shared mount with permissions that satisfy this policy, or keep runtime state in a private user directory and share only the worktree when necessary.

## Example

```json
{
  "task_source": "github",
  "automation": {
    "auto_publish": false
  },
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
  "system_prompt": "Follow our development standards and add tests for behavior changes.",
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

### Automatic publication

Set `automation.auto_publish` to `true` to opt in to external publication.
The default is `false`. A valid completed audit is then published as a GitHub
issue, while a successful issue or local-task run with durable approval is
published as a draft pull request. Existing approval, validation,
protected-file, and publication checks still apply.

Automatic publication skips offline runs, local tasks without a GitHub origin,
unavailable GitHub metadata or authentication, invalid or incomplete durable
state, and unavailable remotes. Existing remote issues and pull requests, plus
a local `publication.json` record, are detected so retries do not duplicate
them. A failure preserves the completed run, worktree, and logs for manual
`--publish` retry.

## Settings

| Setting | Type | Default | Description |
| --- | --- | --- | --- |
| `task_source` | `github` or `local` | `github` | Selects where work requirements come from. GitHub mode uses issue numbers; local mode accepts `--task`, `--task-file`, or pasted tasks from `/run`. |
| `test_commands` | array of argv arrays | required | Checks run inside the worktree. Each command must be an array such as `["go", "test", "./..."]`; shell strings are not accepted. |
| `slow_test_commands` | array of argv arrays | `[]` | Additional checks run after `test_commands` when `--profile slow` is selected. Profiles select checks, not the provider model or reasoning setting. |
| `max_retries` | integer `0..5` | `3` | Extra developer attempts after the first attempt. `5` permits up to six total attempts. |
| `ci_repair.max_attempts` | integer `1..5` | `2` | Maximum pushed repair cycles for `--pr N --repair-ci`, separate from `max_retries`. |
| `ci_repair.poll_interval_seconds` | integer `1..86400` | `15` | Delay between bounded post-push CI status polls. |
| `ci_repair.poll_timeout_seconds` | integer `1..86400` | `900` | Maximum wait for one post-push CI result. |
| `ci_repair.auto_confirm_non_code_failures` | boolean | `false` | Skip confirmation before attempting infrastructure, permission, or ambiguous CI repairs. Use only for explicitly unattended repair workflows. |
| `max_files` | integer `1..100` or `null` | `null` | Maximum distinct files in the proposed patch. `null` disables this limit. |
| `codex_model` | string or `null` | `null` | Model passed to Codex. `null` inherits the Codex CLI configuration. |
| `codex_reasoning_effort` | string or `null` | `null` | Reasoning setting passed to Codex, such as `max`. `null` inherits the Codex CLI configuration. |
| `codex_timeout_seconds` | integer or `null` | `null` | Legacy developer/reviewer timeout. `null` uses the safe provider default. |
| `test_timeout_seconds` | integer or `null` | `300` when omitted | Per-test-command timeout. Explicit `null` disables the per-test-command limit. |
| `task_timeout_seconds` | integer | `1800` | Maximum wall-clock time for one task invocation, including checks and review. |
| `workflow_mode` | `quick`, `standard`, or `strict` | `standard` | `quick` skips automatic investigation only for conservatively classified low-risk tasks; `standard` preserves the normal flow; `strict` always performs independent investigation. High-risk tasks always require investigation. The CLI `--workflow-mode` overrides this setting for one run. |
| `github_host` | hostname | `github.com` | Host used explicitly for GitHub CLI repository, issue, and publication operations. This is independent of provider hosts. |
| `automation.auto_publish` | boolean | `false` | Opt in to automatic publication of eligible completed audits, approved issue/local-task runs, and reviewed CI-repair patches. For CI repair, this authorizes the guarded same-branch commit and push; it does not suppress non-code failure confirmation. |
| `system_prompt` | nonempty string or `null` | `null` | Optional persistent project guidance included as bounded, untrusted context in developer and reviewer prompts. |
| `permission_policy` | object | safe role defaults | Provider-independent capabilities for `developer` and `reviewer`; legacy configurations are normalized to these defaults. |
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
| `executable_path` | string or `null` | Copilot only: optional explicit executable path. In trusted mode, `COPILOT_PATH` and the GitHub CLI download at `~/.local/share/gh/copilot/copilot` are also detected automatically. In isolated mode, set an image-visible path because host paths are not mounted. |
| `host` | hostname or `null` | Copilot only: host used for Copilot authentication and model access. Agentic sets the per-process `COPILOT_GH_HOST` environment variable and never changes global `GH_HOST`. |
| `auth` | `native_cli`, `api_key`, or `both` | Authentication mode. Omitted values preserve native CLI authentication unless `api_key_env` is present; OpenRouter always uses `api_key`. |
| `api_key_env` | string | Environment-variable name for the selected provider credential. Defaults to `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `GH_TOKEN`, or `OPENROUTER_API_KEY` when API-key mode is selected. The value is never stored in configuration. |
| `base_url` | URL | OpenRouter only: HTTPS API base URL, defaulting to the public OpenRouter endpoint. |
| `allow_custom_endpoint` | boolean | OpenRouter only: allow an arbitrary HTTPS host for a self-hosted or compatible endpoint. HTTP and embedded URL credentials are never permitted. |
| `max_tool_turns` | integer or `null` | OpenRouter only: maximum number of model/tool rounds; default `40`. |
| `command_timeout_seconds` | integer or `null` | OpenRouter only: maximum duration of one tool command; `null` uses 300 seconds. |
| `allow_network` | boolean | OpenRouter only: allow explicitly network-capable commands. Defaults to `false`. This is not an OS-level network sandbox. |
| `json_mode` | boolean | OpenRouter only: request JSON-mode responses when the configured model/provider endpoint is known to support it. Defaults to `false`; local schema validation is always used. |

### Permission policy

The normalized policy is independent of the selected provider. The developer
defaults permit file reads/searches, edits and new files inside the assigned
worktree, read-only Git inspection (`status`, `diff`, `log`, and `show`), and
configured checks. Network-capable tool commands and dangerous permissions are
disabled by default. The reviewer defaults permit only reads, searches, and
read-only Git inspection; review never enables writes, arbitrary checks, or
dangerous permissions.

The policy fields are `read_files`, `search_files`, `edit_files`,
`create_files`, `git_read`, `run_checks`, `network`, and `allow_dangerous`.
OpenRouter enforces these fields in its scoped tools. Native providers receive
provider-specific tool allow/deny rules: Claude developer turns receive
path-scoped `Read`, `Glob`, `Grep`, `Edit`, and `Write` rules rooted at the
assigned worktree (or `/workspace` in a container), plus exact patterns for
configured check argv and read-only Git commands plus explicit denials for Git
output redirection and external diff/text conversion. Claude reviewer turns
receive only file-inspection tools; Agentic supplies the staged patch from its
argv-only Git runner. With `network: true`, Claude additionally receives
explicit network shell patterns and its web tools; it does not receive a
wildcard shell grant. Claude does not support a create-only policy: a
configuration with `create_files: true` and `edit_files: false` is rejected.
Gemini developer turns receive its safe-tool approval list plus command-scoped
`ShellTool(git status)`, `ShellTool(git diff)`, `ShellTool(git log)`, and
`ShellTool(git show)` entries when `git_read` is enabled. Gemini reviewers
receive only file-inspection tools; Agentic supplies the staged patch from its
argv-only Git runner. Native-provider `git log` is intentionally limited to
the bare metadata-only command because provider tool patterns cannot safely
validate its argv. OpenRouter requires read-only `git log` to include the
fixed metadata-only format `--format=%H%x09%an%x09%aI%x09%s` (hash, author,
ISO author date, and subject). It also accepts the equivalent
`--pretty=format:` spelling, but rejects bare log, patch, line-range,
`--follow`, message/notes directives, and other history-format options.
A developer with `run_checks: true` is represented by
Agentic running the configured checks itself; it does not grant Gemini
unrestricted shell access. Gemini
`network: true` is rejected because its native network-capable shell tool is
unrestricted; a sandbox does not make arbitrary shell commands an equivalent
network-only capability. Set `run_checks: false` for trusted edit-only turns.
Reviewers receive no native shell access. The `gh` command is
excluded from native network allowlists so publication remains an explicit
Agentic workflow step.
Copilot developer turns receive only file and search tools plus the cwd-scoped
bare `write` rule for worktree files; they do not receive native shell access.
Agentic runs configured checks and Git inspection in its own argv-only runner,
which avoids Copilot's prompt and persisted-approval boundary. Copilot
`network: true` is rejected because its native network capability requires
unrestricted shell access. Copilot reviewer turns likewise receive only
file-inspection tools and no shell. The
Agentic sandbox, when enabled, remains the filesystem and process boundary.

`allow_dangerous: true` is an explicit escape hatch for the developer only.
It is rejected unless `security_mode` is `isolated`, emits a prominent warning,
and is recorded in run security metadata. It enables provider-specific
dangerous approval flags (including Claude bypass permissions and Copilot
allow-all flags) but does not permit reviewers to write, bypass protected
paths, commit, push, or publish. Keep it disabled unless the additional
capability is necessary.

### Providers

`developer.provider` and `reviewer.provider` may be `codex`, `gemini`,
`claude`, `copilot`, or `openrouter`. The developer and reviewer can use
different providers. Native providers use their own CLI installation and
authentication by default; Agentic never stores their credentials. To use an
environment-backed credential, set `auth` to `api_key` (or set `api_key_env`,
which selects that mode) and optionally override the provider default:

```json
{
  "developer": {
    "provider": "claude",
    "auth": "api_key",
    "api_key_env": "ANTHROPIC_API_KEY"
  },
  "reviewer": {
    "provider": "copilot",
    "api_key_env": "REVIEW_GITHUB_TOKEN"
  }
}
```

The supported defaults are `OPENAI_API_KEY` for Codex, `GEMINI_API_KEY` for
Gemini, `ANTHROPIC_API_KEY` for Claude, and `GH_TOKEN` (a GitHub token) for
Copilot. `auth: "both"` requires both the environment credential and native
CLI authentication. Export the selected variables in the shell that runs
Agentic. In isolated mode, add each selected variable to `sandbox.pass_env`;
only the role's selected credential is passed to its provider process.

GitHub repository operations use `github_host` through a per-process `GH_HOST`
environment override. A Copilot provider can use a different Enterprise Cloud
or data-residency host with its role-specific `host` setting:

Completed online GitHub issue runs also use the authenticated GitHub CLI to
read existing issue comments and write one idempotent final summary comment.
The identity must have permission to read and write issue comments. This is
automatic and has no configuration flag; offline resumes, local tasks, and
audits do not use it.

```json
{
  "github_host": "github.com",
  "developer": {
    "provider": "copilot",
    "host": "codehq.ghe.com",
    "model": "gpt-5.6-luna"
  },
  "reviewer": {
    "provider": "copilot",
    "host": "codehq.ghe.com",
    "model": "gpt-5.6-luna"
  }
}
```

The Copilot CLI documents `COPILOT_GH_HOST` as the Copilot-only override for
`GH_HOST`; it is passed only to the provider process (and to its isolated
container when configured). If the selected model is rejected by that host,
Agentic reports the model and provider host so account/host provisioning can be
corrected without exposing credentials.

When Copilot is installed through `gh copilot`, `agentic doctor` checks the
normal `PATH` first and then the GitHub CLI download location
`~/.local/share/gh/copilot/copilot` (or `$XDG_DATA_HOME/gh/copilot/copilot`).
The check is read-only. If it finds the downloaded executable, Agentic uses it
for trusted provider runs and reports the path with instructions to add its
directory to `PATH` or set `COPILOT_PATH`. An explicit
`developer.executable_path` or `reviewer.executable_path` takes precedence.
For isolated runs, the configured sandbox image must contain Copilot; an
`executable_path` there is interpreted inside the image.

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
    "base_url": "https://openrouter.ai/api/v1",
    "allow_custom_endpoint": false
  },
  "reviewer": {
    "provider": "openrouter",
    "model": "anthropic/claude-sonnet-4.5",
    "api_key_env": "OPENROUTER_API_KEY",
    "base_url": "https://openrouter.ai/api/v1",
    "allow_custom_endpoint": false
  }
}
```

The public `openrouter.ai` endpoint is allowed by default. A compatible
self-hosted HTTPS endpoint requires `allow_custom_endpoint: true`; Agentic
rejects HTTP endpoints and disables HTTP redirects so the API key cannot be
sent to a different host.

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
# equivalent shorthand:
agentic init --secure --sandbox-image your-project-agentic-runner:latest
```

The image can also be supplied through `AGENTIC_SANDBOX_IMAGE`. The `--secure`
option is only a shorthand; it still requires a provider-ready image. Use
trusted mode only when host execution is appropriate for the repository.

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

The sandbox can use an existing image or build one from a repository-owned
Dockerfile. A Dockerfile requires an image tag and an optional repository-
relative build context; Agentic runs the build with an argv array before the
isolated checks and provider processes start:

```json
{
  "security_mode": "isolated",
  "sandbox": {
    "enabled": true,
    "engine": "docker",
    "image": "my-project-agentic:latest",
    "dockerfile": "Dockerfile.agentic",
    "build_context": ".",
    "language": "python",
    "allow_network": false,
    "build_network": true,
    "pass_env": ["OPENAI_API_KEY"]
  }
}
```

`dockerfile` and `build_context` must be relative paths inside the target
repository. The build is opt-in: omitted Dockerfile settings do not change
existing image-only behavior, and trusted mode never builds or runs a
container. See the [language-specific starter templates](../examples/sandbox/README.md)
for pinned Python, Node.js, Go, Rust, Java, and .NET examples. Templates
include comments for adding project dependencies, native packages, test tools,
and the selected provider CLI.

`build_network` controls network access while the configured Dockerfile is
built. It defaults to `true` so Dockerfiles that install dependencies remain
functional; set it to `false` for a network-disabled build when the Dockerfile
uses only locally available inputs. This build setting is separate from
`allow_network`, which controls runtime containers.

`agentic init` records a best-effort language hint from common project markers.
Use `--sandbox-language` or set `sandbox.language` to override it. Detection is
only a hint; it does not install tools or make a sandbox mandatory.

The image must contain the selected provider CLI and all tools needed by the
configured checks. When `sandbox.dockerfile` is configured, Agentic builds the
image first. Agentic mounts the task worktree writable for
developer processes, the worktree `.git` entry and Git metadata read-only for
both developer and reviewer processes, and the run-output directory. It makes the container
root filesystem read-only, drops Linux capabilities, applies process/resource
limits, and disables networking unless explicitly enabled. Agentic invokes the
requested argv directly and overrides any image entrypoint. If the engine or
image is unavailable, isolated runs stop instead of falling back to host
execution.

Filesystem read-only mode and networking are independent policies. A read-only
reviewer still needs outbound access when its native provider CLI must contact
its model service. Therefore `sandbox.allow_network: true` enables networking
for both developer and reviewer processes, while their worktree and Git
metadata mounts remain read-only for review. Treat this as a broad outbound
capability and enable it only when the repository, provider, and credentials
are trusted. `agentic doctor` reports the effective network policy.

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
worktree, and command-based inspection applies the same resolved-path and
protected-file checks. Read-only allowlisted inspection commands must use a
bare executable name; path-qualified lookalikes such as `./git` or an
absolute worktree path are rejected before execution. Recursive `rg` directory
scans and content-producing Git commands without explicit regular-file paths
are rejected. Native
developer CLIs run in the isolated Agentic worktree; their
provider approval modes are set to allow autonomous edits, while reviewer
modes are read-only where the provider supports that capability.

OpenRouter tool commands have a per-command timeout and network-command deny
list by default. When a sandbox is enabled, the tool boundary also forces the
command container onto `--network none` unless the OpenRouter permission is
enabled; a broader sandbox network setting cannot override a narrower tool
policy. Set `allow_network` only for a trusted repository and model; in
trusted host mode the setting permits selected network-capable executables but
cannot prevent an arbitrary programming language runtime from opening a
socket. Use isolated mode when OS-level network denial is required.

The legacy `codex_*` settings remain supported for existing configurations.
When a `developer` or `reviewer` object supplies the same setting, the nested
value wins. New configurations should use the nested provider objects.

Timeout values must be between 1 second and 24 hours when set. `max_retries` is limited to 0 through 5 by the configuration validator.

### Prompt guidance

`system_prompt` is persistent project guidance for the developer and
independent read-only reviewer. Agentic bounds and escapes it, passes it as
plain prompt data to every configured provider, and never turns it into shell
syntax or a provider-native authority level. Repository safety rules,
`AGENTS.md`, and the developer/reviewer separation take precedence over it.

The setting is read again on each invocation, including resumes, so changing
the target configuration changes future prompts without rewriting older run
records. It is not copied into run metadata; the per-run `--instruction` value
is the prompt data that is persisted and shown in the dashboard.

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

`agentic doctor` performs the same lightweight executable preflight without
running project checks. To verify that the configured commands actually run,
use the explicit command mode:

```bash
agentic doctor --commands --worktree /path/to/worktree
agentic doctor --commands --profile slow --json
```

The `/doctor` shell command accepts the same `--commands`, `--profile`, and
`--worktree` options. Command mode is opt-in because installs, builds, browser
setup, generated files, services, and network access can be expensive or
mutating. Without `--worktree`, checks run from the repository passed with
`--repo` (normally the current checkout). Results identify each command and
its exit status; command output is not included in JSON diagnostics.

Doctor output is grouped into repository prerequisites, GitHub task access,
and agent access. Repository checks cover Git, the repository and `origin`,
and worktree path compatibility. GitHub mode separately checks repository and
issue read access. When `task_source` is `local`, those two GitHub checks are
reported as skipped because they are not required. Agent checks are emitted
for the developer and reviewer independently, even when they use the same
provider. Codex and Claude include a read-only authentication-status probe;
Gemini and Copilot report authentication as skipped because doctor does not
invoke their documented login flows or infer provider state from unrelated
commands. Provider output, credential values, configured command arguments, and
credential environment-variable names are never printed. For each role, JSON
diagnostics include `auth_mode`, `credential_set`, and `sandbox_passed` (`null`
when no isolated sandbox applies). Human-readable output names the selected
mode and credential status without exposing the environment variable name.
Native authentication checks are skipped when API-key mode is selected, so an
unavailable unused login does not fail the check.

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
