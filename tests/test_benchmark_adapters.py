import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.adapters.common import run_configured_adapter


class BenchmarkAdapterTests(unittest.TestCase):
    def test_adapter_renders_placeholders_and_preserves_normalized_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            result_file = root / "result.json"
            command = [
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "from pathlib import Path; "
                    "Path(os.environ['AGENTIC_BENCHMARK_RESULT_FILE']).write_text("
                    "json.dumps({'status': 'success', 'attempts': 2}) )"
                ),
                "{result_file}",
            ]
            environment = {
                "ADAPTER_COMMAND": json.dumps(command),
                "AGENTIC_BENCHMARK_REPO": str(repo),
                "AGENTIC_BENCHMARK_RESULT_FILE": str(result_file),
                "AGENTIC_BENCHMARK_TASK_FILE": str(root / "task.md"),
                "AGENTIC_BENCHMARK_REVIEWER": "codex",
                "AGENTIC_BENCHMARK_SCENARIO": "agent-alone",
                "AGENTIC_BENCHMARK_TASK_ID": "task-one",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(run_configured_adapter("ADAPTER_COMMAND"), 0)
            self.assertEqual(json.loads(result_file.read_text()), {"status": "success", "attempts": 2})

    def test_adapter_writes_failure_result_for_missing_command(self):
        with tempfile.TemporaryDirectory() as temp:
            result_file = Path(temp) / "result.json"
            with patch.dict(
                os.environ,
                {"AGENTIC_BENCHMARK_RESULT_FILE": str(result_file)},
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "ADAPTER_COMMAND is required"):
                    run_configured_adapter("ADAPTER_COMMAND")


if __name__ == "__main__":
    unittest.main()
