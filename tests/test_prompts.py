"""Focused tests for the extracted prompt-boundary module."""
import unittest

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner.models import Issue
from agentic_runner.prompts import build_agent_prompt


class PromptBoundaryTests(unittest.TestCase):
    def test_developer_prompt_calls_out_windows_compatibility_contract(self):
        prompt = build_agent_prompt(
            Issue(47, "Refactor", "Issue body"), provider="codex",
        )
        self.assertIn("NATIVE WINDOWS COMPATIBILITY", prompt)
        self.assertIn("Linux or WSL pass does not prove Windows behavior", prompt)
        self.assertIn("pathlib/os.fspath", prompt)
        self.assertIn("minimal home/temp environments", prompt)
        self.assertIn("report that check as skipped", prompt)

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
        self.assertIn("Finding 1: finding", prompt)
        self.assertIn("new feedback", prompt)

    def test_copilot_developer_prompt_uses_runner_for_shell_and_git(self):
        prompt = build_agent_prompt(
            Issue(47, "Refactor", "Issue body"), provider="copilot",
        )
        self.assertIn("COPILOT DEVELOPER TOOL BOUNDARY", prompt)
        self.assertIn("Agentic performs configured checks and Git inspection", prompt)
        self.assertNotIn("git status", prompt)
        self.assertNotIn("git diff --cached", prompt)


if __name__ == "__main__":
    unittest.main()
