import json
import tempfile
import unittest
from pathlib import Path

from agentic_telemetry import combine_usage, extract_jsonl_usage, read_run_usage, record_usage


class TelemetryTests(unittest.TestCase):
    def test_extracts_provider_usage_without_guessing_cost(self):
        usage = extract_jsonl_usage(
            json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 4}}),
            provider="openrouter",
        )
        self.assertEqual(usage["total_tokens"], 14)
        self.assertIsNone(usage["estimated_cost_usd"])

    def test_preserves_unknown_token_breakdowns(self):
        usage = combine_usage([{"provider": "provider", "total_tokens": 14}])
        self.assertIsNone(usage["prompt_tokens"])
        self.assertIsNone(usage["completion_tokens"])
        self.assertEqual(usage["total_tokens"], 14)

    def test_record_usage_is_additive_and_backward_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_usage(folder, "developer", combine_usage([{
                "provider": "codex", "prompt_tokens": 10, "completion_tokens": 5,
            }]))
            record_usage(folder, "reviewer", combine_usage([{
                "provider": "openrouter", "prompt_tokens": 3, "completion_tokens": 2,
                "estimated_cost_usd": 0.01,
            }]))
            usage = read_run_usage(folder)
            self.assertEqual(usage["total_tokens"], 20)
            self.assertEqual(usage["estimated_cost_usd"], 0.01)
            self.assertEqual(json.loads((folder / "run.json").read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
