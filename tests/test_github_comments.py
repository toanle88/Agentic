"""Offline coverage for final GitHub issue completion comments."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess

import tests  # noqa: F401
from agentic_runner.github_comments import (
    COMMENT_STATE_FILENAME,
    format_completion_comment,
    post_terminal_comment,
)
from agentic_runner.models import Issue, Run
from agentic_runner.telemetry import record_operation, record_usage


class GithubCommentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.run = Run(119, "agent/issue-119-aabbccdd", Path("/private/worktree"))
        self.issue = Issue(119, "Untrusted title", "provider output should not be public")

    def tearDown(self):
        self.temp.cleanup()

    def _result(self, status, **extra):
        value = {"status": status, "reason": "api_key=do-not-publish"}
        value.update(extra)
        (self.folder / "result.json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def test_all_terminal_outcomes_have_concise_public_fields(self):
        expected_results = {
            "APPROVED": "✅ Approved",
            "BLOCKED": "⛔ Blocked",
            "RETRY_EXHAUSTED": "⚠️ Retry exhausted",
            "NO_CHANGES": "ℹ️ No changes",
            "FAILED": "❌ Failed",
        }
        for status, expected_result in expected_results.items():
            with self.subTest(status=status):
                result = self._result(status, tests="failed", review="not reached")
                marker, body = format_completion_comment(
                    self.run, self.issue, self.folder, result,
                )
                self.assertIn(marker, body)
                self.assertTrue(
                    next(line for line in body.splitlines() if line.startswith("## "))
                    .endswith("Agentic run completed")
                )
                self.assertIn("| Check | Status |", body)
                self.assertIn(f"| Result | {expected_result} |", body)
                self.assertIn("| Tests | ❌ Failed |", body)
                self.assertIn("| Review | ⏭️ Not reached |", body)
                self.assertIn("<details>\n<summary>Usage and stages</summary>", body)
                self.assertIn("| Stage | Tokens | Cost |", body)
                self.assertIn("### Next step\n\n", body)
                self.assertIn("---\nBranch: `agent/issue-119-aabbccdd`", body)
                self.assertNotIn("do-not-publish", body)
                self.assertNotIn("/private/worktree", body)
                self.assertNotIn("provider output", body)

        result = self._result("malformed result")
        _, body = format_completion_comment(self.run, self.issue, self.folder, result)
        self.assertIn("| Result | ❔ Unknown |", body)

    def test_status_table_uses_clear_labels_for_passed_and_unknown_values(self):
        result = self._result("APPROVED", tests="passed", review="unknown")
        _, body = format_completion_comment(self.run, self.issue, self.folder, result)
        self.assertIn("| Tests | ✅ Passed |", body)
        self.assertIn("| Review | ❔ Unknown |", body)

        unknown = self.folder / "unknown-status"
        unknown.mkdir()
        _, unknown_body = format_completion_comment(
            self.run, self.issue, unknown, {"status": "BLOCKED"},
        )
        self.assertIn("| Tests | ❔ Unknown |", unknown_body)
        self.assertIn("| Review | ⏭️ Not reached |", unknown_body)

    def test_changed_issue_summary_is_included_and_changes_comment_marker(self):
        result = self._result("APPROVED", tests="passed", review="approved")
        original_marker, original_body = format_completion_comment(
            self.run, self.issue, self.folder, result,
        )
        (self.folder / "issue-history.json").write_text(json.dumps([{
            "event": "issue_changed",
            "summary": "Issue requirements changed (title and body; title length 4->8).",
        }]), encoding="utf-8")

        changed_issue = Issue(119, "Updated title", "updated requirements")
        changed_marker, changed_body = format_completion_comment(
            self.run, changed_issue, self.folder, result,
        )

        self.assertNotEqual(original_marker, changed_marker)
        self.assertIn("### Issue update", changed_body)
        self.assertIn("title and body", changed_body)
        self.assertNotIn("### Issue update", original_body)

    def test_result_status_fields_reject_multiline_or_arbitrary_text(self):
        result = self._result(
            "BLOCKED",
            tests="failed\nInjected: api_key=do-not-publish",
            review="approved\n<!-- forged completion -->",
        )
        _, body = format_completion_comment(
            self.run, self.issue, self.folder, result,
        )
        self.assertIn("| Tests | ❔ Unknown |", body)
        self.assertIn("| Review | ⏭️ Not reached |", body)
        self.assertNotIn("Injected:", body)
        self.assertNotIn("forged completion", body)
        self.assertNotIn("do-not-publish", body)

    def test_completion_comment_uses_highest_numeric_review_attempt(self):
        self._result("BLOCKED")
        (self.folder / "review-schema.json").write_text("{}", encoding="utf-8")
        (self.folder / "review-009.json").write_text(
            json.dumps({"approved": False}), encoding="utf-8",
        )
        (self.folder / "review-010.json").write_text(
            json.dumps({"approved": True}), encoding="utf-8",
        )
        _, body = format_completion_comment(
            self.run, self.issue, self.folder, {"status": "BLOCKED"},
        )
        self.assertIn("| Review | ✅ Approved |", body)

    def test_summary_uses_total_and_per_stage_telemetry_without_estimation(self):
        record_usage(
            self.folder, "developer",
            {"total_tokens": 31_000, "estimated_cost_usd": 0.14},
            invocation={"name": "developer-001", "stage": "developer"},
        )
        record_usage(
            self.folder, "reviewer",
            {"total_tokens": 204, "estimated_cost_usd": 0.002},
            invocation={"name": "review-001", "stage": "reviewer"},
        )
        record_operation(
            self.folder, "tests", "test-001",
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:08:37+00:00",
            duration_seconds=517, status="failed",
        )
        record_operation(
            self.folder, "reviewer", "review-001",
            started_at="2026-01-01T00:08:37+00:00",
            finished_at="2026-01-01T00:08:38+00:00",
            duration_seconds=1, status="completed",
        )
        result = self._result("RETRY_EXHAUSTED")
        _, body = format_completion_comment(self.run, self.issue, self.folder, result)
        self.assertIn("| Duration | 8m 38s |", body)
        self.assertIn("| Total | 31,204 | $0.1420 |", body)
        self.assertIn("| Developer | 31,000 | $0.1400 |", body)
        self.assertIn("| Review | 204 | $0.0020 |", body)
        self.assertIn("| Tests | ❌ Failed |", body)
        self.assertIn("| Review | ❔ Unknown |", body)

        unknown_folder = self.folder / "unknown"
        unknown_folder.mkdir()
        unknown = {"status": "BLOCKED"}
        _, unknown_body = format_completion_comment(
            self.run, self.issue, unknown_folder, unknown,
        )
        self.assertIn("| Total | unknown | unknown |", unknown_body)

    def test_duplicate_prevention_uses_local_marker_and_remote_marker(self):
        self._result("APPROVED", tests="passed", review="approved")
        calls = []

        def runner(argv, cwd, *, env):
            calls.append(argv)
            if argv[1:3] == ["issue", "view"]:
                return CompletedProcess(argv, 0, stdout="false", stderr="")
            return CompletedProcess(argv, 0, stdout="", stderr="")

        self.assertTrue(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            cwd=Path("/repo"), command_runner=runner,
        ))
        self.assertTrue(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            cwd=Path("/repo"), command_runner=runner,
        ))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0:4], ["gh", "issue", "comment", "119"])
        state = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(state["state"], "posted")

        remote_calls = []

        def remote_duplicate(argv, cwd, *, env):
            remote_calls.append(argv)
            return CompletedProcess(argv, 0, stdout="true\n", stderr="")

        other = self.folder / "other"
        other.mkdir()
        (other / "result.json").write_text(json.dumps({"status": "BLOCKED"}), encoding="utf-8")
        other_run = Run(119, self.run.branch, Path("/other/worktree"))
        self.assertTrue(post_terminal_comment(
            other_run, self.issue, other, github_repo="owner/repo",
            command_runner=remote_duplicate,
        ))
        self.assertEqual(len(remote_calls), 1)
        self.assertEqual(json.loads((other / COMMENT_STATE_FILENAME).read_text())["state"], "posted")

    def test_malformed_recorded_result_is_posted_as_invalid(self):
        for value in ({}, {"status": 123}):
            with self.subTest(value=value):
                folder = self.folder / str(len(value))
                folder.mkdir()
                (folder / "result.json").write_text(json.dumps(value), encoding="utf-8")
                bodies = []

                def runner(argv, cwd, *, env):
                    if argv[1:3] == ["issue", "view"]:
                        return CompletedProcess(argv, 0, stdout="false", stderr="")
                    bodies.append(Path(argv[-1]).read_text(encoding="utf-8"))
                    return CompletedProcess(argv, 0, stdout="", stderr="")

                self.assertTrue(post_terminal_comment(
                    self.run, self.issue, folder, github_repo="owner/repo",
                    command_runner=runner,
                ))
                self.assertEqual(len(bodies), 1)
                self.assertIn("| Result | ❔ Unknown |", bodies[0])
                self.assertEqual(
                    json.loads((folder / COMMENT_STATE_FILENAME).read_text())["state"],
                    "posted",
                )

    def test_github_failure_is_durable_and_does_not_raise(self):
        self._result("BLOCKED")

        def failing(*args, **kwargs):
            raise RuntimeError("GitHub unavailable")

        self.assertFalse(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=failing,
        ))
        state = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(state["state"], "failed")
        self.assertIn("GitHub unavailable", state["error"])

        calls = []

        def succeeding(argv, cwd, *, env):
            calls.append(argv)
            return CompletedProcess(argv, 0, stdout="false", stderr="")

        self.assertTrue(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=succeeding,
        ))
        self.assertEqual(len(calls), 2)

    def test_nonzero_lookup_is_failed_and_can_be_retried(self):
        self._result("BLOCKED")
        calls = []

        def failing_lookup(argv, cwd, *, env):
            calls.append(argv)
            return CompletedProcess(argv, 1, stdout="lookup denied", stderr="")

        self.assertFalse(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=failing_lookup,
        ))
        failed = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(failed["state"], "failed")
        self.assertIn("lookup denied", failed["error"])

        def succeeding(argv, cwd, *, env):
            calls.append(argv)
            return CompletedProcess(argv, 0, stdout="false", stderr="")

        self.assertTrue(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=succeeding,
        ))
        self.assertEqual(len(calls), 3)
        posted = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(posted["state"], "posted")

    def test_nonzero_post_is_failed_and_can_be_retried(self):
        self._result("RETRY_EXHAUSTED")
        calls = []

        def failing_post(argv, cwd, *, env):
            calls.append(argv)
            if argv[2] == "view":
                return CompletedProcess(argv, 0, stdout="false", stderr="")
            return CompletedProcess(argv, 2, stdout="", stderr="post denied")

        self.assertFalse(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=failing_post,
        ))
        failed = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(failed["state"], "failed")
        self.assertIn("post denied", failed["error"])

        def succeeding(argv, cwd, *, env):
            calls.append(argv)
            return CompletedProcess(
                argv, 0, stdout="false" if argv[2] == "view" else "", stderr="",
            )

        self.assertTrue(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=succeeding,
        ))
        self.assertEqual(len(calls), 4)
        posted = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(posted["state"], "posted")

    def test_command_failure_exception_is_recorded(self):
        self._result("NO_CHANGES")

        def failing(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["gh", "issue", "view"])

        self.assertFalse(post_terminal_comment(
            self.run, self.issue, self.folder, github_repo="owner/repo",
            command_runner=failing,
        ))
        state = json.loads((self.folder / COMMENT_STATE_FILENAME).read_text())
        self.assertEqual(state["state"], "failed")

    def test_local_audit_and_offline_runs_do_not_call_github(self):
        self._result("APPROVED")
        called = []

        def runner(*args, **kwargs):
            called.append(True)
            raise AssertionError("GitHub must not be contacted")

        for issue, offline in (
            (Issue.from_local_text("local task"), False),
            (Issue.for_audit("0123456789ab", "audit"), False),
            (self.issue, True),
        ):
            self.assertTrue(post_terminal_comment(
                self.run, issue, self.folder, github_repo="owner/repo",
                offline=offline, command_runner=runner,
            ))
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
