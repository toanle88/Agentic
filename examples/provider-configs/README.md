# Provider configuration templates

Copy one template to the target repository as `agent_config.json`, then
replace the starter `test_commands` with the target project's real checks:

```bash
cp examples/provider-configs/agent_config.gemini.json /path/to/project/agent_config.json
```

Available templates:

| Provider | Template |
| --- | --- |
| Codex | `agent_config.codex.json` |
| Gemini | `agent_config.gemini.json` |
| Claude Code | `agent_config.claude.json` |
| GitHub Copilot | `agent_config.copilot.json` |
| OpenRouter | `agent_config.openrouter.json` |

Or generate one directly from the target repository:

```bash
agentic init --provider gemini
agentic init --provider claude
agentic init --provider copilot
agentic init --provider openrouter
# Optional: use a different provider for review.
agentic init --provider codex --reviewer-provider openrouter
```

The OpenRouter template expects `OPENROUTER_API_KEY` in the environment. Do
not copy an API key into the JSON file. `agent_config.json` is personal and
should remain ignored by Git.
