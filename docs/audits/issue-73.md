# Issue #73 quality, security, and performance audit

Status: implemented in this change where the finding was a bounded,
backward-compatible hardening fix; remaining work is tracked below as focused
follow-ups. Audit baseline: `origin/main` at `985d7a1` plus this patch.

## Audit contract

This audit treats a finding as high priority when it can permit command or
credential exposure, violate the read-only reviewer boundary, corrupt durable
state, or make a normal run fail on input that is merely large. A performance
regression is actionable when the same-host p95 is more than 20% slower than
the recorded baseline or peak memory exceeds the workload budget. Measurements
must identify Python version, OS, filesystem, CPU, memory, and whether the
repository is on local or network storage.

The audit artifact is this file. Follow-ups are recorded as issue-sized
proposals rather than created remotely: this run is explicitly offline and is
not authorized to modify GitHub issues.

## Findings and disposition

| ID | Finding | Disposition |
| --- | --- | --- |
| Q-01 | The issue lacked acceptance and audit contracts. | Fixed by this artifact, including priority, workload, threshold, and follow-up rules. |
| Q-02 | Several compatibility-facing modules remain oversized. | Follow-up Q-02: extract `agentic_shell_input.py`, `agentic_workflow.py`, and `agentic_shell.py` behind characterization tests; preserve `agentic.py` wrappers. |
| Q-03 | CI compiled only an older subset and omitted `git diff --check`. | Fixed: CI, coverage, release, and development documentation now use the complete runtime module set; CI also runs the whitespace check. |
| Q-04 | Dashboard processing reread unbounded logs and rebuilt unchanged snapshots. | Fixed: bounded tail reads, one file index per snapshot, and bounded metadata-keyed snapshot caching; regression coverage includes large logs and repeated collection. |
| Q-05 | OpenRouter inspection scaled poorly on large trees/files. | Fixed: paginated traversal with protected-file filtering on every page, bounded reads, and streaming protected-file hashing; regression coverage includes continuation pages and large artifacts. |
| Q-06 | No reproducible performance corpus or comparison policy existed. | Fixed by the baseline contract below and `docs/benchmarking.md`; live provider measurements remain opt-in. |
| Q-07 | Sandbox image/build inputs needed adversarial hardening. | Fixed for the identified injection risk: image references reject option-like values. Configured builds preserve functional dependency installation by default with `build_network: true`, while allowing an explicit `build_network: false` hardening policy. Dockerfile execution remains an explicit trusted-host boundary and is documented as such. |
| Q-08 | Secret redaction and sensitive-file policy are heuristic. | Fixed as a documented threat model and expanded canary coverage; heuristic redaction is not claimed to be complete secret discovery. |
| Q-09 | Reviewer command-policy coverage needed adversarial cases. | Existing boundary tests plus this audit cover argv-only execution, path checks, restricted network, image option rejection, and read-only mounts. Additional provider-specific policy remains a follow-up if a new provider is added. |
| Q-10 | GitHub/Git freshness and call-count performance were unmeasured. | Follow-up Q-10: profile `refresh_base`, issue listing, and issue selection with mocked call-count/latency tests before introducing freshness-sensitive caching. |

## Security audit result

- Issue text, repository guidance, logs, and provider output remain bounded and
  delimited evidence; they are not converted into shell code.
- Process execution continues to use argv arrays. Reviewer commands and
  OpenRouter tools retain their read-only policy and path containment checks.
- Sandbox builds are host operations over repository-controlled Dockerfiles;
  they are not a security boundary from the Docker/Podman daemon. Builds use
  the engine's default network unless `build_network: false` is configured,
  and credentials are not passed to them. Users should use a trusted checkout
  and review Dockerfiles before enabling isolated mode.
- Redaction protects common bearer/key-value formats and configured sensitive
  environment values. It does not guarantee detection of arbitrary secrets;
  trusted checkouts, provider isolation, and least-privilege environments are
  still required.

## Performance baseline and comparison corpus

The reproducible offline corpus is:

1. 500 valid run folders, each with 24 bounded artifacts and one 8 MiB log;
2. a worktree containing 10,000 files in 1,000 directories, including 301
   readable files and representative protected names; and
3. 1,000 tracked/untracked files for Git status snapshots.

Record cold and warm p50/p95/p99 duration and peak RSS for dashboard
collection/rendering, OpenRouter listing/reading, and Git snapshot operations.
The initial budgets are dashboard collection p95 <= 2 s
and peak RSS <= 256 MiB, OpenRouter capped listing p95 <= 1 s and peak RSS <=
128 MiB on a local SSD. These are engineering budgets, not claims about every
host. Compare
three repetitions on the same host; investigate any >20% p95 or >10% peak-RSS
regression, and do not trade away path or secret safety to meet a budget.

The existing benchmark suite validates result schema and normalization only.
It is not performance evidence. Provider-quality and network-dependent runs
remain opt-in and must not be used as an offline gate.

## Follow-up proposals

- **Q-02 — Modular extraction:** split the remaining oversized shell/workflow
  components with import-boundary characterization tests.
- **Q-10 — Git/API profiling:** collect mocked subprocess/API call counts and
  same-host latency distributions, then evaluate conditional fetches or
  short-lived caches without weakening issue freshness.
- **Q-11 — Secret corpus expansion:** add provider-specific fixtures and
  crash/partial-write simulations for every durable artifact format.
