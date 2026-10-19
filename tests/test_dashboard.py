import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic_dashboard


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
                {"findings": [{"fingerprint": "same", "severity": "HIGH", "text": "Fix wiring"}]},
                {"findings": [{"fingerprint": "same", "severity": "HIGH", "text": "Fix wiring"}]},
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
                    "prompt_tokens": 10, "completion_tokens": 15,
                    "total_tokens": 25,
                }, {
                    "name": "reviewer-001", "role": "reviewer",
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
            self.assertIn("Latest recorded policy", html)
            self.assertIn("Token usage by run", html)
            self.assertIn("Known totals for each provider invocation", html)
            self.assertIn("The highest run number is the latest", html)
            self.assertIn("const runNumber = index + 1", html)
            self.assertIn('latest ? " · latest"', html)
            self.assertIn("const entries = [...DATA.runs].reverse().flatMap", html)
            self.assertIn("<th>Run</th><th>Invocation</th>", html)
            self.assertIn("const invocationRows = invocations.map", html)
            self.assertIn("Tokens: ${esc(formatTokens(tokenValue(run)))}</span>", html)
            self.assertIn("developer-001", html)
            self.assertIn("reviewer-001", html)

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
            self.assertIn('\\u003cscript\\u003e', html)
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
            self.assertEqual(statuses[2], "tests-failed")
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

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-7-deadbeef"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("</script><script>alert('x')</script>")
            snapshot = agentic_dashboard.collect_runs(root)
            html = agentic_dashboard.render_dashboard(snapshot, "demo", root.name)
            self.assertNotIn("</script><script>", html)
            self.assertIn("\\u003c/script\\u003e", html)

    def test_dashboard_redacts_credentials_from_log_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".demo-agent-runs"
            run = root / "agent-issue-7-deadbeef"
            run.mkdir(parents=True)
            (run / "developer-001.txt").write_text("api_key=dashboard-secret-value")
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
                {"findings": [{"fingerprint": "same", "severity": "HIGH", "text": "Fix wiring"}]},
                {"findings": [{"fingerprint": "same", "severity": "HIGH", "text": "Fix wiring"}]},
            ]))
            html = agentic_dashboard.render_dashboard(agentic_dashboard.collect_runs(root), "demo", root.name)
            self.assertIn("Recurring findings", html)
            self.assertIn("Fix wiring", html)

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
            self.assertIn("Integration service is not available", html)

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
            self.assertIn('Agentic run dashboard', output.read_text())


if __name__ == "__main__":
    unittest.main()
