import unittest
from pathlib import Path

from agentic_providers import cli_provider_command, codex_command


class ProviderCommandTests(unittest.TestCase):
    def test_codex_command_uses_explicit_sandbox_and_output_schema(self):
        command = codex_command(
            "inspect the task; do not execute shell text",
            readonly=True,
            schema=Path("/tmp/schema.json"),
            output=Path("/tmp/result.json"),
            model="test-model",
            reasoning_effort="high",
        )
        self.assertEqual(command[:6], ["codex", "exec", "--json", "--sandbox", "read-only", "--model"])
        self.assertIn("--output-schema", command)
        self.assertIn("--output-last-message", command)
        self.assertEqual(command[-1], "inspect the task; do not execute shell text")

    def test_native_provider_modes_keep_reviewer_restrictions(self):
        gemini, structured = cli_provider_command("gemini", "review", readonly=True)
        self.assertTrue(structured)
        self.assertIn("--approval-mode", gemini)
        self.assertEqual(gemini[gemini.index("--approval-mode") + 1], "plan")

        claude, structured = cli_provider_command("claude", "develop", readonly=False)
        self.assertTrue(structured)
        self.assertIn("--dangerously-skip-permissions", claude)

        copilot, structured = cli_provider_command("copilot", "review", readonly=True)
        self.assertFalse(structured)
        self.assertIn("--deny-tool", copilot)
        self.assertNotIn("--allow-all-tools", copilot)


if __name__ == "__main__":
    unittest.main()
