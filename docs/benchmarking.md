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
