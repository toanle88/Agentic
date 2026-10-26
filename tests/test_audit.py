import json
import contextlib
from subprocess import CompletedProcess
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
    def setUp(self):
        # Direct audit workflow tests model the explicit consent required by
        # trusted-mode execution; trust-specific behavior is covered separately.
        self._trust_prompt = patch(
            'agentic.request_repository_trust', return_value='trusted',
        )
        self._trust_prompt.start()
        self.addCleanup(self._trust_prompt.stop)

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
            self.assertEqual(persisted["audit_key"], issue.audit_key)
            self.assertIsNone(persisted["findings"][0]["confidence"])
            self.assertIsNone(persisted["findings"][0]["measurement"])
            self.assertIsNotNone(audit_workflow.load_audit_report(folder, issue.audit_key))
            self.assertLess(len(json.dumps(persisted)), 20_000)

            # Reports written before audit_key was exempted from generic key
            # redaction remain publishable after upgrading.
            persisted["audit_key"] = "[REDACTED]"
            (folder / "audit-report.json").write_text(json.dumps(persisted))
            self.assertIsNotNone(audit_workflow.load_audit_report(folder, issue.audit_key))

    def test_audit_normalization_emits_exact_nullable_schema_shapes(self):
        report = audit_workflow._normalize_audit_report({
            "summary": "summary",
            "findings": [{
                "category": "security",
                "severity": "HIGH",
                "title": "title",
                "description": "description",
                "evidence": [],
                "recommendation": "recommendation",
                "measurement": {"name": "latency", "extra": "ignored"},
            }],
            "measurements": [{"value": 12, "extra": "ignored"}],
            "skipped": [],
        }, "audit-key")
        self.assertEqual(
            set(report["findings"][0]),
            set(audit_workflow.AUDIT_SCHEMA["properties"]["findings"]["items"]["properties"]),
        )
        self.assertIsNone(report["findings"][0]["confidence"])
        self.assertEqual(
            set(report["findings"][0]["measurement"]),
            {"name", "value", "method"},
        )
        self.assertEqual(report["findings"][0]["measurement"]["name"], "latency")
        self.assertEqual(
            report["measurements"],
            [{"name": None, "value": "12", "method": None}],
        )

    def test_audit_schema_requires_declared_finding_properties_for_strict_providers(self):
        finding_schema = audit_workflow.AUDIT_SCHEMA["properties"]["findings"]["items"]
        self.assertEqual(
            set(finding_schema["required"]),
            set(finding_schema["properties"]),
        )
        measurement_schema = audit_workflow.AUDIT_MEASUREMENT_SCHEMA
        self.assertEqual(
            set(measurement_schema["required"]),
            set(measurement_schema["properties"]),
        )
        self.assertFalse(finding_schema["additionalProperties"])
        self.assertFalse(measurement_schema["additionalProperties"])

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

    def test_audit_command_starts_fresh_attempt_without_developer_or_checks(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        run = Run(None, "agent/audit-0123456789ab", Path("/tmp/audit"), audit_key=issue.audit_key)
        config = {
            "task_source": "github", "github_host": "github.test",
            "test_commands": [["python", "-c", "print('ok')"]],
            "developer_agent": {"provider": "codex"},
            "reviewer_agent": {"provider": "codex"},
            "security_mode": "trusted", "sandbox": {"enabled": False},
            "task_timeout_seconds": 60, "effective_test_timeout_seconds": 10,
            "effective_reviewer_timeout_seconds": 10,
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "agent_config.json").write_text("{}")
            args = agentic.build_parser().parse_args([
                "--repo", temp, "--config", str(folder / "agent_config.json"), "audit",
            ])
            with contextlib.ExitStack() as stack:
                for name in (
                    "preflight_security", "preflight_test_commands",
                    "preflight_agent_settings", "refresh_base", "record_issue_metadata",
                    "record_security_policy", "print_run_header", "_warn_dangerous_permissions",
                ):
                    stack.enter_context(patch.object(agentic, name))
                stack.enter_context(patch.object(agentic, "load_config", return_value=config))
                stack.enter_context(patch.object(agentic, "github_repo_from_remote", return_value="owner/repo"))
                stack.enter_context(patch.object(agentic, "default_base_branch", return_value="main"))
                stack.enter_context(patch.object(agentic, "runs_for_audit", return_value=[run]))
                create_audit = stack.enter_context(
                    patch.object(agentic, "create_audit_worktree", return_value=run)
                )
                stack.enter_context(patch.object(agentic, "repository_lock", return_value=contextlib.nullcontext()))
                stack.enter_context(patch.object(agentic, "repair_relative_worktree", return_value=False))
                stack.enter_context(patch.object(agentic, "logs_path", return_value=folder))
                stack.enter_context(patch.object(agentic, "load_publication_approval", return_value=None))
                stack.enter_context(patch.object(agentic, "worktree_lock", return_value=contextlib.nullcontext()))
                stack.enter_context(patch.object(agentic, "_run_audit", return_value={"summary": "recorded"}))
                run_tests = stack.enter_context(patch.object(agentic, "run_tests"))
                develop = stack.enter_context(patch.object(agentic, "develop"))
                review = stack.enter_context(patch.object(agentic, "verify_and_review"))
                self.assertEqual(agentic.execute_locked(args, Path(temp)), 0)
            run_tests.assert_not_called()
            develop.assert_not_called()
            review.assert_not_called()
            create_audit.assert_called_once()
            self.assertTrue(create_audit.call_args.kwargs["separate"])

    def test_audit_resume_option_is_explicit(self):
        args = agentic.build_parser().parse_args(["audit", "--resume"])
        self.assertTrue(args.resume)

    def test_audit_publish_rejects_worktrees_without_saved_reports(self):
        args = agentic.build_parser().parse_args(["audit", "--publish"])
        config = {
            "task_source": "github", "github_host": "github.test",
            "test_commands": [],
            "developer_agent": {"provider": "codex"},
            "reviewer_agent": {"provider": "codex"},
            "security_mode": "trusted", "sandbox": {"enabled": False},
            "task_timeout_seconds": 60, "effective_test_timeout_seconds": 10,
            "effective_reviewer_timeout_seconds": 10,
        }
        run = Run(
            None, "agent/audit-0123456789ab-20261022T120000Z",
            Path("/tmp/audit"), audit_key=agentic.AUDIT_KEY,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "agent_config.json").write_text("{}")
            with contextlib.ExitStack() as stack:
                for name in ("preflight_security", "refresh_base"):
                    stack.enter_context(patch.object(agentic, name))
                stack.enter_context(patch.object(agentic, "load_config", return_value=config))
                stack.enter_context(patch.object(agentic, "github_repo_from_remote", return_value="owner/repo"))
                stack.enter_context(patch.object(agentic, "default_base_branch", return_value="main"))
                stack.enter_context(patch.object(agentic, "runs_for_audit", return_value=[run]))
                stack.enter_context(patch.object(agentic, "logs_path", return_value=root / "missing-run"))
                with self.assertRaisesRegex(RuntimeError, "No completed audit report found"):
                    agentic.execute_locked(args, root)

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
            self.assertEqual(snapshot["status"], "audit-complete")
            self.assertEqual(snapshot["stage"], "audit")
            self.assertEqual(snapshot["severity"], "HIGH")
            self.assertEqual(snapshot["audit_report"]["summary"], "Audit summary")

    def test_publish_audit_issue_uses_saved_report_without_touching_worktree(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        report = {
            "schema_version": 1,
            "audit_key": issue.audit_key,
            "summary": "Audit summary",
            "findings": [{
                "category": "security", "severity": "HIGH", "title": "Unsafe boundary",
                "description": "Input crosses a privileged boundary.",
                "recommendation": "Validate input.", "evidence": ["src/app.py:10"],
            }],
            "measurements": [],
            "skipped": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "audit-report.json").write_text(json.dumps(report))
            run = Run(
                None,
                "agent/audit-0123456789ab-20261022T123456789012Z",
                Path(temp),
            )
            with (
                patch.object(audit_workflow, "command", return_value=CompletedProcess(
                    ["gh"], 0, stdout="https://github.test/issues/7\n", stderr="",
                )) as command,
                patch("agentic_runner.shell_input._interactive_line", return_value="y"),
            ):
                url = audit_workflow.publish_audit_issue(
                    run, issue, "owner/repo", "github.test", folder,
                )
            self.assertEqual(url, "https://github.test/issues/7")
            argv = command.call_args.args[0]
            self.assertEqual(argv[:4], ["gh", "issue", "create", "--repo"])
            self.assertIn("owner/repo", argv)
            self.assertIn(
                "Repository audit findings — 2026-10-22T12:34:56.789012Z",
                argv,
            )
            self.assertIn("Unsafe boundary", argv[-1])
            self.assertTrue((folder / "audit-issue.json").is_file())

    def test_auto_publish_audit_reuses_matching_remote_issue_without_prompt(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        report = {
            "schema_version": 1, "audit_key": issue.audit_key,
            "summary": "Audit summary", "findings": [], "measurements": [],
            "skipped": [],
        }
        run = Run(None, "agent/audit-0123456789ab-20261022T123456789012Z", Path("/tmp/audit"))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "audit-report.json").write_text(json.dumps(report))
            title = audit_workflow._audit_issue_title(run)
            with patch.object(audit_workflow, "command", return_value=CompletedProcess(
                ["gh"], 0, stdout=json.dumps([{"title": title, "url": "https://github.test/issues/8"}]), stderr="",
            )) as command, patch("agentic_runner.shell_input._interactive_line", side_effect=AssertionError("prompted")):
                url = audit_workflow.publish_audit_issue(
                    run, issue, "owner/repo", "github.test", folder, auto=True,
                )
            self.assertEqual(url, "https://github.test/issues/8")
            self.assertTrue((folder / "publication.json").is_file())
            self.assertEqual(command.call_count, 1)

    def test_auto_publish_audit_reuses_local_publication_record_without_github_lookup(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        report = {
            "schema_version": 1, "audit_key": issue.audit_key,
            "summary": "Audit summary", "findings": [], "measurements": [],
            "skipped": [],
        }
        run = Run(None, "agent/audit-0123456789ab-20261022T123456789012Z", Path("/tmp/audit"))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "audit-report.json").write_text(json.dumps(report))
            (folder / "publication.json").write_text(json.dumps({
                "status": "published", "kind": "audit_issue",
                "url": "https://github.test/issues/8", "branch": run.branch,
            }))
            with patch.object(audit_workflow, "command", side_effect=AssertionError("looked up remote")):
                url = audit_workflow.publish_audit_issue(
                    run, issue, "owner/repo", "github.test", folder, auto=True,
                )
            self.assertEqual(url, "https://github.test/issues/8")

    def test_automatic_audit_publication_rejects_terminal_state_markers(self):
        issue = Issue.for_audit("0123456789ab", "Audit scope")
        report = {
            "schema_version": 1, "audit_key": issue.audit_key,
            "summary": "Audit summary", "findings": [], "measurements": [],
            "skipped": [],
        }
        for marker, payload in (
            ("blocked.json", {"status": "blocked"}),
            ("result.json", {"status": "FAILED"}),
            ("result.json", {"status": "RETRY_EXHAUSTED"}),
            ("result.json", {"status": "NO_CHANGES"}),
        ):
            with self.subTest(marker=marker, status=payload["status"]):
                with tempfile.TemporaryDirectory() as temp:
                    folder = Path(temp)
                    (folder / "audit-report.json").write_text(json.dumps(report))
                    (folder / marker).write_text(json.dumps(payload))
                    self.assertFalse(agentic._automatic_candidate_eligible(issue, folder))

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
            timestamped = root / "agent-audit-0123456789ab-20261022T120000Z-deadbeef"
            timestamped.mkdir()
            timestamped_snapshot = next(
                item for item in dashboard_data.collect_runs(root)
                if "20261022T120000Z" in item["branch"]
            )
            self.assertEqual(
                timestamped_snapshot["branch"],
                "agent/audit-0123456789ab-20261022T120000Z-deadbeef",
            )
            html = render_dashboard([snapshot], "repo", "logs")
            self.assertIn('run.audit?"audit":run.issue==null?"local"', html)
            self.assertIn("run.audit_report?.summary", html)
            self.assertIn("function renderAuditOutput(run)", html)
            self.assertIn('insertAdjacentHTML(summaryBlock?"afterend":"beforeend"', html)
            self.assertNotIn("var auditOutputObserver=new MutationObserver", html)
            self.assertNotIn('auditOutputObserver.observe(document.querySelector("#detail")', html)
            self.assertIn("Final audit output", html)
            self.assertIn("audit-complete", html)
            self.assertIn("agentic audit --publish", html)

    def test_shell_audit_parser_preserves_safe_argv_values(self):
        flags = parse_audit_command(["/audit", "--instruction", "--publish", "--new", "--resume"])
        self.assertEqual(flags, ["--instruction", "--publish", "--new", "--resume"])
        with self.assertRaises(ValueError):
            parse_audit_command(["/audit", "--review", "--publish"])
        with self.assertRaises(ValueError):
            parse_audit_command(["/audit", "--review"])

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
