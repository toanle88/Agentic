"""Focused tests for bounded CI evidence and PR-repair safety."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401
from agentic_runner import cli as agentic_cli
from agentic_runner.ci_repair import (
    CIEvidence,
    MAX_CI_OUTPUT_BYTES,
    MAX_CI_LOG_OUTPUT_BYTES,
    can_resume_dirty_worktree,
    classify_failure,
    fetch_ci_evidence,
    find_worktree_for_branch,
    poll_ci,
    record_event,
    stash_dirty_worktree_for_repair,
    validate_repair_limits,
    verify_worktree_for_pr,
    worktree_change_fingerprint,
)
from agentic_runner.models import PullRequest, Run
from agentic_runner.config import load_config
from agentic_runner.worktrees import command as run_bounded_command, get_pull_request


class CIRepairTests(unittest.TestCase):
    def setUp(self):
        self.pr = PullRequest(
            123, "Fix", "body", "OPEN", "main", "base", "agent/issue-1-abcd1234",
            "head", "owner/repo", "https://github.test/pull/123",
        )

    def test_github_preflight_uses_the_active_cli_environment(self):
        with patch("agentic_runner.cli.shutil.which", return_value="/usr/bin/gh") as which:
            with patch(
                "agentic_runner.cli._agentic_worktrees.probe",
                return_value=(True, "authenticated"),
            ) as probe:
                agentic_cli._github_preflight(Path("."), "github.test")
        which.assert_called_once_with("gh")
        argv, cwd = probe.call_args.args[:2]
        self.assertEqual(argv, ["/usr/bin/gh", "auth", "status", "--active"])
        self.assertEqual(cwd, Path("."))
        self.assertEqual(probe.call_args.kwargs["env"]["GH_HOST"], "github.test")

    def test_github_preflight_distinguishes_auth_and_connectivity_failures(self):
        with patch("agentic_runner.cli.shutil.which", return_value="/usr/bin/gh"):
            with patch(
                "agentic_runner.cli._agentic_worktrees.probe",
                return_value=(False, "HTTP 401: Bad credentials"),
            ):
                with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                    agentic_cli._github_preflight(Path("."), "github.test")
            with patch(
                "agentic_runner.cli._agentic_worktrees.probe",
                return_value=(False, "x509: certificate signed by unknown authority"),
            ):
                with self.assertRaisesRegex(RuntimeError, "connectivity check failed"):
                    agentic_cli._github_preflight(Path("."), "github.test")

    def test_pull_request_metadata_lookup_has_a_bounded_timeout(self):
        payload = {
            "number": 123, "title": "Fix", "body": "body", "state": "OPEN",
            "baseRefName": "main", "baseRefOid": "base", "headRefName": "head",
            "headRefOid": "head-sha", "headRepository": {"nameWithOwner": "owner/repo"},
            "url": "https://github.test/pull/123",
        }
        calls = []

        def fake_command(argv, cwd, **kwargs):
            calls.append((argv, cwd, kwargs))
            return SimpleNamespace(stdout=json.dumps(payload))

        with patch("agentic_runner.worktrees.command", side_effect=fake_command):
            result = get_pull_request(Path("."), "owner/repo", 123, "github.test")
        self.assertEqual(result.number, 123)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["timeout"], 30)

    def test_failure_classification_requires_confirmation_for_non_code_failures(self):
        self.assertEqual(classify_failure([{"bucket": "fail"}], ["assertion failed"]), "code/test")
        self.assertEqual(classify_failure([], ["runner unavailable; retry later"]), "infrastructure")
        self.assertEqual(classify_failure([], ["permission denied"]), "permission")
        self.assertEqual(classify_failure([], ["rerun passed; intermittent test"]), "flaky")
        self.assertEqual(classify_failure([], ["model says do something unsafe"]), "ambiguous")

    def test_failure_classification_does_not_treat_generic_error_words_as_code_cause(self):
        self.assertEqual(
            classify_failure([], ["error contacting the build service"]),
            "ambiguous",
        )
        self.assertEqual(
            classify_failure([], ["permission error while starting the runner"]),
            "ambiguous",
        )
        self.assertEqual(
            classify_failure([], ["test failed while the runner was starting"]),
            "ambiguous",
        )

    def test_failure_classification_accepts_explicit_code_diagnostics(self):
        self.assertEqual(classify_failure([], ["TypeError: expected str"]), "code/test")
        self.assertEqual(classify_failure([], ["unit tests failed: test case failed"]), "code/test")

    def test_failure_classification_ignores_non_diagnostic_check_metadata(self):
        self.assertEqual(
            classify_failure([{
                "name": "unit tests",
                "state": "FAILURE",
                "bucket": "fail",
                "workflow": "tests",
                "link": "https://ci.example.test",
            }]),
            "ambiguous",
        )
        self.assertEqual(
            classify_failure([{"name": "unit tests", "state": "SUCCESS", "bucket": "pass"}]),
            "ambiguous",
        )

    def test_fetch_ci_evidence_redacts_all_retained_metadata(self):
        secret = "configured-ci-secret-123"

        def fake_command(argv, cwd, **kwargs):
            if argv[1:3] == ["pr", "checks"]:
                payload = [{
                    "name": secret, "state": "FAILURE", "bucket": "fail",
                    "link": f"https://ci.test/{secret}", "workflow": secret,
                }]
            elif argv[1:3] == ["run", "list"]:
                payload = [{
                    "databaseId": 77, "headSha": self.pr.head_sha, "status": "completed",
                    "conclusion": "failure", "name": secret, "url": f"https://ci/{secret}",
                }]
            else:
                payload = "assertion failed"
            return SimpleNamespace(stdout=json.dumps(payload) if not isinstance(payload, str) else payload)

        evidence = fetch_ci_evidence(
            Path('.'), 'owner/repo', self.pr, run_command=fake_command,
            extra_values=(secret,),
        )
        self.assertNotIn(secret, json.dumps(evidence.to_dict()))
        self.assertNotIn(secret, evidence.prompt_context())

        constructed = CIEvidence(
            "ambiguous", ({"message": secret},), (), self.pr.head_sha, (secret,),
        )
        self.assertNotIn(secret, json.dumps(constructed.to_dict()))
        self.assertNotIn(secret, constructed.prompt_context())

    def test_fetch_ci_evidence_requires_matching_failed_run_head_and_conclusion(self):
        calls = []

        def fake_command(argv, cwd, **kwargs):
            calls.append(argv)
            if argv[1:3] == ["pr", "checks"]:
                payload = [{"name": "tests", "state": "FAILURE", "bucket": "fail"}]
            elif argv[1:3] == ["run", "list"]:
                payload = [
                    {"databaseId": 1, "headSha": None, "status": "completed", "conclusion": "failure"},
                    {"databaseId": 2, "headSha": self.pr.head_sha, "status": "completed", "conclusion": "success"},
                    {"databaseId": 3, "headSha": self.pr.head_sha, "status": "completed", "conclusion": "failure"},
                ]
            else:
                self.assertEqual(argv[3], "3")
                payload = "assertion failed"
            return SimpleNamespace(stdout=json.dumps(payload) if not isinstance(payload, str) else payload)

        evidence = fetch_ci_evidence(Path('.'), 'owner/repo', self.pr, run_command=fake_command)
        self.assertEqual([call[3] for call in calls if call[1:3] == ["run", "view"]], ["3"])
        self.assertEqual(len(evidence.logs), 1)

    def test_external_ci_output_is_bounded_before_parse_or_copy(self):
        calls = []

        def oversized_json(argv, cwd, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(stdout="{" + ("x" * MAX_CI_OUTPUT_BYTES) + "}")

        with self.assertRaisesRegex(RuntimeError, "JSON response.*safety limit"):
            fetch_ci_evidence(Path('.'), 'owner/repo', self.pr, run_command=oversized_json)
        self.assertEqual(calls[0]["max_output_bytes"], MAX_CI_OUTPUT_BYTES)

        def oversized_log(argv, cwd, **kwargs):
            if argv[1:3] == ["pr", "checks"]:
                payload = [{"name": "tests", "state": "FAILURE", "bucket": "fail"}]
            elif argv[1:3] == ["run", "list"]:
                payload = [{
                    "databaseId": 77, "headSha": self.pr.head_sha,
                    "status": "completed", "conclusion": "failure",
                }]
            else:
                payload = "x" * (MAX_CI_LOG_OUTPUT_BYTES + 1)
            return SimpleNamespace(stdout=json.dumps(payload) if not isinstance(payload, str) else payload)

        with self.assertRaisesRegex(RuntimeError, "failed-run log.*safety limit"):
            fetch_ci_evidence(Path('.'), 'owner/repo', self.pr, run_command=oversized_log)

    def test_default_github_commands_request_pre_parse_output_bound(self):
        calls = []

        def fake_command(argv, cwd, **kwargs):
            calls.append(kwargs)
            if argv[1:3] == ["pr", "checks"]:
                payload = [{"name": "tests", "state": "FAILURE", "bucket": "fail"}]
            elif argv[1:3] == ["run", "list"]:
                payload = [{
                    "databaseId": 77, "headSha": self.pr.head_sha,
                    "status": "completed", "conclusion": "failure",
                }]
            else:
                payload = "assertion failed"
            return SimpleNamespace(stdout=json.dumps(payload) if not isinstance(payload, str) else payload)

        with patch("agentic_runner.worktrees.command", side_effect=fake_command):
            fetch_ci_evidence(Path('.'), 'owner/repo', self.pr)
        self.assertTrue(calls)
        self.assertEqual(
            [call.get("max_output_bytes") for call in calls],
            [MAX_CI_OUTPUT_BYTES, MAX_CI_OUTPUT_BYTES, MAX_CI_LOG_OUTPUT_BYTES],
        )

    def test_default_poll_commands_request_pre_parse_output_bound(self):
        calls = []

        def fake_command(argv, cwd, **kwargs):
            calls.append(kwargs)
            if argv[1:3] == ["pr", "view"]:
                payload = {"headRefName": self.pr.head, "headRefOid": self.pr.head_sha}
            else:
                payload = [{"state": "SUCCESS", "bucket": "pass"}]
            return SimpleNamespace(stdout=json.dumps(payload))

        with patch("agentic_runner.worktrees.command", side_effect=fake_command):
            self.assertEqual(
                poll_ci(
                    Path("."), "owner/repo", self.pr, "github.test",
                    timeout_seconds=10, interval_seconds=1,
                ),
                "passed",
            )
        self.assertTrue(calls)
        self.assertTrue(all(call.get("max_output_bytes") == MAX_CI_OUTPUT_BYTES for call in calls))

    def test_real_subprocess_capture_stops_at_output_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            script = "import sys; sys.stdout.write('x' * 300000)"
            with self.assertRaisesRegex(RuntimeError, "output exceeded the 256000-byte safety limit"):
                run_bounded_command(
                    [sys.executable, "-c", script], Path(temp), max_output_bytes=MAX_CI_OUTPUT_BYTES,
                )

    def test_fetch_ci_evidence_bounds_and_redacts_logs(self):
        calls = []

        def fake_command(argv, cwd, **kwargs):
            calls.append(argv)
            if argv[1:3] == ["pr", "checks"]:
                payload = [{"name": "tests", "state": "FAILURE", "bucket": "fail", "link": "https://ci"}]
            elif argv[1:3] == ["run", "list"]:
                payload = [{"databaseId": 77, "headSha": "head", "status": "completed", "conclusion": "failure", "name": "tests"}]
            else:
                payload = "token=supersecret123\nassertion failed\n" + ("x" * 20_000)
            return SimpleNamespace(stdout=json.dumps(payload) if not isinstance(payload, str) else payload)

        evidence = fetch_ci_evidence(Path('.'), 'owner/repo', self.pr, run_command=fake_command)
        self.assertEqual(evidence.classification, "code/test")
        self.assertLessEqual(len(evidence.logs[0]["excerpt"]), 12_000)
        self.assertNotIn("supersecret123", evidence.prompt_context())
        self.assertTrue(any("--log-failed" in call for call in calls))

    def test_worktree_matching_is_exact_and_requires_current_clean_head(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "worktree"
            path.mkdir()
            listing = f"worktree {path}\nbranch refs/heads/{self.pr.head}\n"
            run = find_worktree_for_branch(Path(temp), self.pr.head, run_git=lambda *_: listing)
            self.assertEqual(run.worktree, path.resolve())
            values = iter([self.pr.head, "head", ""])
            verify_worktree_for_pr(run, self.pr, run_git=lambda *_: next(values))
            values = iter(["other"])
            with self.assertRaisesRegex(RuntimeError, "branch"):
                verify_worktree_for_pr(run, self.pr, run_git=lambda *_: next(values))
            with self.assertRaisesRegex(RuntimeError, "stale"):
                values = iter([self.pr.head, "other"])
                verify_worktree_for_pr(run, self.pr, run_git=lambda *_: next(values))
            values = iter([self.pr.head, "head", " M unrelated.py"])
            with self.assertRaisesRegex(RuntimeError, "dirty"):
                verify_worktree_for_pr(run, self.pr, run_git=lambda *_: next(values))
            values = iter([self.pr.head, "head", " M agentic.py"])
            verify_worktree_for_pr(
                run, self.pr, allow_dirty=True, run_git=lambda *_: next(values),
            )

    def test_dirty_worktree_resume_requires_matching_previous_failed_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "run"
            folder.mkdir()
            worktree = Path(temp) / "worktree"
            worktree.mkdir()
            record_event(folder, {
                "event": "repair_started", "pr": self.pr.number,
                "branch": self.pr.head, "head_sha": self.pr.head_sha,
                "worktree": str(worktree),
            })
            record_event(folder, {
                "event": "repair_attempt", "attempt": 1, "completed": False,
                "dirty_fingerprint": "fingerprint-1",
            })
            self.assertTrue(can_resume_dirty_worktree(
                folder, self.pr.number, self.pr.head, self.pr.head_sha, worktree,
                current_fingerprint="fingerprint-1",
            ))
            self.assertFalse(can_resume_dirty_worktree(
                folder, self.pr.number, self.pr.head, self.pr.head_sha, worktree,
                current_fingerprint="fingerprint-2",
            ))
            self.assertFalse(can_resume_dirty_worktree(
                folder, self.pr.number + 1, self.pr.head, self.pr.head_sha, worktree,
                current_fingerprint="fingerprint-1",
            ))

    def test_dirty_worktree_resume_rejects_legacy_failed_repair_records(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "run"
            folder.mkdir()
            worktree = Path(temp) / "worktree"
            worktree.mkdir()
            record_event(folder, {
                "event": "repair_started", "pr": self.pr.number,
                "branch": self.pr.head, "head_sha": self.pr.head_sha,
                "worktree": str(worktree),
            })
            record_event(folder, {
                "event": "repair_attempt", "attempt": 1, "completed": False,
            })
            self.assertFalse(can_resume_dirty_worktree(
                folder, self.pr.number, self.pr.head, self.pr.head_sha, worktree,
                current_fingerprint="fingerprint-1",
            ))

    def test_worktree_change_fingerprint_includes_untracked_content(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp) / "worktree"
            worktree.mkdir()
            untracked = worktree / "new.py"
            untracked.write_text("one\n")
            responses = {
                ("status", "--porcelain=v1", "--untracked-files=all"): "?? new.py",
                ("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"): "",
                ("ls-files", "--others", "--exclude-standard", "-z"): "new.py\0",
            }

            def fake_git(path, *args, **kwargs):
                return responses[args]

            first = worktree_change_fingerprint(worktree, run_git=fake_git)
            untracked.write_text("two\n")
            second = worktree_change_fingerprint(worktree, run_git=fake_git)
            self.assertIsNotNone(first)
            self.assertNotEqual(first, second)

    def test_worktree_change_fingerprint_bounds_total_untracked_content(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "agentic_runner.ci_repair.MAX_REPAIR_FINGERPRINT_INPUT_BYTES", 5,
        ):
            worktree = Path(temp) / "worktree"
            worktree.mkdir()
            (worktree / "a").write_text("1234")
            (worktree / "b").write_text("5678")
            responses = {
                ("status", "--porcelain=v1", "--untracked-files=all"): "?? a\n?? b",
                ("diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"): "",
                ("ls-files", "--others", "--exclude-standard", "-z"): "a\0b\0",
            }

            def fake_git(path, *args, **kwargs):
                return responses[args]

            self.assertIsNone(worktree_change_fingerprint(worktree, run_git=fake_git))

    def test_clean_repair_stashes_changes_and_requires_clean_result(self):
        worktree = Path("/tmp/pr-repair-worktree")
        run = Run(None, self.pr.head, worktree)
        calls = []

        def fake_git(path, *args, **kwargs):
            calls.append((path, args, kwargs))
            return ""

        stash_dirty_worktree_for_repair(run, 123, run_git=fake_git)
        self.assertEqual(calls[0][0], worktree)
        self.assertEqual(
            calls[0][1],
            ("stash", "push", "--include-untracked", "--message", "Agentic CI repair --clean PR #123"),
        )
        self.assertEqual(calls[1][1], ("status", "--porcelain", "--untracked-files=all"))

    def test_poll_ci_is_bounded_and_reports_pass_fail_timeout(self):
        responses = iter([
            [{"state": "IN_PROGRESS", "bucket": "pending"}],
            [{"state": "COMPLETED", "bucket": "pass"}],
        ])
        def fake_command(*args, **kwargs):
            argv = args[0]
            if argv[1:3] == ["pr", "view"]:
                return SimpleNamespace(stdout=json.dumps({
                    "headRefName": self.pr.head, "headRefOid": self.pr.head_sha,
                }))
            return SimpleNamespace(stdout=json.dumps(next(responses)))

        current_time = [0.0]
        self.assertEqual(
            poll_ci(Path('.'), 'owner/repo', self.pr, 'github.test',
                    timeout_seconds=10, interval_seconds=1,
                    run_command=fake_command,
                    sleep=lambda _: current_time.__setitem__(0, current_time[0] + 0.1),
                    monotonic=lambda: current_time[0]),
            "passed",
        )

    def test_poll_ci_treats_neutral_and_skipped_checks_as_terminal_success(self):
        def fake_command(*args, **kwargs):
            argv = args[0]
            if argv[1:3] == ["pr", "view"]:
                return SimpleNamespace(stdout=json.dumps({
                    "headRefName": self.pr.head, "headRefOid": self.pr.head_sha,
                }))
            return SimpleNamespace(stdout=json.dumps([
                {"state": "NEUTRAL", "bucket": "neutral"},
                {"state": "SKIPPED", "bucket": "skipped"},
            ]))

        self.assertEqual(
            poll_ci(Path('.'), 'owner/repo', self.pr, 'github.test',
                    timeout_seconds=10, interval_seconds=1, run_command=fake_command),
            "passed",
        )

    def test_poll_ci_treats_skipping_bucket_as_terminal_success(self):
        def fake_command(*args, **kwargs):
            argv = args[0]
            if argv[1:3] == ["pr", "view"]:
                return SimpleNamespace(stdout=json.dumps({
                    "headRefName": self.pr.head, "headRefOid": self.pr.head_sha,
                }))
            return SimpleNamespace(stdout=json.dumps([
                {"state": "COMPLETED", "bucket": "skipping"},
            ]))

        self.assertEqual(
            poll_ci(Path("."), "owner/repo", self.pr, "github.test",
                    timeout_seconds=10, interval_seconds=1, run_command=fake_command),
            "passed",
        )

    def test_poll_ci_ignores_checks_until_pushed_head_is_visible(self):
        head_values = iter(["old", self.pr.head_sha, self.pr.head_sha])
        checks = iter([
            [{"state": "IN_PROGRESS", "bucket": "pending"}],
            [{"state": "COMPLETED", "bucket": "pass"}],
        ])
        check_calls = []

        def fake_command(*args, **kwargs):
            argv = args[0]
            if argv[1:3] == ["pr", "view"]:
                return SimpleNamespace(stdout=json.dumps({
                    "headRefName": self.pr.head, "headRefOid": next(head_values),
                }))
            check_calls.append(argv)
            return SimpleNamespace(stdout=json.dumps(next(checks)))

        current_time = [0.0]
        self.assertEqual(
            poll_ci(Path('.'), 'owner/repo', self.pr, 'github.test',
                    timeout_seconds=10, interval_seconds=1,
                    run_command=fake_command,
                    sleep=lambda _: current_time.__setitem__(0, current_time[0] + 0.1),
                    monotonic=lambda: current_time[0]),
            "passed",
        )
        self.assertEqual(len(check_calls), 2)

    def test_poll_ci_rejects_a_head_that_never_matches(self):
        def fake_command(*args, **kwargs):
            return SimpleNamespace(stdout=json.dumps({
                "headRefName": self.pr.head, "headRefOid": "old",
            }))

        current_time = [0.0]
        self.assertEqual(
            poll_ci(Path('.'), 'owner/repo', self.pr, 'github.test',
                    timeout_seconds=1, interval_seconds=1,
                    run_command=fake_command,
                    sleep=lambda _: current_time.__setitem__(0, current_time[0] + 1),
                    monotonic=lambda: current_time[0]),
            "head_changed",
        )

    def test_repair_limits_and_durable_event_history_are_bounded(self):
        self.assertEqual(validate_repair_limits(None)["max_attempts"], 2)
        with self.assertRaisesRegex(ValueError, "max_attempts"):
            validate_repair_limits({"max_attempts": 6})
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for index in range(12):
                record_event(folder, {"event": "x", "index": index, "secret": "token=value123456"})
            data = json.loads((folder / "ci-repair.json").read_text())
            self.assertLessEqual(len(data["events"]), 8)
            self.assertNotIn("value123456", (folder / "ci-repair.json").read_text())
            self.assertTrue(any(item.get("event") == "agentic_stream_truncated" for item in data["events"]))

    def test_record_event_redacts_legacy_history_and_bounds_large_events(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / "ci-repair.json"
            path.write_text(json.dumps({"events": [{"event": "legacy", "secret": "old-secret"}]}))
            record_event(
                folder, {"event": "large", "evidence": "x" * 40_000},
                extra_values=("old-secret",),
            )
            payload = path.read_text()
            data = json.loads(payload)
            self.assertLessEqual(len(payload.encode("utf-8")), 256_000)
            self.assertNotIn("old-secret", payload)
            self.assertTrue(any(item.get("event") == "agentic_stream_truncated" for item in data["events"]))

    def test_configuration_normalizes_and_rejects_ci_repair_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "agent_config.json"
            path.write_text(json.dumps({
                "test_commands": [["true"]],
                "ci_repair": {"max_attempts": 1, "poll_interval_seconds": 2,
                               "poll_timeout_seconds": 3,
                               "auto_confirm_non_code_failures": True},
            }))
            loaded = load_config(path)
            self.assertEqual(loaded["ci_repair"]["max_attempts"], 1)
            self.assertTrue(loaded["ci_repair"]["auto_confirm_non_code_failures"])
            path.write_text(json.dumps({
                "test_commands": [["true"]], "ci_repair": {"unbounded": True},
            }))
            with self.assertRaisesRegex(ValueError, "unsupported field"):
                load_config(path)
            path.write_text(json.dumps({
                "test_commands": [["true"]],
                "ci_repair": {"auto_confirm_non_code_failures": "yes"},
            }))
            with self.assertRaisesRegex(ValueError, "auto_confirm_non_code_failures"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
