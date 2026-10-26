import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.agent_benchmark import (
    aggregate_metrics,
    load_benchmark,
    render_command,
    run_scenario as run_scenario_facade,
    run_benchmark,
    validate_benchmark_files,
)
from scripts.agent_benchmark_config import load_benchmark as load_benchmark_config
from scripts.agent_benchmark_scenarios import (
    MAX_RESULT_BYTES, _read_result, _run_bounded_git_command, _run_checks,
    _run_git, _scenario_record, run_scenario,
)
from scripts.agent_benchmark_scenarios import _benchmark_environment
from scripts.benchmark import run_case
from scripts.benchmark_execution import process_group_kwargs
from scripts.dashboard_benchmark import generate_corpus, measure, percentile


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_git_output_is_bounded_before_materialization(self):
        class FakeProcess:
            def __init__(self):
                self.stdout = io.BytesIO(b"too much")
                self.killed = False

            def kill(self):
                self.killed = True

            def wait(self, **_kwargs):
                return 0

        process = FakeProcess()
        with patch(
            "scripts.agent_benchmark_scenarios.subprocess.Popen",
            return_value=process,
        ), patch(
            "scripts.agent_benchmark_scenarios.MAX_GIT_OUTPUT_BYTES", 4,
        ):
            with self.assertRaisesRegex(RuntimeError, "safety limit"):
                _run_bounded_git_command(Path.cwd(), {}, ["config"])
        self.assertTrue(process.killed)

    def test_benchmark_git_timeout_does_not_wait_for_a_stuck_output_reader(self):
        release_reader = threading.Event()

        class BlockingStream:
            def read(self, _size):
                release_reader.wait(timeout=2)
                return b""

            def close(self):
                release_reader.set()

        class FakeProcess:
            def __init__(self):
                self.stdout = BlockingStream()
                self.killed = False

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(["git"], timeout)
                return -9

        process = FakeProcess()
        with patch(
            "scripts.agent_benchmark_scenarios.subprocess.Popen",
            return_value=process,
        ), patch(
            "scripts.agent_benchmark_scenarios.GIT_COMMAND_TIMEOUT_SECONDS", 0.01,
        ):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "Timed out"):
                _run_bounded_git_command(Path.cwd(), {}, ["status"])
        self.assertTrue(process.killed)
        self.assertLess(time.monotonic() - started, 1)

    def test_benchmark_environment_requires_explicit_credential_passthrough(self):
        secret = "benchmark-short"
        with patch.dict(
            os.environ,
            {"PATH": os.environ.get("PATH", ""), "UNRELATED_TOKEN": secret},
            clear=True,
        ):
            safe = _benchmark_environment({"AGENTIC_BENCHMARK_REPO": "/tmp/repo"})
            self.assertNotIn("UNRELATED_TOKEN", safe)
            with patch.dict(os.environ, {"AGENTIC_BENCHMARK_PASSTHROUGH_ENV": "UNRELATED_TOKEN"}):
                self.assertEqual(_benchmark_environment({})["UNRELATED_TOKEN"], secret)

    def test_benchmark_git_uses_restricted_environment(self):
        captured = []

        def fake_git_command(repo, environment, args):
            captured.append(environment)
            return (1, "") if args[0] == "config" else (0, "")

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {
                "PATH": os.environ.get("PATH", ""),
                "UNRELATED_TOKEN": "benchmark-secret",
            },
            clear=True,
        ), patch(
            "scripts.agent_benchmark_scenarios._run_bounded_git_command",
            side_effect=fake_git_command,
        ):
            repo = Path(temp)
            (repo / ".git").mkdir()
            _run_git(repo, "status")

        self.assertEqual(len(captured), 2)
        self.assertNotIn("UNRELATED_TOKEN", captured[0])
        self.assertEqual(captured[0]["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_benchmark_git_does_not_read_global_filter_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            global_config = root / "global.gitconfig"
            marker = root / "filter-ran"
            filter_script = root / "filter.py"
            filter_script.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                f"Path({str(marker)!r}).write_text('ran')\n"
                "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            clean_environment = os.environ.copy()
            clean_environment.update({
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            })
            subprocess.run(
                ["git", "init", "--initial-branch", "main"], cwd=repo,
                env=clean_environment, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(
                [
                    "git", "config", "--file", str(global_config),
                    "filter.malicious.clean", f"{sys.executable} {filter_script}",
                ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            (repo / ".gitattributes").write_text(
                "payload.txt filter=malicious\n", encoding="utf-8",
            )
            (repo / "payload.txt").write_text("safe\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"GIT_CONFIG_GLOBAL": str(global_config)}, clear=False,
            ):
                _run_git(repo, "add", "-A")
            self.assertFalse(marker.exists())

    def test_scenario_result_read_is_bounded_and_preserves_normal_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_text(json.dumps({"status": "success"}), encoding="utf-8")
            self.assertEqual(_read_result(path), {"status": "success"})

    def test_oversized_scenario_result_is_a_bounded_runner_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_text(
                json.dumps({"status": "success", "padding": "x" * MAX_RESULT_BYTES}),
                encoding="utf-8",
            )
            result = _read_result(path)
        self.assertEqual(result["status"], "runner-error")
        self.assertEqual(
            result["error"], f"scenario result exceeds {MAX_RESULT_BYTES}-byte limit",
        )

    def test_malformed_scenario_result_is_a_runner_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_text("{not-json", encoding="utf-8")
            result = _read_result(path)
        self.assertEqual(result["status"], "runner-error")
        self.assertIn("invalid scenario result:", result["error"])

    def test_invalid_utf8_scenario_result_is_a_runner_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            path.write_bytes(b'{"status":"success","detail":"\xff"}')
            result = _read_result(path)
        self.assertEqual(result["status"], "runner-error")
        self.assertIn("invalid scenario result:", result["error"])

    def test_dashboard_benchmark_generates_and_measures_cold_and_warm_samples(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / "dashboard-corpus"
            generate_corpus(logs, runs=3, artifacts=2, developer_bytes=128)
            result = measure(logs, repetitions=3)

        self.assertEqual(result["repetitions"], 3)
        for phase in ("cold", "warm"):
            self.assertEqual(
                set(result[phase]), {"p50_seconds", "p95_seconds", "p99_seconds"}
            )
        self.assertIn("python", result["host"])
        self.assertEqual(percentile([1.0, 2.0, 4.0], 0.5), 2.0)
        self.assertEqual(
            set(result["cases"]),
            {
                "dashboard_collection", "dashboard_rendering",
                "openrouter_file_tools", "git_operations",
            },
        )
        self.assertEqual(result["cases"]["openrouter_file_tools"]["status"], "measured")

    def test_benchmark_facade_preserves_split_component_contract(self):
        self.assertIs(load_benchmark, load_benchmark_config)
        self.assertIs(run_scenario, run_scenario_facade)

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

    def test_legacy_benchmark_redacts_argv_and_output(self):
        secret = "benchmark-secret-canary-123456"
        result = run_case(
            Path.cwd(),
            {
                "name": "secret",
                "command": [sys.executable, "-c", f"print({secret!r})", secret],
            },
            timeout=5,
        )
        serialized = json.dumps(result)
        self.assertEqual(result["status"], "passed")
        self.assertNotIn(secret, serialized)
        self.assertEqual(result["command"]["argv_count"], 4)
        self.assertEqual(result["command"]["executable"], Path(sys.executable).name)

    def test_legacy_benchmark_timeout_terminates_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-alive"
            child = (
                "import pathlib,time; time.sleep(0.5); "
                f"pathlib.Path({str(marker)!r}).write_text('alive')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            )
            result = run_case(
                Path(temp), {"name": "timeout", "command": [sys.executable, "-c", parent]},
                timeout=1,
            )
            time.sleep(0.8)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(marker.exists())

    def test_benchmark_checks_terminate_process_group_on_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-alive"
            child = (
                "import pathlib,time; time.sleep(0.5); "
                f"pathlib.Path({str(marker)!r}).write_text('alive')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
            )
            passed, records = _run_checks(
                Path(temp), [[sys.executable, '-c', parent]], timeout=1,
            )
            time.sleep(0.8)
        self.assertFalse(passed)
        self.assertEqual(records[0]["status"], "timeout")
        self.assertFalse(marker.exists())

    def test_benchmark_process_group_uses_windows_creation_flag_fallback(self):
        with patch('scripts.benchmark_execution.os.name', 'nt'), \
             patch('subprocess.CREATE_NEW_PROCESS_GROUP', 512, create=True):
            self.assertEqual(process_group_kwargs(), {"creationflags": 512})

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

    def test_explicit_incomplete_scenario_cannot_be_reported_as_passed(self):
        task = {
            "id": "task-one", "name": "Incomplete task",
            "defects": [],
        }
        record = _scenario_record(
            task, "agentic", {"status": "success", "completed": False}, 0,
            1.0, True, [], None, 1,
        )

        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["completed"])
        self.assertEqual(aggregate_metrics([record])["completed"], 0)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "fixture").mkdir()
            (root / "fixture" / "README.md").write_text("fixture\n", encoding="utf-8")
            (root / "task.md").write_text("task\n", encoding="utf-8")
            adapter = root / "adapter.py"
            adapter.write_text(
                "import json, os\n"
                "from pathlib import Path\n"
                "Path(os.environ['AGENTIC_BENCHMARK_RESULT_FILE']).write_text("
                "json.dumps({'status': 'success', 'completed': False}), "
                "encoding='utf-8')\n",
                encoding="utf-8",
            )
            benchmark = root / "benchmark.json"
            benchmark.write_text(json.dumps({
                "schema_version": 1,
                "tasks": [{
                    "id": "task-one", "repository": "fixture", "task_file": "task.md",
                    "checks": [[sys.executable, "-c", "pass"]],
                }],
                "runners": {
                    "agent-alone": [sys.executable, str(adapter)],
                    "agentic": [sys.executable, str(adapter)],
                },
            }), encoding="utf-8")
            payload = run_benchmark(benchmark, timeout=30)

        self.assertEqual(payload["summary"], {"total": 2, "passed": 0, "failed": 2})
        self.assertEqual(payload["scenarios"]["agentic"]["completed"], 0)

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
