import unittest
from pathlib import Path

from scripts.benchmark import run_case


class BenchmarkTests(unittest.TestCase):
    def test_missing_executable_is_a_failed_case(self):
        result = run_case(
            Path.cwd(),
            {"name": "missing", "command": ["agentic-command-that-does-not-exist"]},
            timeout=1,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 127)


if __name__ == "__main__":
    unittest.main()
