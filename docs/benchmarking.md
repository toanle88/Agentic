# Offline benchmarking

## Performance baselines

Issue execution performance is compared using the reproducible corpus and
budgets in [`audits/2026-10-21-issue-73.md`](audits/2026-10-21-issue-73.md). Record p50, p95, p99,
peak resident memory, Python version, operating system, CPU, filesystem type,
and repetition count for dashboard collection/rendering, log processing,
OpenRouter file tools and Git operations. Use three cold
and three warm repetitions on the same host. A result is a regression when
p95 is more than 20% slower or peak memory is more than 10% higher than the
same-host baseline; investigate before changing a safety boundary.

The quality benchmark below measures normalized workflow outcomes, not these
performance budgets. Provider calls and external services are never required
for the offline performance or schema checks.

The dashboard corpus and measurement path are reproducible with the standard
library helper below. It generates 500 run folders, 24 artifacts per folder,
and one 8 MiB developer artifact per folder, then records three cold and three
warm samples with p50, p95, p99, peak resident memory, and host metadata:

```bash
python scripts/dashboard_benchmark.py --logs /tmp/agentic-dashboard-corpus \
  --generate --output /tmp/agentic-dashboard-results.json
```

Use `--logs` without `--generate` to measure an existing corpus. The helper
clears only the in-process dashboard snapshot cache between cold samples and
does not invoke providers or network services. Filesystem type is reported as
unknown when the standard library cannot identify it; record it from the host
when comparing results across filesystems.

The benchmark harness runs deterministic argv-array cases without provider
credentials. This is suitable for fake providers, fixture repositories, and
regression checks:

```json
[
  {"name": "unit checks", "command": ["python", "-m", "unittest", "discover", "-s", "tests", "-q"]},
  {"name": "compile", "command": ["python", "-m", "py_compile", "src/agentic_runner/cli.py"]}
]
```

Run it with:

```bash
python scripts/benchmark.py --cases benchmark-cases.json --output benchmark.json
```

Results include pass/fail/timeout status, wall time, exit code, and a bounded
output tail. The legacy harness records only bounded command metadata
(executable name and argv count), never literal argv values, and redacts
credential-like values from output. Live-provider benchmarks should be opt-in and should add provider
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
  "repetitions": 3,
  "runners": {
    "agent-alone": ["python", "adapters/agent-alone.py"],
    "agentic": ["python", "adapters/agentic.py"]
  }
}
```

`repetitions` defaults to `1` and may be set from `1` through `20`. Use more
than one repetition for live provider comparisons because model behavior,
review findings, retries, and cost can vary between runs. Repetitions use
fresh disposable repositories and are reported individually as well as in the
aggregate metrics.

Scenario commands are argv arrays; they are never passed through a shell. The
runner receives `{benchmark_root}`, `{repo}`, `{reviewer}`, `{result_file}`,
`{scenario}`, `{state_dir}`, `{task_file}`, `{task_id}`, and `{repetition}`
placeholders plus the same values as `AGENTIC_BENCHMARK_*` environment
variables. It must leave the candidate patch in `{repo}` and
write a normalized result such as:

Each scenario and configured check runs in its own process group/session. A
timeout terminates the complete group (using the Windows `taskkill /T`
fallback on Windows) before the disposable repository is removed, preventing
descendants from writing into a cleaned-up fixture.

```json
{
  "status": "success",
  "completed": true,
  "approved": true,
  "attempts": 1,
  "interventions": 0,
  "recovered": false,
  "review": {"approved": true, "finding_ids": ["missing-negative-case"]},
  "usage": {"total_tokens": 1200, "estimated_cost_usd": 0.03}
}
```

The adapter's boolean `completed` field is authoritative when present. An
explicit `completed: false` is normalized as a failed scenario even when the
status, exit code, and configured checks otherwise indicate success. Adapters
should always emit this field; older adapters that omit it retain the
compatibility fallback that infers completion from a successful status and
zero exit code.

Scenario result files are read through a single descriptor and are limited to
256,000 bytes. Missing, malformed, non-object, or oversized results become a
bounded `runner-error` record.

Validate a corpus without running providers:

```bash
python scripts/agent_benchmark.py benchmarks/agent-quality.example.json --validate-only
```

Run live scenarios only when credentials and provider commands are ready:

```bash
python scripts/agent_benchmark.py benchmarks/agent-quality.example.json \
  --live --output benchmark-results.json
```

The output reports completion, check-pass, approval, first-pass approval,
defect recall, false-positive rate, retries, interventions, recovery,
duration, tokens, and observed cost for each scenario. It also includes
`task_metrics`, preserving each task's name and the same metrics grouped by
scenario so a multi-issue corpus cannot hide weak performance on individual
tasks. Prompts, provider output, and credentials are intentionally excluded
from the result artifact.
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

### Supplying provider commands

The checked-in adapters under `benchmarks/adapters/` are deliberately thin.
They accept provider-specific commands through environment variables so the
benchmark corpus stays free of credentials, local paths, and model-specific
assumptions. Commands are JSON argv arrays and are never passed through a
shell. For example:

```bash
export AGENTIC_BENCHMARK_ALONE_COMMAND='["python","/path/to/provider_adapter.py","--repo","{repo}","--task-file","{task_file}","--result-file","{result_file}"]'
export AGENTIC_BENCHMARK_AGENTIC_COMMAND='["python","/path/to/agentic.py","--repo","{repo}","--task-file","{task_file}","--state-dir","{state_dir}","--config","/path/to/benchmark-agent_config.json"]'
export AGENTIC_BENCHMARK_REVIEWER_COMMAND='["python","/path/to/reviewer_adapter.py","--repo","{repo}","--task-file","{task_file}","--result-file","{result_file}"]'
```

The commands must be provider-aware if they need to report attempts,
interventions, review findings, token usage, or cost. A wrapper fallback result
records only whether the command completed successfully. Keep live benchmark
runs isolated, use disposable repositories, and pass only the credentials
required by the selected provider. The benchmark is an evaluation harness,
not an operating-system security boundary.

The repository also includes a deterministic workflow profile for validating
the harness without credentials. It intentionally does not measure model
quality:

```bash
python scripts/agent_benchmark.py benchmarks/agent-quality.offline.json \
  --live --output /tmp/agentic-offline-benchmark.json
```

This exercises baseline and Agentic scenarios, labeled reviewer findings,
sanitized result aggregation, retries, recovery, and reviewer comparison.
