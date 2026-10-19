import json
import tempfile
import unittest
from pathlib import Path

from agentic_telemetry import (
    combine_usage,
    extract_jsonl_usage,
    read_run_usage,
    record_operation,
    record_usage,
)


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

    def test_record_usage_keeps_invocation_metadata_without_prompt_text(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_usage(
                folder,
                "developer",
                {"provider": "openrouter", "prompt_tokens": 12, "completion_tokens": 3},
                invocation={"name": "developer-001", "prompt_chars": 8000, "turns": 4},
            )
            raw = json.loads((folder / "run.json").read_text())
            self.assertEqual(raw["usage_by_invocation"][0]["prompt_chars"], 8000)
            self.assertEqual(raw["usage_by_invocation"][0]["turns"], 4)
            self.assertNotIn("full prompt", json.dumps(raw["usage_by_invocation"][0]))
            self.assertEqual(read_run_usage(folder)["by_invocation"][0]["name"], "developer-001")

    def test_record_operation_persists_duration_and_status_without_command_text(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_operation(
                folder, "tests", "test-command-001",
                started_at="2026-01-01T00:00:00.000+00:00",
                finished_at="2026-01-01T00:00:02.500+00:00",
                duration_seconds=2.5, status="completed", exit_code=0,
            )
            record_operation(
                folder, "developer", "developer-001",
                started_at="2026-01-01T00:00:03.000+00:00",
                finished_at="2026-01-01T00:00:05.000+00:00",
                duration_seconds=2, status="failed", exit_code=1,
            )
            metrics = read_run_usage(folder)["metrics"]
            self.assertEqual(metrics["duration_seconds"], 5.0)
            self.assertEqual(metrics["operation_count"], 2)
            self.assertEqual(metrics["operations"][1]["status"], "failed")
            self.assertEqual(metrics["operations"][1]["exit_code"], 1)
            self.assertNotIn("python -c", json.dumps(metrics))


if __name__ == "__main__":
    unittest.main()
