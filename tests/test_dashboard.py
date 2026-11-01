import base64
import csv
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import io
import json
import math
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import dashboard as agentic_dashboard
from agentic_runner import dashboard_data as agentic_dashboard_data
from agentic_runner.models import StatePaths
from agentic_runner.dashboard_data import filter_runs
from agentic_runner.persistence import atomic_write_text


class DashboardTests(unittest.TestCase):
    def test_help_command_is_available(self):
        self.assertEqual(agentic_dashboard.build_parser().parse_args(['help']).command, 'help')

    def test_collect_runs_extracts_review_tests_and_developer_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-119-ab12cd34"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("Implemented the invoice slice.\n")
            (run / "developer-001.log").write_text("[10:00:00] [DEVELOPER] Finished in 3s (exit 0)\n")
            (run / "test-command-001.txt").write_text("[TESTS] Finished in 1s (exit 0)\n")
            (run / "review-001.json").write_text(json.dumps({
                "approved": False,
                "summary": "Needs persistence fixes",
                "findings": ["Use an empty JSON array instead of null."],
                "skipped": ["Browser check unavailable"],
            }))
            # Progress/event files share the review prefix but are not the
            # structured review result.
            (run / "review-001.log").write_text("[REVIEW] Finished in 1s (exit 0)\n")
            (run / "review-001.jsonl").write_text('{"type":"turn.completed"}\n')
            (run / "review-history.json").write_text(json.dumps([
                {"findings": [{"severity": "HIGH", "requirement": "Fix wiring", "text": "Fix wiring"}]},
                {"findings": [{"severity": "HIGH", "requirement": "Fix wiring", "text": "Fix wiring"}]},
            ]))
            snapshots = agentic_dashboard.collect_runs(root)
            self.assertEqual(len(snapshots), 1)
            snapshot = snapshots[0]
            self.assertEqual(snapshot["issue"], 119)
            self.assertEqual(snapshot["status"], "needs-fix")
            self.assertEqual(snapshot["tests"]["status"], "passed")
            self.assertEqual(snapshot["attempts"], 1)
            self.assertNotIn("instruction", snapshot)
            self.assertIn("persistence", snapshot["review"]["summary"])
            self.assertEqual(snapshot["review"]["skipped"], ["Browser check unavailable"])
            self.assertEqual(snapshot["review"]["recurring"][0]["count"], 2)

    def test_dashboard_uses_terminal_test_result_over_aggregate_fixture_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-260-ab12cd34"
            run.mkdir(parents=True)
            (run / "test-command-035.txt").write_text(
                "[TESTS] Finished in 11s (exit 0)\n", encoding="utf-8",
            )
            (run / "tests-035.txt").write_text(
                "fixture output: FAILED (exit 1)\n", encoding="utf-8",
            )
            (run / "run.json").write_text(json.dumps({
                "metrics": {"operations": [{
                    "role": "tests", "name": "test-command-035",
                    "status": "completed", "exit_code": 0,
                }]},
                "test_result": {"status": "passed"},
            }), encoding="utf-8")
            snapshot = agentic_dashboard_data.collect_runs(root)[0]

        self.assertEqual(snapshot["tests"]["status"], "passed")
        self.assertEqual(snapshot["tests"]["file"], "test-command-035.txt")

    def test_dashboard_uses_test_operation_for_legacy_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-261-ab12cd34"
            run.mkdir(parents=True)
            (run / "test-command-001.txt").write_text(
                "[TESTS] Finished in 1s (exit 0)\n", encoding="utf-8",
            )
            (run / "tests-001.txt").write_text(
                "fixture output: FAILED (exit 1)\n", encoding="utf-8",
            )
            (run / "run.json").write_text(json.dumps({
                "metrics": {"operations": [{
                    "role": "tests", "name": "test-command-001",
                    "status": "completed", "exit_code": 0,
                }]},
            }), encoding="utf-8")
            snapshot = agentic_dashboard_data.collect_runs(root)[0]

        self.assertEqual(snapshot["tests"]["status"], "passed")

    def test_dashboard_text_fallback_uses_terminal_command_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-262-ab12cd34"
            run.mkdir(parents=True)
            (run / "test-command-001.txt").write_text(
                "fixture output: FAILED (exit 1)\n"
                "[TESTS] Finished in 1s (exit 0)\n",
                encoding="utf-8",
            )
            snapshot = agentic_dashboard_data.collect_runs(root)[0]

        self.assertEqual(snapshot["tests"]["status"], "passed")

    def test_dashboard_bounds_run_and_artifact_corpus(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            root.mkdir(parents=True)
            for index in range(agentic_dashboard_data.MAX_DASHBOARD_RUNS + 3):
                run = root / f"agent-issue-{index}-ab12cd34"
                run.mkdir()
                for artifact in range(agentic_dashboard_data.MAX_FILES_PER_RUN + 3):
                    (run / f"developer-{artifact:03d}.log").write_text("done\n")
            snapshots = agentic_dashboard_data.collect_runs(root)
            self.assertLessEqual(
                len(snapshots), agentic_dashboard_data.MAX_DASHBOARD_RUNS,
            )
            self.assertTrue(snapshots)
            self.assertLessEqual(
                max(item["file_count"] for item in snapshots),
                agentic_dashboard_data.MAX_FILES_PER_RUN,
            )

    def test_dashboard_bounds_candidate_folder_scan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            root.mkdir(parents=True)
            for index in range(4):
                run = root / f"agent-issue-{index}-ab12cd34"
                run.mkdir()
                (run / "developer-001.log").write_text("done\n")
            with patch.object(
                agentic_dashboard_data, "MAX_DASHBOARD_CANDIDATE_FOLDERS", 2,
            ):
                snapshots = agentic_dashboard_data.collect_runs(root)
            self.assertLessEqual(len(snapshots), 2)

    def test_dashboard_preserves_active_run_older_than_folder_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            active = root / "agent-issue-130-active001"
            newer = root / "agent-issue-131-newer001"
            active.mkdir(parents=True)
            (active / "developer-001.log").write_text("Started\n", encoding="utf-8")
            newer.mkdir()
            (newer / "developer-001.log").write_text("Finished in 1s\n", encoding="utf-8")
            # Updating an existing artifact changes the run's activity without
            # changing the containing directory mtime.
            activity_time = dt.datetime.now().timestamp() + 60
            os.utime(active / "developer-001.log", (activity_time, activity_time))

            with patch.object(agentic_dashboard_data, "MAX_DASHBOARD_RUNS", 1):
                snapshots = agentic_dashboard_data.collect_runs(root)

            self.assertEqual([snapshot["id"] for snapshot in snapshots], [active.name])
            self.assertEqual(snapshots[0]["status"], "in-progress")

    def test_dashboard_bounds_large_log_reads_to_a_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-120-ab12cd34"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text(
                "prefix\n" + ("x" * (3 * 1024 * 1024)) + "TAIL-MARKER\n"
            )
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertIn("TAIL-MARKER", snapshot["developer"]["summary"])
            self.assertIn("earlier output omitted", snapshot["developer"]["summary"])
            self.assertLess(len(snapshot["developer"]["summary"]), 6_000)

    def test_dashboard_reuses_unchanged_run_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-121-ab12cd34"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("first\n")
            with patch.object(
                agentic_dashboard_data, "_run_snapshot",
                wraps=agentic_dashboard_data._run_snapshot,
            ) as snapshot:
                agentic_dashboard_data.collect_runs(root)
                agentic_dashboard_data.collect_runs(root)
                self.assertEqual(snapshot.call_count, 1)
                (run / "developer-001.txt").write_text("second\n")
                agentic_dashboard_data.collect_runs(root)
                self.assertEqual(snapshot.call_count, 2)

    def test_dashboard_ignores_symlinked_known_and_numbered_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-audit-ab12cd345678"
            outside = Path(temp) / "outside"
            run.mkdir(parents=True)
            outside.write_text(json.dumps({
                "summary": "outside audit", "findings": [],
            }), encoding="utf-8")
            try:
                (run / "audit-report.json").symlink_to(outside)
                (run / "review-001.json").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            snapshot = agentic_dashboard_data.collect_runs(root)[0]

        self.assertEqual(snapshot["audit_report"]["state"], "not-run")
        self.assertEqual(snapshot["review"]["status"], "not-run")
        self.assertNotIn("outside audit", json.dumps(snapshot))

    def test_dashboard_ignores_symlinked_run_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            root.mkdir(parents=True)
            outside = Path(temp) / "outside-run"
            outside.mkdir()
            (outside / "developer-001.txt").write_text("outside run\n")
            linked_run = root / "agent-issue-999-ab12cd34"
            try:
                linked_run.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory links unavailable: {exc}")
            self.assertEqual(agentic_dashboard_data.collect_runs(root), [])

    def test_dashboard_cache_invalidates_when_an_older_artifact_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-audit-ab12cd345678"
            run.mkdir(parents=True)
            audit = run / "audit-report.json"
            audit.write_text(json.dumps({"summary": "old", "findings": []}), encoding="utf-8")
            old_time = dt.datetime.now().timestamp() - 1_000
            os.utime(audit, (old_time, old_time))
            for index in range(agentic_dashboard_data.MAX_FILES_PER_RUN + 3):
                path = run / f"developer-{index:03d}.txt"
                path.write_text(f"developer {index}\n", encoding="utf-8")
                os.utime(path, (old_time + index + 1, old_time + index + 1))

            with patch.object(
                agentic_dashboard_data, "_run_snapshot",
                wraps=agentic_dashboard_data._run_snapshot,
            ) as snapshot:
                first = agentic_dashboard_data.collect_runs(root)[0]
                audit.write_text(json.dumps({"summary": "new", "findings": []}), encoding="utf-8")
                os.utime(audit, (old_time, old_time))
                second = agentic_dashboard_data.collect_runs(root)[0]

        self.assertEqual(first["audit_report"]["summary"], "old")
        self.assertEqual(second["audit_report"]["summary"], "new")
        self.assertEqual(snapshot.call_count, 2)

    def test_dashboard_reuses_one_bounded_folder_index_for_snapshot_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-126-ab12cd34"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("first\n")
            with (patch.object(
                agentic_dashboard_data, "_folder_metadata",
                wraps=agentic_dashboard_data._folder_metadata,
            ) as metadata, patch.object(
                agentic_dashboard_data, "_all_log_files",
                side_effect=AssertionError("snapshot rescanned the folder"),
            )):
                snapshots = agentic_dashboard_data.collect_runs(root)
            self.assertEqual(len(snapshots), 1)
            # One scan selects the folder and one revalidates the snapshot's
            # metadata after the bounded read.
            self.assertEqual(metadata.call_count, 2)

    def test_dashboard_folder_signature_uses_metadata_without_reading_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "agent-issue-124-ab12cd34"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_bytes(b"x" * (2 * 1024 * 1024))
            with patch.object(Path, "open", side_effect=AssertionError("artifact was read")):
                signature = agentic_dashboard_data._folder_signature(run)
        self.assertEqual(len(signature), 1)
        self.assertEqual(len(signature[0]), 4)

    def test_dashboard_large_folder_inventory_metadata_stays_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "agent-issue-127-ab12cd34"
            run.mkdir(parents=True)
            for index in range(agentic_dashboard_data.MAX_DASHBOARD_ENTRIES_PER_RUN + 1):
                (run / f"artifact-{index:05d}.txt").write_text(str(index), encoding="utf-8")

            metadata = agentic_dashboard_data._folder_metadata(run)

        self.assertLessEqual(len(metadata.files), agentic_dashboard_data.MAX_FILES_PER_RUN)
        self.assertLessEqual(len(metadata.signature), agentic_dashboard_data.MAX_FILES_PER_RUN + 1)
        self.assertTrue(any(item[0] == "__agentic_inventory__" for item in metadata.signature))

    def test_windows_dashboard_signature_does_not_read_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "agent-issue-125-ab12cd34"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_bytes(b"x" * (2 * 1024 * 1024))
            with (patch.object(agentic_dashboard_data.os, "name", "nt"),
                  patch.object(
                      agentic_dashboard_data, "_windows_file_change_time",
                      return_value=123, create=True,
                  ),
                  patch.object(
                      agentic_dashboard_data.os, "read",
                      side_effect=AssertionError("artifact was read"),
                  )):
                signature = agentic_dashboard_data._folder_signature(run)
        self.assertEqual(len(signature), 1)
        self.assertIn("123:", str(signature[0][3]))

    def test_dashboard_cache_serializes_concurrent_refreshes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-122-ab12cd34"
            run.mkdir(parents=True)
            marker = run / "developer-001.txt"
            marker.write_text("old\n", encoding="utf-8")
            entered = Event()
            release = Event()

            def snapshot(folder):
                value = marker.read_text(encoding="utf-8")
                entered.set()
                release.wait(timeout=2)
                return {
                    "id": folder.name,
                    "last_activity": "2026-01-01T00:00:00+00:00",
                    "developer": {"summary": value},
                }

            with patch.object(agentic_dashboard_data, "_run_snapshot", side_effect=snapshot):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(agentic_dashboard_data.collect_runs, root)
                    self.assertTrue(entered.wait(timeout=2))
                    # Windows can report identical metadata for rapid
                    # same-size writes, so make the refreshed artifact's
                    # size part of the observable cache change.
                    marker.write_text("new content\n", encoding="utf-8")
                    second = executor.submit(agentic_dashboard_data.collect_runs, root)
                    self.assertFalse(second.done())
                    release.set()
                    first.result(timeout=2)
                    latest = second.result(timeout=2)
            self.assertEqual(latest[0]["developer"]["summary"], "new content\n")

    def test_dashboard_discards_snapshot_when_folder_changes_during_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-123-ab12cd34"
            run.mkdir(parents=True)
            marker = run / "developer-001.txt"
            marker.write_text("old\n", encoding="utf-8")
            calls = 0

            def snapshot(folder):
                nonlocal calls
                calls += 1
                value = marker.read_text(encoding="utf-8")
                if calls == 1:
                    marker.write_text("new content\n", encoding="utf-8")
                return {
                    "id": folder.name,
                    "last_activity": "2026-01-01T00:00:00+00:00",
                    "developer": {"summary": value},
                }

            with patch.object(agentic_dashboard_data, "_run_snapshot", side_effect=snapshot):
                snapshots = agentic_dashboard_data.collect_runs(root)

            self.assertEqual(calls, 2)
            self.assertEqual(snapshots[0]["developer"]["summary"], "new content\n")

    def test_dashboard_exposes_run_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-12-deadbeef"
            run.mkdir(parents=True)
            (run / "run.json").write_text(json.dumps({
                "usage": {"total_tokens": 25, "estimated_cost_usd": 0.02},
                "security_policy": {
                    "mode": "isolated", "network": "none",
                    "git_metadata_mount": "ro",
                },
                "security_policy_history": [{
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                    "policy": {"mode": "isolated", "network": "none", "git_metadata_mount": "ro"},
                }, {
                    "recorded_at": "2026-01-01T00:01:00+00:00",
                    "policy": {"mode": "trusted", "network": "host", "git_metadata_mount": "host"},
                }],
                "usage_by_invocation": [{
                    "name": "developer-001", "role": "developer",
                    "recorded_at": "2026-01-01T00:02:00+00:00",
                    "prompt_tokens": 10, "completion_tokens": 15,
                    "total_tokens": 25,
                }, {
                    "name": "reviewer-001", "role": "reviewer",
                    "recorded_at": "2026-01-01T00:01:00+00:00",
                    "prompt_tokens": 4, "completion_tokens": 6,
                    "total_tokens": 10,
                }],
                "metrics": {"duration_seconds": 4.5, "operation_count": 2},
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["usage"]["metrics"]["duration_seconds"], 4.5)
            self.assertEqual(snapshot["usage"]["security_policy"]["mode"], "isolated")
            self.assertEqual(snapshot["usage"]["by_invocation"][0]["total_tokens"], 25)
            html = agentic_dashboard.render_dashboard([snapshot], "demo", root.name)
            self.assertIn("Run metrics", html)
            self.assertIn("Security policy", html)
            self.assertIn('<pre class="json-block">', html)
            self.assertIn("JSON.stringify(value,null,2)", html)
            self.assertIn("Latest recorded policy", html)
            self.assertIn("<h2>Token usage</h2>", html)
            self.assertIn("Recorded provider usage by run.", html)
            self.assertIn('class="panel token-panel"', html)
            self.assertIn('<div class="table-wrap" id="token-table"></div>', html)
            self.assertNotIn('class="panel chart-panel"', html)
            self.assertNotIn('id="token-chart"', html)
            self.assertNotIn('<span class="token-legend">', html)
            self.assertNotIn('id="token-sort"', html)
            self.assertIn(".recent-heading{padding:22px}", html)
            self.assertNotIn('<div class="detail"><h2>Recent activity', html)
            self.assertIn("<h3>Tests</h3>", html)
            self.assertNotIn("Tests · ${esc(run.tests.status)}", html)
            self.assertNotIn("Tests: ${esc(run.tests.status)}", html)
            self.assertIn("function compareTokenEntries", html)
            self.assertIn('return state.tokenSort===\"newest\"?rightTime-leftTime:leftTime-rightTime;', html)
            self.assertIn("String(left.run.id).localeCompare", html)
            self.assertIn("const entries=invocationsForRuns(tokenRuns).sort(compareTokenEntries)", html)
            self.assertIn("<th>Timestamp</th><th>Run</th><th>Invocation</th>", html)
            self.assertIn("tokenTimestampText", html)
            self.assertIn("Tokens: ${esc(formatTokens(tokenValue(run)))}</span>", html)
            payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            embedded = json.loads(base64.b64decode(payload).decode("utf-8"))
            self.assertEqual(embedded["runs"][0]["usage"]["by_invocation"][0]["name"], "developer-001")
            self.assertEqual(embedded["runs"][0]["usage"]["by_invocation"][1]["name"], "reviewer-001")

    def test_dashboard_exposes_and_escapes_per_run_instruction(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '.demo-agent-runs'
            run = root / 'agent-issue-13-deadbeef'
            run.mkdir(parents=True)
            (run / 'run.json').write_text(json.dumps({
                'instruction': "Keep <script>alert('x')</script> as data",
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot['instruction'], "Keep <script>alert('x')</script> as data")
            html = agentic_dashboard.render_dashboard([snapshot], 'demo', root.name)
            self.assertIn('Per-run instruction', html)
            payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            self.assertEqual(
                json.loads(base64.b64decode(payload).decode("utf-8"))["runs"][0]["instruction"],
                "Keep <script>alert('x')</script> as data",
            )
            self.assertNotIn("<script>alert('x')</script>", html)

    def test_review_parser_accepts_json_code_fence(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "agent-issue-8-cafebabe"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(
                '```json\n{"approved": true, "summary": "Looks good", "findings": []}\n```'
            )
            snapshot = agentic_dashboard.collect_runs(run.parent)
            self.assertEqual(snapshot[0]["review"]["status"], "approved")

    def test_invalid_review_output_is_visible_as_invalid_not_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-9-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(
                '[REVIEW] invalid JSON: Expecting comma delimiter'
            )
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["review"]["status"], "invalid")
            self.assertIn("not valid JSON", snapshot["review"]["summary"])
            self.assertEqual(snapshot["review"]["findings"], [])

    def test_blocked_run_is_visible_as_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-35-deadbeef"
            run.mkdir(parents=True)
            (run / "blocked.json").write_text(json.dumps({
                "status": "blocked",
                "reason": "Repeated review blocker with no patch change",
                "patch_digest": "same",
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["status"], "blocked")
            self.assertEqual(snapshot["blocked"]["status"], "blocked")

    def test_no_code_changes_blocker_shows_summary_and_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-36-deadbeef"
            run.mkdir(parents=True)
            (run / "blocked.json").write_text(json.dumps({
                "status": "blocked",
                "kind": "no-code-changes",
                "reason": "Developer reported a blocker without producing code changes",
                "patch_digest": "",
                "developer_summary": "The upstream service is unavailable.",
                "logs_path": str(run),
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            html = agentic_dashboard.render_dashboard([snapshot], "demo", root.name)
            payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            embedded = json.dumps(json.loads(base64.b64decode(payload).decode("utf-8")))
            self.assertIn("The upstream service is unavailable.", embedded)
            self.assertIn(json.dumps(str(run))[1:-1], embedded)

    def test_terminal_result_statuses_are_visible_and_legacy_blockers_still_parse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            exhausted = root / "agent-issue-61-deadbeef"
            exhausted.mkdir(parents=True)
            (exhausted / "result.json").write_text(json.dumps({
                "status": "RETRY_EXHAUSTED",
                "attempts": 4,
                "max_retries": 3,
                "reason": "Tests failed: assertion mismatch",
                "logs_path": str(exhausted),
                "next_action": "rerun --issue 61",
            }))
            no_changes = root / "agent-issue-62-deadbeef"
            no_changes.mkdir(parents=True)
            (no_changes / "blocked.json").write_text(json.dumps({
                "status": "blocked",
                "kind": "no-code-changes",
                "developer_summary": "Missing upstream contract.",
                "logs_path": str(no_changes),
            }))
            snapshots = {item["issue"]: item for item in agentic_dashboard.collect_runs(root)}
            self.assertEqual(snapshots[61]["status"], "retry_exhausted")
            self.assertEqual(snapshots[61]["result"]["attempts"], 4)
            self.assertEqual(snapshots[62]["status"], "no_changes")
            approved = root / "agent-issue-60-deadbeef"
            approved.mkdir(parents=True)
            (approved / "result.json").write_text(json.dumps({
                "status": "APPROVED", "next_action": "publish",
            }))
            approved_snapshot = next(
                item for item in agentic_dashboard.collect_runs(root)
                if item["issue"] == 60
            )
            self.assertEqual(approved_snapshot["status"], "approved")
            self.assertEqual(approved_snapshot["result"]["status"], "APPROVED")
            rendered = agentic_dashboard.render_dashboard(list(snapshots.values()), "demo", root.name)
            self.assertIn("Retry exhausted", rendered)
            self.assertIn("No changes", rendered)
            payload = rendered.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            self.assertIn(
                "Missing upstream contract.",
                json.dumps(json.loads(base64.b64decode(payload).decode("utf-8"))),
            )
            self.assertIn(
                'run.blocked && run.status !== "no_changes" && '
                'run.result?.status !== "NO_CHANGES"',
                rendered,
            )

    def test_malformed_terminal_result_status_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            for suffix, invalid_status in (("array", []), ("object", {})):
                run = root / f"agent-issue-63-{suffix}"
                run.mkdir(parents=True)
                (run / "developer-001.txt").write_text("Developer output\n")
                (run / "result.json").write_text(json.dumps({
                    "status": invalid_status,
                    "attempts": 1,
                }))

            snapshots = {
                item["id"]: item for item in agentic_dashboard.collect_runs(root)
            }
            self.assertEqual(snapshots["agent-issue-63-array"]["status"], "awaiting-review")
            self.assertIsNone(snapshots["agent-issue-63-array"]["result"])
            self.assertEqual(snapshots["agent-issue-63-object"]["status"], "awaiting-review")
            self.assertIsNone(snapshots["agent-issue-63-object"]["result"])

    def test_investigation_feedback_is_visible_before_development(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-10-deadbeef"
            run.mkdir(parents=True)
            (run / "investigation-001.json").write_text(json.dumps({
                "summary": "The requested behavior is already present.",
                "findings": ["Existing tests cover the reported case."],
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["status"], "awaiting-development")
            self.assertEqual(snapshot["investigation"]["state"], "recorded")
            self.assertIn("already present", snapshot["investigation"]["summary"])

    def test_dashboard_indexes_investigation_outside_display_artifact_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-110-deadbeef"
            run.mkdir(parents=True)
            (run / "investigation-001.json").write_text(json.dumps({
                "summary": "Investigation retained despite later logs.",
                "findings": ["Keep structured evidence discoverable."],
            }))
            for index in range(1, 31):
                (run / f"developer-{index:03d}.txt").write_text(
                    f"Developer attempt {index}\n",
                )

            snapshot = agentic_dashboard_data.collect_runs(root)[0]

        self.assertLessEqual(snapshot["file_count"], agentic_dashboard_data.MAX_FILES_PER_RUN)
        self.assertNotIn("investigation-001.json", {item["name"] for item in snapshot["files"]})
        self.assertEqual(snapshot["investigation"]["state"], "recorded")
        self.assertIn("retained", snapshot["investigation"]["summary"])

    def test_legacy_run_list_uses_review_summary_without_investigation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-11-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(json.dumps({
                "approved": False,
                "summary": "Review still needs a persistence fix.",
                "findings": [],
            }))
            html = agentic_dashboard.render_dashboard(
                agentic_dashboard.collect_runs(root), "demo", root.name,
            )
            self.assertIn('const investigationSummary = run.investigation.state === "not-run" ? "" : run.investigation.summary;', html)
            self.assertIn('esc(runSummary(run))', html)

    def test_dashboard_statuses_distinguish_active_tests_and_empty_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            active = root / "agent-issue-1-aaaaaaaa"
            active.mkdir(parents=True)
            (active / "developer-001.log").write_text('[DEVELOPER] Started\n')
            failed = root / "agent-issue-2-bbbbbbbb"
            failed.mkdir(parents=True)
            (failed / "test-command-001.txt").write_text('[TESTS] exit 1 FAILED\n')
            waiting = root / "agent-issue-3-cccccccc"
            waiting.mkdir(parents=True)
            (waiting / "developer-001.txt").write_text('Implementation finished\n')
            empty = root / "agent-issue-4-dddddddd"
            empty.mkdir(parents=True)
            statuses = {
                snapshot["issue"]: snapshot["status"]
                for snapshot in agentic_dashboard.collect_runs(root)
            }
            self.assertEqual(statuses[1], "in-progress")
            self.assertEqual(statuses[2], "awaiting-review")
            self.assertEqual(statuses[3], "awaiting-review")
            self.assertEqual(statuses[4], "no-activity")

    def test_collect_runs_supports_local_task_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-task-add-export-ab12cd34ef56"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("Implemented local task.\n")
            snapshots = agentic_dashboard.collect_runs(root)
            self.assertEqual(len(snapshots), 1)
            self.assertIsNone(snapshots[0]["issue"])
            self.assertEqual(snapshots[0]["label"], "Local task: add-export")
            self.assertEqual(snapshots[0]["branch"], "agent/task-add-export-ab12cd34ef56")

    def test_render_dashboard_escapes_log_content_and_supports_empty_runs(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        self.assertIn("Agentic run dashboard", html)
        self.assertIn("No runs match this filter", html)
        self.assertIn('<p id="repo-name">demo</p>', html)
        self.assertIn("No runs recorded yet.", html)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-7-deadbeef"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("</script><script>alert('x')</script>")
            snapshot = agentic_dashboard.collect_runs(root)
            html = agentic_dashboard.render_dashboard(snapshot, "demo", root.name)
            self.assertNotIn("</script><script>", html)
            payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            self.assertIn("</script><script>alert('x')</script>", json.loads(
                base64.b64decode(payload).decode("utf-8"),
            )["runs"][0]["files"][0]["content"])

    def test_render_dashboard_server_fallback_contains_recorded_runs(self):
        html = agentic_dashboard.render_dashboard([{
            "issue": 7,
            "issue_metadata": {"title": "Dashboard issue", "state": "OPEN"},
            "label": "Issue #7", "status": "approved", "stage": "completed",
            "last_activity": "2026-10-21T12:00:00+00:00",
        }], "demo", "runs")
        self.assertIn("Issue #7", html)
        self.assertIn("2026-10-21T12:00:00+00:00", html)
        self.assertIn("Dashboard issue", html)
        self.assertIn("window.__dashboardBootError", html)
        self.assertIn("Total runs", html)

    def test_render_dashboard_embeds_browser_valid_json_for_non_finite_values(self):
        html = agentic_dashboard.render_dashboard(
            [{"id": "run", "metrics": {"duration": math.nan, "limit": math.inf}}],
            "demo", ".demo-agent-runs",
        )
        payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
            "</script>", 1,
        )[0]
        data = json.loads(base64.b64decode(payload).decode("utf-8"))
        self.assertIsNone(data["runs"][0]["metrics"]["duration"])
        self.assertIsNone(data["runs"][0]["metrics"]["limit"])
        self.assertNotIn("NaN", payload)
        self.assertNotIn("Infinity", payload)

    def test_dashboard_reads_credentials_redacted_by_log_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-7-deadbeef"
            run.mkdir(parents=True)
            log_path = run / "developer-001.txt"
            atomic_write_text(log_path, "api_key=dashboard-secret-value")
            self.assertNotIn("dashboard-secret-value", log_path.read_text())
            snapshot = agentic_dashboard.collect_runs(root)
            self.assertNotIn("dashboard-secret-value", snapshot[0]["developer"]["summary"])

    def test_dashboard_renders_recurring_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-7-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(json.dumps({
                "approved": False, "summary": "Needs work", "findings": ["Fix wiring"],
            }))
            (run / "review-history.json").write_text(json.dumps([
                {"findings": [{"severity": "HIGH", "requirement": "Fix wiring", "text": "Fix wiring"}]},
                {"findings": [{"severity": "HIGH", "requirement": "Fix wiring", "text": "Fix wiring"}]},
            ]))
            html = agentic_dashboard.render_dashboard(agentic_dashboard.collect_runs(root), "demo", root.name)
            self.assertIn("Recurring findings", html)
            payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            self.assertIn(
                "Fix wiring",
                json.dumps(json.loads(base64.b64decode(payload).decode("utf-8"))),
            )

    def test_dashboard_bounds_oversized_review_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-8-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(json.dumps({
                "approved": False, "summary": "Needs work", "findings": [],
            }))
            huge = "x" * (agentic_dashboard_data.MAX_REVIEW_HISTORY_BYTES * 2)
            (run / "review-history.json").write_text(json.dumps([
                {"findings": [{"severity": "HIGH", "requirement": "repeat", "text": "repeat"}]},
                {"findings": [{"severity": "HIGH", "requirement": "repeat", "text": "repeat"}]},
                {"findings": [{"severity": "MEDIUM", "requirement": "large", "text": huge}]},
            ]))
            snapshot = agentic_dashboard.collect_runs(root)[0]
        self.assertLess(len(json.dumps(snapshot)), 50_000)
        self.assertLessEqual(
            len(snapshot["review"]["recurring"][0]["text"]),
            agentic_dashboard_data.MAX_REVIEW_HISTORY_FIELD,
        )

    def test_dashboard_template_replacement_requires_marker(self):
        with self.assertRaises(ValueError):
            agentic_dashboard._required_replace("<main></main>", "missing", "replacement")

    def test_dashboard_renders_skipped_review_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-7-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(json.dumps({
                "approved": True, "summary": "Approved with a limitation", "findings": [],
                "skipped": ["Integration service is not available"],
            }))
            html = agentic_dashboard.render_dashboard(
                agentic_dashboard.collect_runs(root), "demo", root.name,
            )
            self.assertIn("Skipped validation", html)
            payload = html.split('<script type="application/json" id="dashboard-data">', 1)[1].split(
                "</script>", 1,
            )[0]
            self.assertIn(
                "Integration service is not available",
                json.dumps(json.loads(base64.b64decode(payload).decode("utf-8"))),
            )

    def test_collect_runs_from_roots_prefers_centralized_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            centralized = root / 'centralized'
            legacy = root / 'legacy'
            for location, summary in ((centralized, 'central'), (legacy, 'legacy')):
                run = location / 'agent-issue-7-deadbeef'
                run.mkdir(parents=True)
                (run / 'review-001.json').write_text(json.dumps({
                    'approved': False, 'summary': summary, 'findings': [],
                }))
            runs = agentic_dashboard.collect_runs_from_roots([centralized, legacy])
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]['review']['summary'], 'central')

    def test_dashboard_main_writes_requested_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / 'logs'
            logs.mkdir()
            output = root / 'nested' / 'dashboard.html'
            self.assertEqual(agentic_dashboard.main([
                '--repo', str(root), '--logs', str(logs), '--output', str(output),
            ]), 0)
            self.assertTrue(output.is_file())
            rendered = output.read_text()
            self.assertIn('Agentic run dashboard', rendered)
            self.assertNotIn('[REDACTED]', rendered)

    def test_dashboard_serve_mode_rebuilds_data_for_each_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            output = root / "dashboard.html"
            with patch.object(agentic_dashboard, "_serve_dashboard") as serve:
                self.assertEqual(agentic_dashboard.main([
                    "--repo", str(root), "--logs", str(logs),
                    "--output", str(output), "--serve", "--port", "0",
                ]), 0)
            serve.assert_called_once()
            self.assertEqual(serve.call_args.args[0], output.resolve())
            render_current = serve.call_args.args[1]
            self.assertNotIn("Issue #73", output.read_text())
            first = render_current()
            run = logs / "agent-issue-73-deadbeef"
            run.mkdir()
            (run / "developer-001.txt").write_text("Fresh activity\n")
            second = render_current()
            self.assertIn("Issue #73", second)
            self.assertNotEqual(first, second)

    def test_dashboard_refuses_remote_binding_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            with self.assertRaisesRegex(RuntimeError, "Refusing non-loopback"):
                agentic_dashboard.main([
                    "--repo", str(root), "--logs", str(logs), "--output", str(root / "dashboard.html"),
                    "--serve", "--host", "0.0.0.0", "--port", "0",
                ])

    def test_dashboard_remote_binding_requires_and_passes_bearer_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            with patch.dict(os.environ, {"AGENTIC_DASHBOARD_TOKEN": "test-token"}), \
                 patch.object(agentic_dashboard, "_serve_dashboard") as serve:
                self.assertEqual(agentic_dashboard.main([
                    "--repo", str(root), "--logs", str(logs), "--output", str(root / "dashboard.html"),
                    "--serve", "--allow-remote", "--host", "0.0.0.0", "--port", "0",
                    "--tls-certfile", str(root / "server.crt"),
                    "--tls-keyfile", str(root / "server.key"),
                ]), 0)
            self.assertEqual(serve.call_args.kwargs["auth_token"], "test-token")
            self.assertEqual(serve.call_args.kwargs["tls_certfile"], root / "server.crt")
            self.assertEqual(serve.call_args.kwargs["tls_keyfile"], root / "server.key")

    def test_dashboard_loopback_does_not_inherit_remote_token_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            with patch.dict(os.environ, {"AGENTIC_DASHBOARD_TOKEN": "test-token"}), \
                 patch.object(agentic_dashboard, "_serve_dashboard") as serve:
                self.assertEqual(agentic_dashboard.main([
                    "--repo", str(root), "--logs", str(logs), "--output", str(root / "dashboard.html"),
                    "--serve", "--host", "127.0.0.1", "--port", "0",
                ]), 0)
            self.assertIsNone(serve.call_args.kwargs["auth_token"])

    def test_dashboard_remote_binding_requires_tls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            with patch.dict(os.environ, {"AGENTIC_DASHBOARD_TOKEN": "test-token"}), \
                 self.assertRaisesRegex(RuntimeError, "requires --tls-certfile"):
                agentic_dashboard.main([
                    "--repo", str(root), "--logs", str(logs), "--output", str(root / "dashboard.html"),
                    "--serve", "--allow-remote", "--host", "0.0.0.0", "--port", "0",
                ])

    def test_dashboard_tls_context_rejects_legacy_protocol_versions(self):
        with patch.object(agentic_dashboard.ssl, "SSLContext") as ssl_context:
            context = ssl_context.return_value
            result = agentic_dashboard._dashboard_tls_context(
                Path("server.crt"), Path("server.key"),
            )

        self.assertIs(result, context)
        ssl_context.assert_called_once_with(ssl.PROTOCOL_TLS_SERVER)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        context.load_cert_chain.assert_called_once_with(
            Path("server.crt"), Path("server.key"),
        )

    def test_dashboard_renders_navigable_views_filters_and_log_actions(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        for view in ("dashboard", "runs", "logs", "tokens", "issues", "settings"):
            self.assertIn(f'data-view="{view}"', html)
        for control in ("search", "status", "issue", "stage", "severity", "model", "provider"):
            self.assertIn(f'id="{control}"', html)
        self.assertNotIn('id="token-sort"', html)
        self.assertNotIn('id="token-chart"', html)
        self.assertIn('id="token-table"', html)
        self.assertIn('data-copy=', html)
        self.assertIn('data-download=', html)
        self.assertIn("async function refreshDashboard", html)
        self.assertIn("const searchableCache=new Map()", html)
        self.assertIn("function searchable(run){return searchableCache[run.id]||\"\"}", html)
        self.assertIn("function scheduleSearchRender()", html)
        self.assertIn("searchTimer=window.setTimeout", html)
        self.assertIn("function renderCurrentView()", html)
        self.assertIn("function renderAll(){renderCurrentView()}", html)
        self.assertIn("data-log-file=", html)
        self.assertIn("data-detail-log-file=", html)
        self.assertIn("details.dataset.loaded", html)
        self.assertIn('var searchableText=state.query===""?"":searchable(run)', html)
        self.assertNotIn("MutationObserver", html)
        self.assertNotIn("var searchable=JSON.stringify(run).toLowerCase()", html)
        self.assertIn("window.setInterval(refreshDashboard,30000)", html)
        self.assertIn(
            'dataset.theme=document.documentElement.dataset.theme==="light"?"dark":"light"',
            html,
        )
        self.assertIn('value="investigation-invalid"', html)
        self.assertIn('value="awaiting-development"', html)
        self.assertIn('value="no-activity"', html)
        for severity in ("BLOCKER", "CRITICAL", "MAJOR", "HIGH", "MEDIUM", "MINOR", "LOW"):
            self.assertIn(f'value="{severity}"', html)
        self.assertIn("Latest recorded run activity, newest first.", html)
        self.assertIn("Issue status", html)
        self.assertIn("data-issue-row", html)
        self.assertIn("const runTimestamp=run=>", html)
        self.assertIn("runTimestamp(right.runs[0])-runTimestamp(left.runs[0])", html)
        self.assertIn("function issueTimestamp(run)", html)
        self.assertIn("Next action", html)
        self.assertIn("suggestedCommand", html)

    def test_dashboard_detail_renders_terminal_block_reason(self):
        html = agentic_dashboard.render_dashboard([{
            "id": "blocked-run",
            "label": "Issue #35",
            "status": "blocked",
            "result": {
                "status": "BLOCKED",
                "reason": "Repeated identical test failure",
            },
        }], "demo", ".demo-agent-runs")

        self.assertIn("Blocked reason", html)
        self.assertIn("renderTerminalBlockReason", html)
        self.assertIn("blocked-reason", html)

    def test_dashboard_token_sort_contract_uses_timestamp_and_run_id_tie_breaker(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        self.assertIn("recorded_at||invocation.timestamp||invocation.finished_at", html)
        self.assertIn("operations.find(operation=>operation&&operation.name===invocation.name)", html)
        self.assertIn("state.tokenSort===\"newest\"?-1:1", html)
        self.assertIn("document.querySelector(\"#token-sort\").addEventListener", html)

    def test_dashboard_token_alignment_and_bounded_view_pagination_contract(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        self.assertIn(".numeric{font-variant-numeric:tabular-nums;text-align:right}", html)
        self.assertIn('<th class="numeric">Tokens</th>', html)
        self.assertIn('<td class="numeric">', html)
        self.assertIn("var PAGE_SIZE=25", html)
        self.assertIn("function pageItems(items,view)", html)
        self.assertIn("function pagination(view,total,shown)", html)
        self.assertIn("data-load-more=\"'+view+'\"", html)
        for view in ("runs", "logs", "tokens", "issues"):
            self.assertIn(f'pagination("{view}"', html)
        self.assertIn("resetPages()", html)
        self.assertIn('state.pages[loadMore.getAttribute("data-load-more")]+=1', html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the dashboard JavaScript regression")
    def test_dashboard_token_sort_orders_newest_and_ties_by_run_id(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        start = html.index("function invocationTimestamp")
        end = html.index("function renderTokenChart", start)
        helpers = html[start:end]
        harness = f"""
globalThis.state = {{tokenSort: "newest"}};
{helpers}
const records = [
  {{run: {{id: "issue-1", last_activity: "2026-01-01T00:00:00Z"}}, invocation: {{name: "a", recorded_at: "2026-01-01T00:02:00Z"}}}},
  {{run: {{id: "issue-2", last_activity: "2026-01-01T00:00:00Z"}}, invocation: {{name: "b", recorded_at: "2026-01-01T00:01:00Z"}}}},
  {{run: {{id: "issue-3", last_activity: "2026-01-01T00:00:00Z"}}, invocation: {{name: "c", recorded_at: "2026-01-01T00:01:00Z"}}}}
];
state.tokenSort = "newest";
const newest = records.slice().sort(compareTokenEntries).map(item => item.run.id);
state.tokenSort = "oldest";
const oldest = records.slice().sort(compareTokenEntries).map(item => item.run.id);
console.log(JSON.stringify({{newest, oldest}}));
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {"newest": ["issue-1", "issue-3", "issue-2"],
             "oldest": ["issue-2", "issue-3", "issue-1"]},
        )

    def test_dashboard_filter_contract_matches_and_excludes_runs(self):
        runs = [
            {
                "id": "issue-1", "issue": 1, "status": "needs-fix",
                "stage": "review", "severity": "HIGH",
                "models": ["model-a"], "providers": ["provider-a"],
                "review": {"summary": "database timeout"},
                "files": [{"content": "connection error"}],
            },
            {
                "id": "issue-2", "issue": 2, "status": "approved",
                "stage": "completed", "severity": "NONE",
                "models": ["model-b"], "providers": ["provider-b"],
                "review": {"summary": "ready"},
                "files": [{"content": "all checks passed"}],
            },
            {
                "id": "local", "issue": None, "status": "blocked",
                "stage": "blocked", "severity": "CRITICAL",
                "models": ["model-a"], "providers": ["provider-b"],
                "review": {"summary": "blocked by database timeout"},
                "files": [{"content": "fatal error"}],
            },
        ]

        cases = [
            ({"query": "connection error"}, ["issue-1"]),
            ({"query": "ready"}, ["issue-2"]),
            ({"status": "approved"}, ["issue-2"]),
            ({"issue": "1"}, ["issue-1"]),
            ({"stage": "blocked"}, ["local"]),
            ({"severity": "HIGH"}, ["issue-1"]),
            ({"model": "model-b"}, ["issue-2"]),
            ({"provider": "provider-a"}, ["issue-1"]),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.assertEqual(
                    [run["id"] for run in filter_runs(runs, **filters)], expected,
                )
        self.assertEqual(
            [run["id"] for run in filter_runs(runs, query="not present")], [],
        )

    def test_dashboard_extended_filters_sort_and_bounded_exports(self):
        runs = [
            {
                "id": "slow", "repository": "repo-a", "workflow_mode": "standard",
                "publication_state": "pending", "action_required": True,
                "action_reasons": ["pending publication"], "last_activity": "2026-01-02T00:00:00+00:00",
                "usage": {"total_tokens": 20, "estimated_cost_usd": 0.4,
                          "metrics": {"duration_seconds": 8},
                          "workflow_metrics": {"retries": 2, "review_findings": 3}},
            },
            {
                "id": "fast", "repository": "repo-b", "workflow_mode": "quick",
                "publication_state": "published", "action_required": False,
                "last_activity": "2026-01-01T00:00:00+00:00",
                "usage": {"total_tokens": 10, "estimated_cost_usd": 0.1,
                          "metrics": {"duration_seconds": 2},
                          "workflow_metrics": {"retries": 0, "review_findings": 0}},
            },
        ]
        self.assertEqual(
            [run["id"] for run in filter_runs(
                runs, repository="repo-a", workflow_mode="standard",
                publication_state="pending", action_required="yes",
                date_from="2026-01-02", date_to="2026-01-02",
            )],
            ["slow"],
        )
        self.assertEqual(
            [run["id"] for run in agentic_dashboard_data.sort_runs(runs, sort_by="duration")],
            ["slow", "fast"],
        )
        exported = agentic_dashboard_data.export_summary_json(runs)
        self.assertLessEqual(len(exported.encode("utf-8")), 256_000)
        self.assertNotIn('"files"', exported)
        self.assertNotIn('"instruction"', exported)
        self.assertEqual(set(json.loads(exported)[0]), {
            "id", "repository", "issue", "label", "status", "stage", "severity",
            "action_required", "action_reasons", "workflow_mode", "publication_state",
            "review_status", "tests_status", "last_activity", "duration_seconds",
            "total_tokens", "estimated_cost_usd", "retries", "review_findings",
            "first_pass_approval", "worktree_state",
        })
        csv_text = agentic_dashboard_data.export_summary_csv(runs)
        self.assertIn("action_reasons", csv_text)
        self.assertLessEqual(len(csv_text.encode("utf-8")), 256_000)

    def test_dashboard_sort_handles_missing_and_malformed_timestamps(self):
        runs = [
            {"id": "valid", "last_activity": "2026-01-03T00:00:00+00:00"},
            {"id": "malformed", "last_activity": "not-a-timestamp"},
            {"id": "missing"},
        ]
        self.assertEqual(
            [run["id"] for run in agentic_dashboard_data.sort_runs(runs)],
            ["valid", "malformed", "missing"],
        )

    def test_dashboard_csv_budget_preserves_quoted_rows(self):
        runs = [
            {
                "id": f"run-{index}",
                "label": "x" * 240,
                "action_reasons": ["reason " + ("y" * 240)],
            }
            for index in range(500)
        ]
        runs[140]["label"] = "line one\nline two"
        csv_text = agentic_dashboard_data.export_summary_csv(runs)
        rows = list(csv.reader(io.StringIO(csv_text, newline="")))
        self.assertLessEqual(len(csv_text.encode("utf-8")), 256_000)
        self.assertTrue(rows)
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))

    def test_dashboard_marks_stale_worktrees_as_action_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            run = logs / "agent-issue-76-deadbeef"
            run.mkdir()
            activity = run / "developer-001.txt"
            activity.write_text("Finished\n")
            old_timestamp = (dt.datetime.now().timestamp() - (31 * 24 * 60 * 60))
            os.utime(activity, (old_timestamp, old_timestamp))
            worktrees = root / "worktrees"
            (worktrees / "issue-76-deadbeef").mkdir(parents=True)
            state = StatePaths(root / "state", worktrees,
                               root / "runs", root / "shell", root / "dashboards")

            with (patch.object(agentic_dashboard, "state_paths", return_value=state),
                  patch.object(agentic_dashboard, "legacy_worktrees_dir",
                               return_value=root / "legacy-worktrees")):
                runs, _ = agentic_dashboard._dashboard_snapshot(
                    root, root / "state", logs, include_legacy=False,
                )

            self.assertEqual(runs[0]["worktree_state"], "stale")
            self.assertTrue(runs[0]["action_required"])
            self.assertIn("stale worktree", runs[0]["action_reasons"])

    def test_dashboard_summary_handles_incomplete_legacy_nested_fields(self):
        runs = [{
            "id": "legacy", "tests": None, "review": None,
            "usage": {"metrics": None, "workflow_metrics": None},
        }]
        row = agentic_dashboard_data.summary_row(runs[0])
        self.assertEqual(row["review_status"], "unknown")
        self.assertEqual(row["tests_status"], "unknown")
        self.assertEqual(agentic_dashboard_data.export_summary_csv(iter(runs)).count("legacy"), 1)

    def test_dashboard_records_action_and_publication_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-75-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(json.dumps({
                "approved": False, "summary": "Needs a fix", "findings": ["Fix it"],
            }))
            (run / "test-command-001.txt").write_text("FAILED exit 1\n")
            (run / "run.json").write_text(json.dumps({
                "workflow_mode": "quick", "usage": {"workflow_mode": "quick"},
            }))
            (run / "publication.json").write_text(json.dumps({"status": "failed"}))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["workflow_mode"], "quick")
            self.assertEqual(snapshot["publication_state"], "failed")
            self.assertTrue(snapshot["action_required"])
            self.assertIn("failed checks", snapshot["action_reasons"])
            self.assertIn("rejected review", snapshot["action_reasons"])

    def test_dashboard_html_exposes_action_filters_compare_and_exports(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        for marker in (
            'id="repository"', 'id="workflow-mode"', 'id="publication"',
            'id="action"', 'id="date-from"', 'id="sort"', 'data-view="compare"',
            'id="export-json"', 'id="export-csv"', 'Unavailable',
        ):
            self.assertIn(marker, html)

    def test_dashboard_preserves_issue_title_and_state_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-72-deadbeef"
            run.mkdir(parents=True)
            (run / "run.json").write_text(json.dumps({
                "issue_metadata": {
                    "number": 72,
                    "title": "Redesign dashboard navigation",
                    "state": "OPEN",
                    "source": "github",
                },
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["issue_metadata"]["title"], "Redesign dashboard navigation")
            self.assertEqual(snapshot["issue_metadata"]["state"], "OPEN")
            html = agentic_dashboard.render_dashboard([snapshot], "demo", root.name)
            self.assertIn("Redesign dashboard navigation", html)
            self.assertIn("<td>OPEN</td>", html)

    def test_dashboard_exposes_bounded_issue_body_in_run_detail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-73-deadbeef"
            run.mkdir(parents=True)
            (run / "run.json").write_text(json.dumps({
                "issue_metadata": {
                    "number": 73,
                    "title": "Dashboard issue details",
                    "state": "OPEN",
                    "source": "github",
                },
            }))
            (run / "issue-snapshot.json").write_text(json.dumps({
                "number": 73,
                "title": "Dashboard issue details",
                "body": "Acceptance criteria\n\n- Show the issue body safely.",
                "state": "OPEN",
                "source": "github",
            }))
            snapshot = agentic_dashboard_data.collect_runs(root)[0]
            html = agentic_dashboard.render_dashboard([snapshot], "demo", root.name)
            payload = html.split(
                '<script type="application/json" id="dashboard-data">', 1,
            )[1].split("</script>", 1)[0]
            data = json.loads(base64.b64decode(payload).decode("utf-8"))

        self.assertEqual(
            data["runs"][0]["issue_body"],
            "Acceptance criteria\n\n- Show the issue body safely.",
        )
        self.assertIn("Issue details", html)

    def test_dashboard_summary_gates_audit_fallback_by_run_kind(self):
        html = agentic_dashboard.render_dashboard([], "demo", ".demo-agent-runs")
        self.assertIn(
            'const auditSummary=run.audit?run.audit_report?.summary:"";',
            html,
        )

    def test_dashboard_snapshot_normalizes_finding_severity_and_model_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-71-deadbeef"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(json.dumps({
                "approved": False,
                "summary": "Needs dashboard coverage",
                "findings": [{"severity": "high", "text": "Add a browser regression test."}],
            }))
            (run / "run.json").write_text(json.dumps({
                "usage_by_invocation": [{
                    "name": "developer-001", "provider": "openrouter",
                    "model": "openai/example", "total_tokens": 12,
                }],
            }))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["severity"], "HIGH")
            self.assertEqual(snapshot["providers"], ["openrouter"])
            self.assertEqual(snapshot["models"], ["openai/example"])
            self.assertEqual(snapshot["review"]["findings"], ["Add a browser regression test."])
            self.assertEqual(snapshot["review"]["finding_records"][0]["severity"], "HIGH")

    def test_dashboard_bounds_legacy_issue_review_and_investigation_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-75-deadbeef"
            run.mkdir(parents=True)
            huge = "x" * (agentic_dashboard_data.MAX_TEXT * 3)
            (run / "issue-snapshot.json").write_text(json.dumps({"body": huge}))
            (run / "review-001.json").write_text(json.dumps({
                "approved": False, "summary": huge, "findings": [huge], "skipped": [huge],
            }))
            (run / "investigation-001.json").write_text(json.dumps({
                "summary": huge, "findings": [huge],
            }))
            snapshot = agentic_dashboard_data.collect_runs(root)[0]

        self.assertLessEqual(len(snapshot["issue_body"]), agentic_dashboard_data.MAX_TEXT)
        self.assertLessEqual(len(snapshot["review"]["summary"]), agentic_dashboard_data.MAX_TEXT)
        self.assertLessEqual(len(snapshot["review"]["findings"][0]), agentic_dashboard_data.MAX_TEXT)
        self.assertLessEqual(len(snapshot["review"]["skipped"][0]), agentic_dashboard_data.MAX_TEXT)
        self.assertLessEqual(len(snapshot["investigation"]["summary"]), agentic_dashboard_data.MAX_TEXT)
        self.assertLessEqual(len(snapshot["investigation"]["findings"][0]), agentic_dashboard_data.MAX_TEXT)

    def test_dashboard_render_payload_has_an_aggregate_size_bound(self):
        runs = [{
            "id": f"run-{index}", "label": "x" * 5_000, "status": "approved",
            "files": [{"name": "log.txt", "content": "y" * 5_000}],
        } for index in range(500)]
        html = agentic_dashboard.render_dashboard(runs, "demo", "logs")
        payload = html.split(
            '<script type="application/json" id="dashboard-data">', 1,
        )[1].split("</script>", 1)[0]
        decoded = base64.b64decode(payload)
        self.assertLessEqual(
            len(decoded), agentic_dashboard.MAX_DASHBOARD_PAYLOAD_BYTES,
        )

    def test_dashboard_render_bounds_direct_envelope_inputs(self):
        html = agentic_dashboard.render_dashboard(
            [{"id": "run-1", "status": "approved"}],
            "r" * 900_000,
            "l" * 900_000,
            settings={"note": "s" * 900_000},
        )
        payload = html.split(
            '<script type="application/json" id="dashboard-data">', 1,
        )[1].split("</script>", 1)[0]
        decoded = base64.b64decode(payload)
        data = json.loads(decoded)

        self.assertLessEqual(
            len(decoded), agentic_dashboard.MAX_DASHBOARD_PAYLOAD_BYTES,
        )
        self.assertLessEqual(len(data["repo"]), agentic_dashboard.MAX_DASHBOARD_VALUE_CHARS)
        self.assertLessEqual(len(data["logs"]), agentic_dashboard.MAX_DASHBOARD_VALUE_CHARS)
        self.assertLessEqual(
            len(data["settings"]["note"]), agentic_dashboard.MAX_DASHBOARD_VALUE_CHARS,
        )

    def test_dashboard_parses_legacy_finding_severity_and_blocked_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            blocked = root / "agent-issue-74-deadbeef"
            blocked.mkdir(parents=True)
            (blocked / "review-001.json").write_text(json.dumps({
                "approved": False,
                "summary": "Blocking review findings",
                "findings": [
                    "P0: Preserve the read-only boundary.",
                    "[MINOR] Improve copy.",
                    "P3: Improve copy.",
                ],
            }))
            (blocked / "blocked.json").write_text(json.dumps({"status": "blocked"}))
            (blocked / "result.json").write_text(json.dumps({"status": "BLOCKED"}))
            snapshot = agentic_dashboard.collect_runs(root)[0]
            self.assertEqual(snapshot["severity"], "BLOCKER")
            self.assertEqual(snapshot["review"]["finding_records"][0]["severity"], "BLOCKER")
            self.assertEqual(snapshot["review"]["finding_records"][1]["severity"], "MINOR")
            self.assertEqual(snapshot["review"]["finding_records"][2]["severity"], "LOW")
            self.assertEqual(snapshot["stage"], "blocked")
            self.assertEqual(
                [run["id"] for run in filter_runs([snapshot], severity="BLOCKER")],
                [snapshot["id"]],
            )
            self.assertEqual(
                [run["id"] for run in filter_runs([snapshot], stage="blocked")],
                [snapshot["id"]],
            )

    def test_dashboard_server_does_not_expose_sibling_files(self):
        render_current = Mock(return_value="<html>dashboard</html>")
        handler_type = agentic_dashboard._dashboard_handler("dashboard.html", render_current)
        handler = handler_type.__new__(handler_type)
        handler.path = "/secret.txt"
        with patch.object(handler, "send_error") as send_error:
            handler.do_GET()
        send_error.assert_called_once_with(404, "Not found")
        render_current.assert_not_called()

    def test_dashboard_server_checks_bearer_token_when_configured(self):
        render_current = Mock(return_value="<html>dashboard</html>")
        handler_type = agentic_dashboard._dashboard_handler(
            "dashboard.html", render_current, "test-token",
        )
        handler = handler_type.__new__(handler_type)
        handler.path = "/dashboard.html"
        handler.headers = {"Authorization": "Bearer wrong-token"}
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.do_GET()
        handler.send_response.assert_called_once_with(401)
        render_current.assert_not_called()

    def test_dashboard_server_accepts_browser_basic_auth_when_configured(self):
        render_current = Mock(return_value="<html>dashboard</html>")
        handler_type = agentic_dashboard._dashboard_handler(
            "dashboard.html", render_current, "test-token",
        )
        handler = handler_type.__new__(handler_type)
        handler.path = "/dashboard.html"
        handler.headers = {"Authorization": "Basic YWdlbnRpYzp0ZXN0LXRva2Vu"}
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        render_current.assert_called_once_with()
        self.assertTrue(any(
            call.args[0] == "Content-Security-Policy"
            and "connect-src 'self'" in call.args[1]
            for call in handler.send_header.call_args_list
        ))

    def test_dashboard_links_state_and_legacy_worktrees_from_branch_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            (logs / "agent-issue-71-deadbeef").mkdir()
            (logs / "agent-issue-72-cafebabe").mkdir()
            state_worktrees = root / "state-worktrees"
            legacy_worktrees = root / "legacy-worktrees"
            (state_worktrees / "issue-71-deadbeef").mkdir(parents=True)
            (legacy_worktrees / "issue-72-cafebabe").mkdir(parents=True)
            state = StatePaths(root / "state", state_worktrees,
                               root / "runs", root / "shell", root / "dashboards")

            with (patch.object(agentic_dashboard, "state_paths", return_value=state),
                  patch.object(agentic_dashboard, "legacy_worktrees_dir",
                               return_value=legacy_worktrees)):
                runs, _ = agentic_dashboard._dashboard_snapshot(
                    root, root / "state", logs, include_legacy=False,
                )

            linked = {run["issue"]: run["worktree"] for run in runs}
            self.assertEqual(linked[71], str((state_worktrees / "issue-71-deadbeef").resolve()))
            self.assertEqual(linked[72], str((legacy_worktrees / "issue-72-cafebabe").resolve()))


if __name__ == "__main__":
    unittest.main()
