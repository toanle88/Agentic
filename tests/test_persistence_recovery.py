"""Crash and partial-write coverage for durable run artifacts."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import dashboard_data as agentic_dashboard_data
from agentic_runner import workflow as agentic_workflow
from agentic_runner import workflow_state as agentic_workflow_state
from agentic_runner.models import Issue
import agentic_runner.persistence as persistence
from agentic_runner.persistence import atomic_write_json, atomic_write_text, read_text_bounded
from agentic_runner.review import record_review_findings
from agentic_runner.telemetry import read_issue_snapshot, read_run_usage, record_usage


class PersistenceRecoveryTests(unittest.TestCase):
    def assert_failed_replacement_preserves_previous_artifact(self, writer, name="artifact.json"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / name
            path.write_text("previous complete state\n", encoding="utf-8")
            with patch("agentic_persistence.os.replace", side_effect=OSError("simulated interruption")):
                with self.assertRaises(OSError):
                    writer(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous complete state\n")
            self.assertEqual(list(root.glob(f".{name}.*.tmp")), [])

    def test_atomic_text_and_json_replacement_failure_is_recoverable(self):
        self.assert_failed_replacement_preserves_previous_artifact(
            lambda path: atomic_write_text(path, "new state\n"), "stream.txt",
        )
        self.assert_failed_replacement_preserves_previous_artifact(
            lambda path: atomic_write_json(path, {"status": "new"}), "state.json",
        )

    def test_atomic_replacement_syncs_the_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "agentic_persistence._fsync_parent_directory"
        ) as sync_parent:
            atomic_write_text(Path(temp) / "state.txt", "complete\n")
        sync_parent.assert_called_once()

    def test_atomic_replacement_fails_closed_when_artifact_swaps_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.txt"
            path.write_text("previous complete state\n", encoding="utf-8")
            unrelated = Path(temp) / "unrelated.txt"
            swapped = False
            import agentic_runner.persistence as persistence
            original_stat = persistence.os.stat

            def swap_target(name, *args, **kwargs):
                nonlocal swapped
                relative_target = name == "state.txt" and kwargs.get("dir_fd") is not None
                windows_target = (
                    os.name == "nt"
                    and Path(name).name.casefold() == path.name.casefold()
                )
                if not swapped and (relative_target or windows_target):
                    path.unlink()
                    unrelated.write_text("unrelated\n", encoding="utf-8")
                    unrelated.replace(path)
                    swapped = True
                return original_stat(name, *args, **kwargs)

            with patch.object(persistence.os, "stat", side_effect=swap_target):
                with self.assertRaisesRegex(
                    (OSError, ValueError), r"changed while .*being replaced"
                ):
                    atomic_write_text(path, "new state\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "unrelated\n")

    def test_each_durable_artifact_format_preserves_previous_state(self):
        artifacts = (
            ("run.json", atomic_write_json, {"schema_version": 1}),
            ("issue-snapshot.json", atomic_write_json, {"number": 146}),
            ("review-history.json", atomic_write_json, []),
            ("result.json", atomic_write_json, {"status": "BLOCKED"}),
            ("blocked.json", atomic_write_json, {"status": "blocked"}),
            ("manual-approval.json", atomic_write_json, {"status": "approved"}),
            ("review-approval.json", atomic_write_json, {"status": "approved"}),
            ("review-001.json", atomic_write_json, {"approved": False}),
            ("investigation-001.json", atomic_write_json, {"summary": "partial"}),
            ("tests-001.txt", atomic_write_text, "partial test output\n"),
            ("developer-001.txt", atomic_write_text, "partial developer output\n"),
            ("developer-001.log", atomic_write_text, "partial progress\n"),
            ("developer-001.jsonl", atomic_write_text, '{"type":"partial"}\n'),
            ("resume-task.md", atomic_write_text, "partial task\n"),
            ("job-001.json", atomic_write_json, {"state": "running"}),
            ("dashboard.html", atomic_write_text, "<html>partial</html>\n"),
        )
        for name, writer, value in artifacts:
            with self.subTest(name=name):
                self.assert_failed_replacement_preserves_previous_artifact(
                    lambda path, writer=writer, value=value: writer(path, value), name,
                )

    def test_atomic_json_redacts_at_the_final_write_boundary(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"OPENAI_API_KEY": "synthetic-final-boundary-123456"}, clear=True
        ):
            path = Path(temp) / "run.json"
            atomic_write_json(path, {"diagnostic": "api_key=synthetic-final-boundary-123456"})
            stored = path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-final-boundary-123456", stored)
        self.assertIn("[REDACTED]", stored)

    def test_truncated_and_empty_metadata_are_safe_and_recoverable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = root / "run.json"
            for broken in ("", '{"usage":'):
                run.write_text(broken, encoding="utf-8")
                record_usage(root, "developer", {"provider": "codex", "total_tokens": 3})
                self.assertEqual(read_run_usage(root)["total_tokens"], 3)

            snapshot = root / "issue-snapshot.json"
            for broken in ("", '{"number": 146'):
                snapshot.write_text(broken, encoding="utf-8")
                self.assertIsNone(read_issue_snapshot(root))

    def test_review_history_and_dashboard_ignore_invalid_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history = root / "review-history.json"
            history.write_text('{"findings":', encoding="utf-8")
            record_review_findings(
                root, {"approved": False, "findings": ["Recover history"]},
            )
            self.assertIsInstance(json.loads(history.read_text(encoding="utf-8")), list)

            run = root / "agent-issue-146-recovery123"
            run.mkdir()
            (run / "review-001.json").write_text("", encoding="utf-8")
            (run / "investigation-001.json").write_text('{"summary":', encoding="utf-8")
            (run / "blocked.json").write_text("", encoding="utf-8")
            (run / "result.json").write_text('{"status":', encoding="utf-8")
            snapshot = agentic_dashboard_data.collect_runs(root)[0]
            self.assertEqual(snapshot["review"]["status"], "invalid")
            self.assertEqual(snapshot["investigation"]["state"], "invalid")
            self.assertIsNone(snapshot["blocked"])
            self.assertIsNone(snapshot["result"])

    def test_invalid_approval_fails_closed_and_invalid_resume_evidence_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manual-approval.json").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                agentic.load_manual_approval(root)

            (root / "review-001.json").write_text('{"approved":', encoding="utf-8")
            (root / "tests-001.txt").write_text("partial output", encoding="utf-8")
            feedback = agentic.load_resume_feedback(root)
            self.assertIn("partial output", feedback)
            self.assertIn("Previous review output", feedback)

    def test_oversized_durable_state_is_bounded_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            oversized = 'x' * (agentic_workflow_state.MAX_DURABLE_JSON_BYTES + 1)
            (root / 'blocked.json').write_text(
                json.dumps({'status': 'blocked', 'padding': oversized}), encoding='utf-8'
            )
            self.assertIsNone(agentic_workflow_state.load_blocked(root))

            (root / 'manual-approval.json').write_text(
                json.dumps({'status': 'approved', 'padding': oversized}), encoding='utf-8'
            )
            with self.assertRaisesRegex(RuntimeError, 'unreadable'):
                agentic_workflow_state.load_manual_approval(root)

            run = root / 'agent-issue-146-bounded'
            run.mkdir()
            (run / 'result.json').write_text(
                json.dumps({'status': 'APPROVED', 'padding': oversized}), encoding='utf-8'
            )
            self.assertIsNone(agentic_dashboard_data._result(run))

            (root / 'issue-snapshot.json').write_text(
                json.dumps({'title': 'title', 'body': oversized}), encoding='utf-8'
            )
            self.assertIsNone(read_issue_snapshot(root))

    def test_bounded_text_rejects_oversized_file_before_a_short_native_read(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "metadata.txt"
            path.write_bytes(b"x" * 4)
            with patch.object(
                persistence.os, "read", side_effect=AssertionError("read should be skipped"),
            ):
                self.assertIsNone(read_text_bounded(path, 3))

    def test_review_approval_is_redacted_before_validation(self):
        value = "approval-synthetic-secret-123456"
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {}, clear=True
        ):
            root = Path(temp)
            (root / "review-approval.json").write_text(json.dumps({
                "status": "approved",
                "branch": "agent/issue-146-recovery123",
                "issue_digest": "issue-digest",
                "base_sha": "base-sha",
                "patch_digest": "patch-digest",
                "summary": f"api_key={value}",
                "skipped": [f"token={value}"],
            }), encoding="utf-8")

            approval = agentic.load_review_approval(root)

            self.assertIsNotNone(approval)
            self.assertNotIn(value, json.dumps(approval))
            self.assertIn("[REDACTED]", json.dumps(approval))

    def test_cli_and_workflow_state_approval_writers_bound_review_evidence_identically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = agentic.Run(146, "agent/issue-146-approval123", root)
            issue = Issue(146, "Approval parity", "description")
            verdict = {
                "summary": "\x1b[31m" + "s" * 2_500,
                "skipped": ["\x1b[32m" + "k" * 1_500, "", 42],
            }
            cli_record = agentic.record_review_approval(
                run, issue, "main", "base-sha", "patch-digest", verdict,
                {"reviewer_agent": {"provider": "codex"}}, root / "cli",
            )
            state_record = agentic_workflow_state.record_review_approval(
                run, issue, "main", "base-sha", "patch-digest", verdict,
                {"reviewer_agent": {"provider": "codex"}}, root / "state",
            )

        self.assertEqual(cli_record["summary"], state_record["summary"])
        self.assertEqual(cli_record["skipped"], state_record["skipped"])
        self.assertEqual(len(cli_record["summary"]), 2_000)
        self.assertEqual(len(cli_record["skipped"][0]), 1_000)
        self.assertEqual(cli_record["skipped"][1:], [])

    def test_cli_and_workflow_state_approval_loaders_bound_and_reject_the_same_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = {
                "status": "approved",
                "branch": "agent/issue-146-approval123",
                "issue_digest": "issue-digest",
                "base_sha": "base-sha",
                "patch_digest": "patch-digest",
                "summary": "\x1b[31m" + "s" * 2_500,
                "skipped": ["\x1b[32m" + "k" * 1_500],
            }
            (root / "review-approval.json").write_text(json.dumps(record), encoding="utf-8")

            cli_loaded = agentic.load_review_approval(root)
            state_loaded = agentic_workflow_state.load_review_approval(root)
            self.assertEqual(cli_loaded, state_loaded)
            self.assertEqual(len(cli_loaded["summary"]), 2_000)
            self.assertEqual(len(cli_loaded["skipped"][0]), 1_000)

            for malformed in ("{", '{"status": "pending"}', '{"status": "approved"}'):
                (root / "review-approval.json").write_text(malformed, encoding="utf-8")
                for loader in (agentic.load_review_approval, agentic_workflow_state.load_review_approval):
                    with self.subTest(malformed=malformed, loader=loader):
                        with self.assertRaises(RuntimeError):
                            loader(root)

    def test_resume_task_is_redacted_and_partial_streams_remain_untrusted(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"CUSTOM_PROVIDER_TOKEN": "resume-synthetic-token-123456"}, clear=True
        ):
            root = Path(temp)
            issue = Issue.from_local_text("api_key=resume-synthetic-token-123456\nContinue work")
            action = agentic_workflow._resume_action(issue, root)
            task = root / "resume-task.md"
            self.assertNotIn("resume-synthetic-token-123456", task.read_text(encoding="utf-8"))
            self.assertNotIn("resume-synthetic-token-123456", action)


if __name__ == "__main__":
    unittest.main()
