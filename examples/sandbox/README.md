# Isolated sandbox images

An isolated Agentic run needs an image containing the selected provider CLI,
Git, and every executable used by the target repository's configured checks.
Agentic does not install provider tools into the image automatically because
their installation and authentication methods vary by provider.

## Starter Dockerfiles

This directory contains small, pinned starting points for common projects:

| Language | Template |
| --- | --- |
| Python | [`Dockerfile.python`](Dockerfile.python) |
| Node.js | [`Dockerfile.node`](Dockerfile.node) |
| Go | [`Dockerfile.go`](Dockerfile.go) |
| Rust | [`Dockerfile.rust`](Dockerfile.rust) |
| Java | [`Dockerfile.java`](Dockerfile.java) |
| .NET | [`Dockerfile.dotnet`](Dockerfile.dotnet) |

Copy a template into the target repository, uncomment the project-specific
package/tool lines, and add the provider CLI required by the selected
developer and reviewer. Build it before enabling isolation:

```bash
docker build --pull=false -t my-agentic-runner:latest -f Dockerfile.python .
agentic init --secure --sandbox-image my-agentic-runner:latest
```

Alternatively, let Agentic build a repository-relative Dockerfile before an
isolated run:

```bash
agentic init --secure --sandbox-image my-agentic-runner:latest \
  --sandbox-dockerfile Dockerfile.python --sandbox-context .
```

The `sandbox.dockerfile` and `sandbox.build_context` configuration fields are
relative to the target repository. `sandbox.image` is still required as the
tag to build and run. Existing configurations that specify only `image` keep
their current behavior, and disabled sandboxes never build an image.

## Build checklist

Start from a project-owned base image that is compatible with the selected
provider, then add:

- the provider CLI (`codex`, `gemini`, `claude`, or `copilot`);
- Git and any file-search tools used by the provider;
- the target repository's language runtime and build/test tools;
- trusted CA certificates when the provider needs outbound API access.

Keep the image free of long-lived credentials when possible. Prefer
`sandbox.pass_env` for explicitly named credential variables, and keep
`sandbox.allow_network` disabled unless the provider must contact its service.

Example initialization:

```bash
agentic init --secure --sandbox-image my-agentic-runner:latest
agentic doctor
```

`agentic doctor` verifies the engine and image, checks the effective network
policy, and probes native provider executables inside the image. It never
falls back to host execution when isolated mode is selected. Agentic invokes
each requested executable directly, so the image's default entrypoint is
overridden.

Automatic language detection in `agentic init` records a best-effort
`sandbox.language` hint for Python, Node.js, Go, Rust, Java, or .NET projects.
Use `--sandbox-language` or set `sandbox.language` explicitly when a repository
contains multiple languages. Detection does not install tools or select an
image automatically; choose and customize the matching template.

## Image ownership

Treat the image as part of the repository's security boundary. Pin its base
image and provider versions, rebuild it when those dependencies change, and
do not publish an image containing personal credentials or production tokens.
Building a Dockerfile executes its `RUN` steps with the container engine's
build privileges, so only enable a Dockerfile you trust. Keep network access
disabled for runtime unless the provider needs it, and pass credential names
through `sandbox.pass_env` rather than storing values in the image.
