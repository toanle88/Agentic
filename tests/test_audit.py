import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tests  # noqa: F401
import agentic
from agentic_runner import dashboard_data
from agentic_runner.dashboard import render_dashboard
from agentic_runner import workflow as audit_workflow
from agentic_runner.models import Issue, Run
from agentic_runner.shell import AgenticShell
from agentic_runner.shell_commands import parse_audit_command


class AuditTests(unittest.TestCase):
    def test_audit_is_a_distinct_cli_and_work_item_identity(self):
        args = agentic.build_parser().parse_args(["audit"])
        self.assertEqual(args.command, "audit")
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        self.assertTrue(issue.is_audit)
        self.assertFalse(issue.is_local)
        self.assertEqual(issue.reference, "AUDIT 01234567")

    def test_audit_report_is_bounded_and_persisted_from_readonly_provider(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        report = {
            "summary": "Repository needs focused hardening.",
            "findings": [
                {
                    "id": "SEC-1",
                    "category": "security",
                    "severity": "HIGH",
                    "title": "Unsafe boundary",
                    "description": "Input crosses a privileged boundary.",
                    "evidence": ["src/runner.py:10"],
                    "recommendation": "Validate before execution.",
                },
            ],
            "measurements": [{"name": "benchmark", "value": "12ms", "method": "existing log"}],
            "skipped": ["External service unavailable"],
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(
                audit_workflow, "run_agent", return_value=(True, json.dumps(report))
            ) as run_agent:
                result = audit_workflow.audit_repository(
                    Path(temp), issue, folder, agent={"provider": "codex"}
                )
            self.assertEqual(result["audit_key"], issue.audit_key)
            self.assertEqual(result["findings"][0]["severity"], "HIGH")
            run_agent.assert_called_once()
            self.assertTrue(run_agent.call_args.kwargs["readonly"])
            self.assertEqual(run_agent.call_args.kwargs["schema"].name, "audit-schema.json")
            persisted = json.loads((folder / "audit-report.json").read_text())
            self.assertEqual(persisted["schema_version"], 1)
            self.assertLess(len(json.dumps(persisted)), 20_000)

    def test_audit_resume_uses_cached_report_without_second_provider_turn(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        run = Run(None, "agent/audit-0123456789ab", Path("/tmp/audit"), audit_key=issue.audit_key)
        config = {"reviewer_agent": {"provider": "codex"}, "effective_reviewer_timeout_seconds": 10}
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "audit-report.json").write_text(json.dumps({
                "schema_version": 1,
                "audit_key": issue.audit_key,
                "summary": "cached",
                "findings": [],
                "measurements": [],
                "skipped": [],
            }))
            with patch.object(audit_workflow, "audit_repository") as provider:
                result = audit_workflow._run_audit(run, issue, config, folder)
            self.assertEqual(result["summary"], "cached")
            provider.assert_not_called()

    def test_dashboard_labels_and_exposes_audit_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "agent-audit-0123456789ab"
            folder.mkdir()
            (folder / "audit-report.json").write_text(json.dumps({
                "audit_key": "0123456789ab",
                "summary": "Audit summary",
                "findings": [{"category": "performance", "severity": "HIGH", "title": "Slow path"}],
                "measurements": [],
                "skipped": [],
            }))
            snapshot = dashboard_data.collect_runs(root)[0]
            self.assertTrue(snapshot["audit"])
            self.assertEqual(snapshot["label"], "Repository audit")
            self.assertEqual(snapshot["stage"], "audit")
            self.assertEqual(snapshot["severity"], "HIGH")
            self.assertEqual(snapshot["audit_report"]["summary"], "Audit summary")

    def test_dashboard_keeps_audit_identity_in_generated_views_and_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "agent-audit-0123456789ab-deadbeef"
            folder.mkdir()
            (folder / "audit-report.json").write_text(json.dumps({
                "audit_key": "0123456789ab",
                "summary": "Audit summary",
                "findings": [],
                "measurements": [],
                "skipped": [],
            }))
            snapshot = dashboard_data.collect_runs(root)[0]
            self.assertEqual(snapshot["branch"], "agent/audit-0123456789ab-deadbeef")
            html = render_dashboard([snapshot], "repo", "logs")
            self.assertIn('run.audit?"audit":run.issue==null?"local"', html)
            self.assertIn("run.audit_report?.summary", html)

    def test_shell_audit_parser_preserves_safe_argv_values(self):
        flags = parse_audit_command(["/audit", "--instruction", "--publish", "--new"])
        self.assertEqual(flags, ["--instruction", "--publish", "--new"])
        with self.assertRaises(ValueError):
            parse_audit_command(["/audit", "--review", "--publish"])

    def test_interactive_shell_dispatches_audit_as_a_runner_subcommand(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = AgenticShell(root, root / "agentic.py", session_dir=root / "shell")
            with patch.object(shell, "_launch") as launch:
                self.assertTrue(shell.execute("/audit --instruction 'review --publish'") )
            argv = launch.call_args.args[0]
            self.assertIn("audit", argv)
            self.assertIn("--instruction=review --publish", argv)


if __name__ == "__main__":
    unittest.main()
