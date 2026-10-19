# Isolated sandbox images

An isolated Agentic run needs an image containing the selected provider CLI,
Git, and every executable used by the target repository's configured checks.
Agentic does not install provider tools into the image automatically because
their installation and authentication methods vary by provider.

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

## Image ownership

Treat the image as part of the repository's security boundary. Pin its base
image and provider versions, rebuild it when those dependencies change, and
do not publish an image containing personal credentials or production tokens.
