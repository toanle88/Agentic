# Benchmark adapters

The example benchmark configuration expects two local adapters:

- `agent-alone.py` runs one provider directly against `{repo}`;
- `agentic.py` runs the Agentic workflow against `{repo}` and `{state_dir}`.
- reviewer adapters configured under `reviewers` inspect a task's
  `review_repository`; configure at least two provider commands for a
  cross-provider comparison.

Adapters are intentionally not included because provider authentication,
models, sandbox images, and budgets are environment-specific. Each adapter
must write the normalized JSON result to the `{result_file}` argument and
leave its final candidate patch in the supplied repository. See
[`docs/benchmarking.md`](../../docs/benchmarking.md) for the result contract.
Reviewer adapters also receive `{reviewer}` and `{scenario}` and should report
labeled findings through `review.finding_ids` so defect recall and false
positive rate can be compared across providers.
