import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_providers import (
    cli_provider_command,
    codex_command,
    resolve_provider_executable,
)


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
        self.assertNotIn("--allowed-tools", gemini)
        self.assertNotIn("ShellTool", gemini)

        claude, structured = cli_provider_command("claude", "develop", readonly=False)
        self.assertTrue(structured)
        self.assertIn("--dangerously-skip-permissions", claude)

        copilot, structured = cli_provider_command("copilot", "review", readonly=True)
        self.assertFalse(structured)
        self.assertEqual(
            copilot[copilot.index("--available-tools") + 1], "view,grep,glob",
        )
        self.assertNotIn("bash", copilot)
        self.assertNotIn("--allow-tool", copilot)
        self.assertNotIn("--deny-tool", copilot)
        self.assertNotIn("--allow-all-tools", copilot)

    def test_copilot_download_path_is_resolved_and_used_as_argv_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            data_home = Path(temp)
            executable = data_home / "gh" / "copilot" / "copilot"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with patch.dict(
                "os.environ", {"XDG_DATA_HOME": str(data_home), "COPILOT_PATH": ""},
                clear=True,
            ), patch("agentic_providers.shutil.which", return_value=None):
                resolved = resolve_provider_executable("copilot")

            self.assertEqual(resolved.path, str(executable))
            self.assertEqual(resolved.source, "GitHub CLI download")
            command, structured = cli_provider_command(
                "copilot", "review", readonly=True, executable_path=resolved.path,
            )
            self.assertEqual(command[0], str(executable))
            self.assertFalse(structured)

    def test_isolated_resolution_does_not_use_host_copilot_path(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "copilot"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            with patch.dict("os.environ", {"COPILOT_PATH": str(executable)}, clear=True):
                resolved = resolve_provider_executable("copilot", discover_copilot=False)
        self.assertIsNone(resolved.path)


if __name__ == "__main__":
    unittest.main()
