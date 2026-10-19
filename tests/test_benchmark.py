import json
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.agent_benchmark import (
    aggregate_metrics,
    load_benchmark,
    render_command,
    run_benchmark,
    validate_benchmark_files,
)
from scripts.benchmark import run_case


class BenchmarkTests(unittest.TestCase):
    def test_checked_in_deterministic_quality_profile_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "benchmarks" / "agent-quality.offline.json"
        config = load_benchmark(path)
        validate_benchmark_files(config)
        self.assertEqual(len(config["tasks"]), 1)
        self.assertEqual(set(config["reviewers"]), {"deterministic-a", "deterministic-b"})

    def test_missing_executable_is_a_failed_case(self):
        result = run_case(
            Path.cwd(),
            {"name": "missing", "command": ["agentic-command-that-does-not-exist"]},
            timeout=1,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 127)

    def test_quality_benchmark_requires_both_scenarios_and_safe_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "fixture").mkdir()
            (root / "task.md").write_text("task\n", encoding="utf-8")
            valid = {
                "schema_version": 1,
                "repetitions": 2,
                "tasks": [{
                    "id": "task-one",
                    "repository": "fixture",
                    "task_file": "task.md",
                    "checks": [["python", "-c", "pass"]],
                }],
                "runners": {
                    "agent-alone": ["python", "adapter.py", "{result_file}"],
                    "agentic": ["python", "adapter.py", "{result_file}"],
                },
            }
            path = root / "benchmark.json"
            path.write_text(json.dumps(valid), encoding="utf-8")
            loaded = load_benchmark(path)
            self.assertEqual(len(loaded["tasks"]), 1)
            self.assertEqual(set(loaded["runners"]), {"agent-alone", "agentic"})
            self.assertEqual(loaded["repetitions"], 2)
            self.assertEqual(
                render_command(["adapter", "{repo}", "{task_id}", "{reviewer}", "{scenario}"], {
                    "benchmark_root": "/tmp/benchmark",
                    "repo": "/tmp/repo", "task_id": "task-one",
                    "reviewer": "codex", "scenario": "reviewer:codex",
                    "task_file": "/tmp/task.md", "result_file": "/tmp/result.json",
                    "state_dir": "/tmp/state",
                }),
                ["adapter", "/tmp/repo", "task-one", "codex", "reviewer:codex"],
            )

            invalid = dict(valid)
            invalid["tasks"] = [dict(valid["tasks"][0], repository="../outside")]
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "repository"):
                load_benchmark(path)

            invalid_reviewers = dict(valid)
            invalid_reviewers["reviewers"] = {
                "codex": ["python", "adapter.py", "{result_file}"],
            }
            path.write_text(json.dumps(invalid_reviewers), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at least two"):
                load_benchmark(path)

    def test_quality_benchmark_rejects_symlinked_paths_outside_root(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            outside_root = Path(outside)
            (outside_root / "repo").mkdir()
            (outside_root / "task.md").write_text("task\n", encoding="utf-8")
            try:
                (root / "link").symlink_to(outside_root, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            config = {
                "schema_version": 1,
                "tasks": [{
                    "id": "task-one",
                    "repository": "link/repo",
                    "task_file": "link/task.md",
                    "checks": [["python", "-c", "pass"]],
                }],
                "runners": {
                    "agent-alone": ["python", "adapter.py"],
                    "agentic": ["python", "adapter.py"],
                },
            }
            path = root / "benchmark.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not use symlinks"):
                validate_benchmark_files(load_benchmark(path))

    def test_render_command_preserves_literal_braces(self):
        values = {
            "benchmark_root": "/tmp/benchmark", "repo": "/tmp/repo",
            "reviewer": "codex", "scenario": "agentic", "state_dir": "/tmp/state",
            "task_file": "/tmp/task.md", "result_file": "/tmp/result.json",
            "task_id": "task-one",
        }
        self.assertEqual(
            render_command(["python", "-c", "print({'x': 1})", "{repo}"], values),
            ["python", "-c", "print({'x': 1})", "/tmp/repo"],
        )

    def test_quality_benchmark_collects_sanitized_quality_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "README.md").write_text("fixture\n", encoding="utf-8")
            candidate = root / "candidate"
            candidate.mkdir()
            (candidate / "README.md").write_text("candidate\n", encoding="utf-8")
            (root / "task.md").write_text("private task prompt\n", encoding="utf-8")
            adapter = root / "adapter.py"
            adapter.write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "repo = Path(os.environ['AGENTIC_BENCHMARK_REPO'])\n"
                "expected = 'candidate' if os.environ['AGENTIC_BENCHMARK_SCENARIO'].startswith('reviewer:') else 'fixture'\n"
                "assert (repo / 'README.md').read_text(encoding='utf-8').strip() == expected\n"
                "(repo / 'answer.txt').write_text('done\\n', encoding='utf-8')\n"
                "Path(sys.argv[1]).write_text(json.dumps({\n"
                "  'status': 'success', 'approved': True, 'attempts': 1,\n"
                "  'review': {'approved': True, 'finding_ids': ['known-defect']},\n"
                "  'usage': {'total_tokens': 10, 'estimated_cost_usd': 0.25}\n"
                "}), encoding='utf-8')\n"
                "print('private task prompt and secret output')\n",
                encoding="utf-8",
            )
            config = {
                "schema_version": 1,
                "tasks": [{
                    "id": "task-one",
                    "name": "A labeled task",
                    "repository": "fixture",
                    "review_repository": "candidate",
                    "task_file": "task.md",
                    "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('answer.txt').is_file()"]],
                    "defects": [{"id": "known-defect", "description": "A labeled review target"}],
                }],
                "runners": {
                    "agent-alone": [sys.executable, str(adapter), "{result_file}"],
                    "agentic": [sys.executable, str(adapter), "{result_file}"],
                },
                "reviewers": {
                    "codex": [sys.executable, str(adapter), "{result_file}"],
                    "claude": [sys.executable, str(adapter), "{result_file}"],
                },
            }
            benchmark = root / "benchmark.json"
            benchmark.write_text(json.dumps(config), encoding="utf-8")
            payload = run_benchmark(benchmark, timeout=30)

        self.assertEqual(payload["summary"], {"total": 4, "passed": 4, "failed": 0})
        self.assertEqual(payload["scenarios"]["agentic"]["defect_recall"], 1.0)
        self.assertEqual(payload["scenarios"]["agentic"]["false_positive_rate"], 0.0)
        self.assertEqual(payload["scenarios"]["agentic"]["total_tokens_observed"], 10)
        self.assertTrue(payload["reviewer_comparison"]["same_candidate_fixture"])
        self.assertEqual(
            set(payload["reviewer_comparison"]["providers"]), {"codex", "claude"}
        )
        self.assertEqual(payload["scenarios"]["reviewer:codex"]["defect_recall"], 1.0)
        self.assertEqual(payload["task_metrics"]["task-one"]["name"], "A labeled task")
        self.assertEqual(
            payload["task_metrics"]["task-one"]["scenarios"]["agentic"]["runs"], 1,
        )
        self.assertNotIn("private task prompt", json.dumps(payload))
        self.assertNotIn("secret output", json.dumps(payload))
        self.assertNotIn("command", payload["results"][0]["checks"][0])

    def test_quality_metrics_leave_unavailable_review_metrics_unknown(self):
        metrics = aggregate_metrics([{
            "completed": True,
            "checks_passed": True,
            "approved": None,
            "first_pass_approved": None,
            "attempts": 1,
            "interventions": 0,
            "recovered": None,
            "duration_seconds": 1,
            "defects_total": 0,
            "defects_caught": 0,
            "finding_total": 0,
            "false_positives": 0,
            "usage": {"total_tokens": None, "estimated_cost_usd": None},
        }])
        self.assertIsNone(metrics["approval_rate"])
        self.assertIsNone(metrics["first_pass_approval_rate"])

    def test_repeated_quality_scenarios_get_distinct_repetition_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "README.md").write_text("fixture\n", encoding="utf-8")
            adapter = root / "adapter.py"
            adapter.write_text(
                "import json, os, sys\n"
                "assert sys.argv[1] == os.environ['AGENTIC_BENCHMARK_REPETITION']\n"
                "from pathlib import Path\n"
                "Path(os.environ['AGENTIC_BENCHMARK_RESULT_FILE']).write_text("
                "json.dumps({'status': 'success', 'completed': True}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            config = {
                "schema_version": 1,
                "repetitions": 2,
                "tasks": [{
                    "id": "task-one", "repository": "fixture", "task_file": "task.md",
                    "checks": [[sys.executable, "-c", "pass"]],
                }],
                "runners": {
                    "agent-alone": [sys.executable, str(adapter), "{repetition}"],
                    "agentic": [sys.executable, str(adapter), "{repetition}"],
                },
            }
            (root / "task.md").write_text("task\n", encoding="utf-8")
            benchmark = root / "benchmark.json"
            benchmark.write_text(json.dumps(config), encoding="utf-8")
            payload = run_benchmark(benchmark, timeout=30)

        self.assertEqual(payload["summary"], {"total": 4, "passed": 4, "failed": 0})
        records = payload["results"]
        self.assertEqual({item["repetition"] for item in records}, {1, 2})


if __name__ == "__main__":
    unittest.main()
