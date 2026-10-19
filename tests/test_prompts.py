"""Focused tests for the extracted prompt-boundary module."""
import unittest

from agentic_models import Issue
from agentic_prompts import build_agent_prompt


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
        self.assertIn("Finding 1: finding", prompt)
        self.assertIn("new feedback", prompt)


if __name__ == "__main__":
    unittest.main()
