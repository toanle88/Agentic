# Provider authorization matrix

Agentic validates one provider-neutral permission policy before dispatching a
developer, reviewer, or investigator turn. The policy fields are
`read_files`, `search_files`, `edit_files`, `create_files`, `git_read`,
`run_checks`, `network`, and `allow_dangerous`.

The matrix below describes the effective command boundary. A cell marked
“reject” means the adapter fails closed during configuration or command
construction; it does not silently approximate the requested capability.

## Common capability fields

| Policy field | Developer, trusted or isolated | Reviewer/investigator | Enforcement rule |
| --- | --- | --- | --- |
| `read_files` | May be enabled | May be enabled | Providers expose only policy-approved file reads; protected paths remain hidden |
| `search_files` | May be enabled | May be enabled | Search/listing tools remain bounded and worktree-scoped |
| `edit_files` | Developer only | Reject | Native projections and OpenRouter edits are separately scoped |
| `create_files` | Developer only | Reject | Native projections and OpenRouter writes are separately scoped |
| `git_read` | May be enabled | May be enabled | Git operations use the provider-specific read-only allowlist |
| `run_checks` | Developer only | Reject | OpenRouter requires an exact configured argv, except its safe compatibility check |
| `network` | Explicit opt-in; still subject to provider support | Reject by default | OpenRouter additionally requires its network setting and an exact configured network command |
| `allow_dangerous` | Reject in trusted mode; isolated mode only | Reject | Must be explicitly configured and represented by the adapter's isolated escape hatch |

| Provider | Trusted developer | Isolated developer | Reviewer/investigator | Exact configured checks |
| --- | --- | --- | --- | --- |
| OpenRouter | Inspection tools and the safe `python -m unittest` compatibility path only; other commands are rejected | Same boundary, with `allow_dangerous=true` permitting the explicitly requested generic argv inside the isolated boundary | File inspection and read-only Git only; no edits, checks, network, or dangerous commands | Exact argv matches are allowed when `run_checks=true`, including absolute executable paths explicitly present in configuration |
| Codex | Read/search/Git are required; writes and checks are coupled; network and dangerous permissions are rejected | Same native capability contract; isolation is enforced by the sandbox | Read-only provider turn | Represented by the provider sandbox where supported; unsupported combinations reject configuration |
| Gemini | File tools are scoped by policy; configured checks and safe Git inspection use explicit tool patterns; unrestricted shell is unavailable | `allow_dangerous` may select the isolated unrestricted mode | Plan/read-only tools; no writes or checks | Added as explicit shell-tool patterns; no arbitrary shell fallback |
| Claude | Read/search/edit/create and configured command patterns use `dontAsk`; dangerous mode is unavailable in trusted mode | `allow_dangerous` may select `--dangerously-skip-permissions` | `dontAsk` with read-only tools | Added as exact permission patterns; unmatched shell commands remain denied |
| Copilot | File tools are scoped to the worktree; developer shell remains disabled; independent edit/create and network combinations that cannot be represented reject | `allow_dangerous` may enable the explicit all-tools/all-paths mode | File inspection tools only; no shell | Checks remain Agentic-owned argv execution rather than prompt-capable Copilot shell |

`allow_dangerous` is never an implicit grant. Shared policy validation requires
`security_mode: isolated`, and native adapters either translate it to their
provider-specific isolated escape hatch or reject it. OpenRouter applies the
same check at its command boundary: trusted mode cannot use the dangerous
escape hatch, and a command without that explicit escape hatch must be an
exact configured check.

The contract is covered by the OpenRouter command tests in
`tests/test_security_boundaries.py` and `tests/test_openrouter.py`, and by
native translation/representability tests in `tests/test_provider_module.py`.
