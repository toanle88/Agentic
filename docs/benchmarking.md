# Offline benchmarking

The benchmark harness runs deterministic argv-array cases without provider
credentials. This is suitable for fake providers, fixture repositories, and
regression checks:

```json
[
  {"name": "unit checks", "command": ["python", "-m", "unittest", "discover", "-s", "tests", "-q"]},
  {"name": "compile", "command": ["python", "-m", "py_compile", "agentic.py"]}
]
```

Run it with:

```bash
python scripts/benchmark.py --cases benchmark-cases.json --output benchmark.json
```

Results include pass/fail/timeout status, wall time, exit code, and a bounded
output tail. Live-provider benchmarks should be opt-in and should add provider
usage records from `run.json`; do not put credentials or full prompts into the
benchmark artifact. Reviewer-quality accuracy requires a labeled corpus and
should not be inferred from this offline harness alone.

## Agent-quality benchmarking

Use `scripts/agent_benchmark.py` for an opt-in benchmark of real coding-agent
workflows. It keeps the deterministic harness above credential-free and uses
disposable copies of repository fixtures for every scenario.

The benchmark configuration contains labeled tasks and two required scenario
adapters: `agent-alone` is the baseline, and `agentic` runs the full
developer/check/reviewer workflow. A task keeps its prompt in a separate file
and declares the checks and any known review defects:

```json
{
  "schema_version": 1,
  "tasks": [
    {
      "id": "add-subtract",
      "name": "Add subtract with regression coverage",
      "repository": "tasks/add-subtract/repository",
      "task_file": "tasks/add-subtract/task.md",
      "checks": [["python", "-m", "unittest", "discover", "-s", "tests", "-q"]],
      "defects": [
        {"id": "missing-negative-case", "description": "Review target for the labeled corpus"}
      ]
    }
  ],
  "runners": {
    "agent-alone": ["python", "adapters/alone.py", "{repo}", "{task_file}", "{result_file}"],
    "agentic": ["python", "adapters/agentic.py", "{repo}", "{task_file}", "{state_dir}", "{result_file}"]
  }
}
```

Scenario commands are argv arrays; they are never passed through a shell. The
runner receives `{benchmark_root}`, `{repo}`, `{reviewer}`, `{result_file}`,
`{scenario}`, `{state_dir}`, `{task_file}`, and `{task_id}` placeholders plus the
same values as `AGENTIC_BENCHMARK_*` environment variables. It must leave the
candidate patch in `{repo}` and
write a normalized result such as:

```json
{
  "status": "success",
  "approved": true,
  "attempts": 1,
  "interventions": 0,
  "recovered": false,
  "review": {"approved": true, "finding_ids": ["missing-negative-case"]},
  "usage": {"total_tokens": 1200, "estimated_cost_usd": 0.03}
}
```

Validate a corpus without running providers:

```bash
python scripts/agent_benchmark.py benchmarks/agent-quality.example.json --validate-only
```

Run live scenarios only when credentials and provider adapters are ready:

```bash
python scripts/agent_benchmark.py benchmarks/agent-quality.example.json \
  --live --output benchmark-results.json
```

The output reports completion, check-pass, approval, first-pass approval,
defect recall, false-positive rate, retries, interventions, recovery,
duration, tokens, and observed cost for each scenario. Prompts, provider
output, and credentials are intentionally excluded from the result artifact.
The `--live` flag is mandatory for execution; without it the command only
validates configuration.

### Comparing reviewer providers

To compare reviewer providers on the same candidate, add a `review_repository`
fixture to each task and at least two entries under `reviewers`:

```json
{
  "tasks": [{
    "id": "add-subtract",
    "repository": "tasks/add-subtract/repository",
    "review_repository": "tasks/add-subtract/candidate",
    "task_file": "tasks/add-subtract/task.md",
    "checks": [["python", "-m", "unittest", "discover", "-s", "tests", "-q"]]
  }],
  "reviewers": {
    "codex": ["python", "adapters/reviewer.py", "{repo}", "{task_file}", "{reviewer}", "{result_file}"],
    "claude": ["python", "adapters/reviewer.py", "{repo}", "{task_file}", "{reviewer}", "{result_file}"]
  }
}
```

Each provider receives an independent disposable copy of the same
`review_repository` fixture, the same task file, and the same declared checks.
Provider adapters should report labeled findings through
`review.finding_ids`; they must not include prompts, credentials, or raw
provider output in the normalized result. Results are grouped under
`reviewer_comparison.providers` and `scenarios` as `reviewer:<provider>`.
The harness reports the metrics needed for comparison but does not pick a
winner, since accuracy, latency, intervention rate, and cost may have
different priorities.
