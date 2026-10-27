"""Focused tests for the extracted prompt-boundary module."""
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner.models import Issue
from agentic_runner.prompts import build_agent_prompt
from agentic_runner.telemetry import record_test_result
from agentic_runner.workflow import host_validation_note


class PromptBoundaryTests(unittest.TestCase):
    def test_directives_are_delimited_and_html_escaped(self):
        prompt = build_agent_prompt(
            Issue(47, "Refactor", "Issue body"),
            instruction="Keep <script> as data",
            system_prompt="Use <safe> guidance",
        )
        self.assertIn("<run_instruction>\nKeep &lt;script&gt; as data", prompt)
        self.assertIn("<system_prompt>\nUse &lt;safe&gt; guidance", prompt)
        self.assertIn("does not authorize weakening tests", prompt)

    def test_required_findings_are_bounded_in_feedback(self):
        prompt = build_agent_prompt(
            Issue(47, "Refactor", "Issue body"),
            "new feedback",
            required_review_findings=["finding"],
        )
        self.assertIn("Finding: finding | action target: finding", prompt)
        self.assertIn("new feedback", prompt)

    def test_copilot_developer_prompt_uses_runner_for_shell_and_git(self):
        prompt = build_agent_prompt(
            Issue(47, "Refactor", "Issue body"), provider="copilot",
        )
        self.assertIn("COPILOT DEVELOPER TOOL BOUNDARY", prompt)
        self.assertIn("Agentic performs configured checks and Git inspection", prompt)
        self.assertNotIn("git status", prompt)
        self.assertNotIn("git diff --cached", prompt)

    def test_host_validation_note_is_authoritative_and_contains_only_host_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            record_test_result(folder, passed=True, diff_check="passed")
            note = host_validation_note("main", "abc123", folder)
        self.assertIn("HOST-AUTHORITATIVE VALIDATION", note)
        self.assertIn("Base comparison: performed", note)
        self.assertIn("Base revision used by host: abc123", note)
        self.assertIn("git diff --check: passed", note)
        self.assertNotIn("version", note.casefold())


if __name__ == "__main__":
    unittest.main()
