# Sandbox configuration

Use isolated mode when provider or check processes should not run with the
host account's permissions. The sandbox is configured in `agent_config.json`;
the full field reference and security model are in
[Configuration](configuration.md#container-sandbox-and-security-modes).

## Minimal generic profile

The image must already contain the selected provider CLI, Git, and the tools
used by the target repository's checks. Credential values never belong in the
configuration file.

```json
{
  "security_mode": "isolated",
  "sandbox": {
    "enabled": true,
    "engine": "docker",
    "image": "your-project-agentic-runner:latest",
    "allow_network": false,
    "pass_env": ["PROVIDER_API_KEY"]
  }
}
```

Set `pass_env` to the names of credentials required by the selected provider,
and export those variables in the shell that starts Agentic. Keep runtime
networking disabled unless the provider requires it.

## Building an image

An image can be built from a repository-relative Dockerfile. Builds use a
bounded context that excludes protected files and non-regular entries, and
build networking is disabled unless `build_network` is explicitly enabled.
The repository contains [generic starter Dockerfiles](https://github.com/toanle88/Agentic/tree/main/examples/sandbox)
for common languages; copy and customize one in the target repository.

```json
{
  "security_mode": "isolated",
  "sandbox": {
    "enabled": true,
    "engine": "docker",
    "image": "your-project-agentic-runner:latest",
    "dockerfile": "Dockerfile.agentic",
    "build_context": ".",
    "build_network": false,
    "allow_network": false,
    "pass_env": ["PROVIDER_API_KEY"]
  }
}
```

Run `agentic doctor` after configuring the image. If the engine, image, or
required executable is unavailable, isolated execution stops rather than
falling back to host execution.
