# Benchmark adapters

The example benchmark configuration expects two local adapters:

- `agent-alone.py` runs one provider directly against `{repo}`;
- `agentic.py` runs the Agentic workflow against `{repo}` and `{state_dir}`.
- reviewer adapters configured under `reviewers` inspect a task's
  `review_repository`; configure at least two provider commands for a
  cross-provider comparison.

The repository includes thin, provider-neutral adapter wrappers. They do not
choose a provider or contain credentials; each wrapper reads a JSON argv array
from an environment variable and runs it without a shell:

| Scenario | Environment variable |
| --- | --- |
| `agent-alone` | `AGENTIC_BENCHMARK_ALONE_COMMAND` |
| `agentic` | `AGENTIC_BENCHMARK_AGENTIC_COMMAND` |
| reviewer | `AGENTIC_BENCHMARK_REVIEWER_COMMAND` |

Commands may use `{repo}`, `{task_file}`, `{result_file}`, `{reviewer}`,
`{scenario}`, and `{task_id}` placeholders. A provider-aware command should
write the normalized JSON result to `{result_file}` and leave its final
candidate patch in the supplied repository. If it does not write a result,
the wrapper records only command success/failure; it never copies raw provider
output into the benchmark artifact. See
[`docs/benchmarking.md`](../../docs/benchmarking.md) for the result contract.
Reviewer adapters also receive `{reviewer}` and `{scenario}` and should report
labeled findings through `review.finding_ids` so defect recall and false
positive rate can be compared across providers.
