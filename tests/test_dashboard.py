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
            self.assertIn("persistence", snapshot["review"]["summary"])
            self.assertEqual(snapshot["review"]["recurring"][0]["count"], 2)

    def test_review_parser_accepts_json_code_fence(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "agent-issue-8-cafebabe"
            run.mkdir(parents=True)
            (run / "review-001.json").write_text(
                '```json\n{"approved": true, "summary": "Looks good", "findings": []}\n```'
            )
            snapshot = agentic_dashboard.collect_runs(run.parent)
            self.assertEqual(snapshot[0]["review"]["status"], "approved")

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


if __name__ == "__main__":
    unittest.main()
