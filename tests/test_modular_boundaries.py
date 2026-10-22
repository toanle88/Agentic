"""Characterization tests for the shell and workflow extraction seams."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import shell as agentic_shell
from agentic_runner import shell_completion as agentic_shell_completion
from agentic_runner import shell_jobs as agentic_shell_jobs
from agentic_runner import workflow as agentic_workflow
from agentic_runner import workflow_orchestration as agentic_workflow_orchestration
from agentic_runner import workflow_state as agentic_workflow_state
from agentic_runner.models import Issue


class ModularBoundaryTests(unittest.TestCase):
    def test_shell_completion_facade_reexports_focused_model(self):
        self.assertIs(agentic_shell._completion_candidates, agentic_shell_completion.completion_candidates)
        self.assertIs(agentic_shell._completion_items, agentic_shell_completion.completion_items)
        self.assertIs(agentic_shell._apply_completion, agentic_shell_completion.apply_completion)

    def test_shell_facade_reexports_job_and_foreground_boundaries(self):
        self.assertIs(agentic_shell.Job, agentic_shell_jobs.Job)
        self.assertIs(
            agentic_shell._ForegroundInputForwarder,
            agentic_shell_jobs.ForegroundInputForwarder,
        )
        self.assertIs(agentic_shell._signal_process_tree, agentic_shell_jobs.signal_process_tree)
        self.assertEqual(
            agentic_shell._normalize_foreground_output("$'first\\nsecond'"),
            "first\nsecond",
        )

    def test_workflow_facade_reexports_durable_state_boundary(self):
        self.assertIs(agentic_workflow.next_log, agentic_workflow_state.next_log)
        self.assertIs(agentic_workflow.load_blocked, agentic_workflow_state.load_blocked)
        self.assertIs(agentic_workflow.record_result, agentic_workflow_state.record_result)
        self.assertIs(
            agentic_workflow.record_review_approval,
            agentic_workflow_state.record_review_approval,
        )

    def test_legacy_cli_state_helpers_delegate_to_workflow_state(self):
        self.assertIs(agentic.next_log, agentic_workflow_state.next_log)
        self.assertIs(agentic.load_blocked, agentic_workflow_state.load_blocked)
        self.assertIs(agentic.record_blocked, agentic_workflow_state.record_blocked)
        self.assertIs(
            agentic.record_no_code_changes_blocked,
            agentic_workflow_state.record_no_code_changes_blocked,
        )
        self.assertIs(agentic.clear_blocked, agentic_workflow_state.clear_blocked)
        self.assertIs(agentic._latest_numbered_log, agentic_workflow_state.latest_numbered_log)
        self.assertIs(
            agentic.load_latest_developer_report,
            agentic_workflow_state.load_latest_developer_report,
        )

    def test_workflow_facade_reexports_developer_orchestration_boundary(self):
        self.assertIs(
            agentic_workflow._orchestrated_develop,
            agentic_workflow_orchestration.develop,
        )
        runtime = agentic_workflow._orchestration_runtime()
        self.assertIs(runtime.run_agent, agentic_workflow.run_agent)
        self.assertIs(runtime.verify_and_review, agentic_workflow.verify_and_review)

    def test_extracted_developer_orchestration_preserves_deadline_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            run = agentic_workflow.Run(None, "agent/task-export-aabbccddeeff", folder)
            issue = Issue.from_local_text("Implement export")
            result = agentic_workflow_orchestration.develop(
                run,
                issue,
                "main",
                {"max_retries": 0},
                folder,
                deadline=time.monotonic() - 1,
                runtime=agentic_workflow._orchestration_runtime(),
            )
        self.assertFalse(result)

    def test_workflow_state_preserves_bounded_resume_and_blocked_records(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            issue = Issue.from_local_text("Implement export\nKeep the header order.")
            action = agentic_workflow_state.resume_action(issue, folder)
            self.assertNotIn(issue.body, action)
            self.assertEqual(
                (folder / "resume-task.md").read_text(encoding="utf-8"),
                issue.body,
            )

            agentic_workflow_state.record_blocked(
                folder, "digest", ["Finding with \x1b[31mterminal styling"],
                attempts=2, max_retries=3,
            )
            record = agentic_workflow_state.load_blocked(folder)
            self.assertEqual(record["patch_digest"], "digest")
            self.assertEqual(record["attempts"], 2)
            self.assertEqual(json.loads((folder / "blocked.json").read_text())["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
