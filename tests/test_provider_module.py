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

    def test_codex_rejects_capabilities_it_cannot_represent(self):
        for field, value in (
            ("create_files", False),
            ("git_read", False),
            ("run_checks", False),
            ("network", True),
            ("allow_dangerous", True),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "Codex cannot represent"):
                    codex_command("develop", permission_policy={field: value})

        command = codex_command("develop")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")

    def test_native_provider_modes_keep_reviewer_restrictions(self):
        gemini, structured = cli_provider_command("gemini", "review", readonly=True)
        self.assertTrue(structured)
        self.assertIn("--approval-mode", gemini)
        self.assertEqual(gemini[gemini.index("--approval-mode") + 1], "plan")
        self.assertNotIn("--allowed-tools", gemini)
        self.assertNotIn("ShellTool", gemini)

        claude, structured = cli_provider_command("claude", "develop", readonly=False)
        self.assertTrue(structured)
        self.assertIn("--permission-mode", claude)
        self.assertIn("dontAsk", claude)
        self.assertNotIn("acceptEdits", claude)
        self.assertNotIn("--dangerously-skip-permissions", claude)

        gemini, structured = cli_provider_command(
            "gemini", "develop", readonly=False,
            permission_policy={"run_checks": False},
        )
        self.assertTrue(structured)
        self.assertIn("auto_edit", gemini)
        self.assertNotIn("yolo", gemini)

        copilot, structured = cli_provider_command("copilot", "review", readonly=True)
        self.assertFalse(structured)
        available = copilot[copilot.index("--available-tools") + 1]
        self.assertEqual(available, "view,grep,glob")
        self.assertNotIn("bash", available)
        self.assertNotIn("--allow-tool", copilot)
        self.assertNotIn("--deny-tool", copilot)
        self.assertNotIn("--allow-all-tools", copilot)

        copilot, structured = cli_provider_command("copilot", "develop", readonly=False)
        self.assertFalse(structured)
        self.assertIn("--available-tools", copilot)
        self.assertIn("view,grep,glob,edit,create", copilot)
        self.assertNotIn("bash", copilot[copilot.index("--available-tools") + 1])
        self.assertIn("--no-ask-user", copilot)
        self.assertNotIn("--allow-all-tools", copilot)
        self.assertNotIn("--allow-all-paths", copilot)

        claude_review, _ = cli_provider_command("claude", "review", readonly=True)
        self.assertIn("dontAsk", claude_review)
        self.assertNotIn("plan", claude_review)

    def test_native_provider_rejects_reviewer_write_and_check_overrides(self):
        for provider in ("gemini", "claude", "copilot"):
            with self.subTest(provider=provider):
                with self.assertRaisesRegex(ValueError, "read-only provider"):
                    cli_provider_command(
                        provider, "review", readonly=True,
                        permission_policy={"edit_files": True},
                    )

    def test_copilot_keeps_developer_turn_shell_free_when_checks_are_disabled(self):
        command, _ = cli_provider_command(
            "copilot", "review", readonly=False,
            permission_policy={"run_checks": False},
            worktree=Path("/tmp/agentic-worktree"),
        )
        available = command[command.index("--available-tools") + 1]
        self.assertNotIn("bash", available.split(","))
        self.assertNotIn("shell(", " ".join(command))

    def test_native_reviewers_are_shell_free(self):
        for provider in ("gemini", "claude", "copilot"):
            with self.subTest(provider=provider):
                command, _ = cli_provider_command(provider, "review", readonly=True)
                rendered = " ".join(command)
                self.assertNotIn("ShellTool", rendered)
                if provider == "claude":
                    tools = command[command.index("--tools") + 1]
                    self.assertNotIn("Bash", tools)
                elif provider == "copilot":
                    available = command[command.index("--available-tools") + 1]
                    self.assertNotIn("bash", available)
                else:
                    self.assertNotIn("run_shell_command", rendered)
                self.assertNotIn("shell(", rendered)
                self.assertNotIn("--allow-tool", command)
                self.assertNotIn("--deny-tool", command)

    def test_native_provider_commands_translate_common_capabilities(self):
        policy = {
            "read_files": True, "search_files": True, "edit_files": True,
            "create_files": True, "git_read": True, "run_checks": True,
            "network": False, "allow_dangerous": False,
        }
        claude, _ = cli_provider_command(
            "claude", "develop", permission_policy=policy,
            worktree=Path("/tmp/agentic-worktree"),
            check_commands=[["python", "-m", "unittest", "discover", "-s", "tests"]],
        )
        self.assertIn("--allowedTools", claude)
        self.assertNotIn("--allowed-tools", claude)
        for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
            self.assertIn(f"{tool}(//tmp/agentic-worktree/**)", claude)
        for bare_tool in ("Read", "Glob", "Grep", "Edit", "Write"):
            self.assertNotIn(bare_tool, claude[claude.index("--allowedTools") + 1:])
        self.assertIn("Bash(git status)", claude)
        self.assertIn("Bash(git diff --no-ext-diff --no-textconv:*)", claude)
        self.assertIn("Bash(python -m unittest discover -s tests)", claude)
        allowed_start = claude.index("--allowedTools") + 1
        allowed_end = claude.index("--disallowedTools")
        self.assertNotIn("Bash", claude[allowed_start:allowed_end])
        self.assertIn("--disallowedTools", claude)
        self.assertNotIn("--disallowed-tools", claude)
        self.assertIn("Bash(curl:*)", claude)

        gemini, _ = cli_provider_command(
            "gemini", "develop", permission_policy={
                **policy, "git_read": False, "run_checks": False,
            },
        )
        self.assertEqual(
            gemini[gemini.index("--allowed-tools") + 1],
            "read_file,list_directory,grep_search,replace,write_file",
        )
        self.assertNotIn("run_shell_command", " ".join(gemini))

        gemini_trusted, _ = cli_provider_command(
            "gemini", "develop", permission_policy=policy,
            check_commands=[["python", "-m", "unittest"]],
        )
        gemini_allowed = gemini_trusted[gemini_trusted.index("--allowed-tools") + 1]
        for command in ("status", "log"):
            self.assertIn(f"ShellTool(git {command})", gemini_allowed)
        for command in ("diff", "show"):
            self.assertIn(
                f"ShellTool(git {command} --no-ext-diff --no-textconv)",
                gemini_allowed,
            )
        self.assertIn("auto_edit", gemini_trusted)
        self.assertNotIn("run_shell_command", " ".join(gemini_trusted))
        gemini_sandboxed, _ = cli_provider_command(
            "gemini", "develop", permission_policy=policy, sandbox_enabled=True,
        )
        self.assertNotIn("run_shell_command", " ".join(gemini_sandboxed))
        gemini_networked, _ = cli_provider_command(
            "gemini", "develop", permission_policy=policy,
            sandbox_enabled=True, sandbox_allow_network=True,
        )
        self.assertNotIn("run_shell_command", " ".join(gemini_networked))

        copilot, _ = cli_provider_command(
            "copilot", "develop", permission_policy=policy,
            worktree=Path("/tmp/agentic-worktree"),
            check_commands=[["python", "-m", "unittest", "discover", "-s", "tests"]],
        )
        self.assertIn("--allow-tool", copilot)
        self.assertIn("write", copilot)
        self.assertEqual(copilot[copilot.index("--allow-tool") + 1], "write")
        nested_file = Path("/tmp/agentic-worktree") / "src" / "module.py"
        self.assertNotIn(f"write({nested_file})", copilot)
        self.assertNotIn("write(/tmp/agentic-worktree)", copilot)
        self.assertNotIn("write(/tmp/agentic-worktree/**)", copilot)
        self.assertNotIn("bash", copilot[copilot.index("--available-tools") + 1])
        self.assertNotIn("shell(", " ".join(copilot))
        self.assertIn("--disallow-temp-dir", copilot)
        self.assertNotIn("--deny-tool", copilot)

        claude_default, _ = cli_provider_command("claude", "default")
        self.assertNotIn("Bash(gh:*)", claude_default)
        for legacy in ("bash(git status)", "edit(/tmp/agentic-worktree)", "create(/tmp/agentic-worktree)", "bash(python -m unittest discover -s tests)", "bash(curl:*)"):
            self.assertNotIn(legacy, copilot)

        sandboxed_copilot, _ = cli_provider_command(
            "copilot", "develop", permission_policy=policy,
            worktree=Path("/tmp/agentic-worktree"), sandbox_enabled=True,
        )
        self.assertIn("write", sandboxed_copilot)
        self.assertNotIn("write(/workspace/**)", sandboxed_copilot)
        self.assertNotIn("write(/tmp/agentic-worktree)", sandboxed_copilot)

    def test_copilot_developer_shell_policy_is_noninteractive_and_exact(self):
        command, _ = cli_provider_command(
            "copilot", "develop", permission_policy={
                "edit_files": True, "create_files": True,
                "git_read": True, "run_checks": True,
            },
            check_commands=[["python", "-m", "unittest"]],
        )

        self.assertIn("--no-ask-user", command)
        self.assertNotIn("bash", command[command.index("--available-tools") + 1])
        self.assertNotIn("shell(", " ".join(command))

    def test_claude_tools_option_is_one_comma_separated_argv_value(self):
        command, _ = cli_provider_command("claude", "develop")

        tools_index = command.index("--tools")
        self.assertEqual(
            command[tools_index:tools_index + 2],
            ["--tools", "Read,Glob,Grep,Edit,Write,Bash"],
        )
        self.assertNotIn("Read", command[tools_index + 2:])
        self.assertNotIn("Bash", command[tools_index + 2:])

    def test_native_provider_scopes_claude_rules_to_sandbox_worktree(self):
        command, _ = cli_provider_command(
            "claude", "develop", sandbox_enabled=True,
        )
        for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
            self.assertIn(f"{tool}(//workspace/**)", command)
            self.assertNotIn(f"{tool}(/workspace/**)", command)
        self.assertNotIn("Read", command[command.index("--allowedTools") + 1:])

    def test_native_provider_network_opt_in_adds_explicit_claude_rules(self):
        policy = {
            "read_files": False, "search_files": False, "edit_files": False,
            "create_files": False, "git_read": False, "run_checks": False,
            "network": True, "allow_dangerous": False,
        }
        command, _ = cli_provider_command(
            "claude", "networked", permission_policy=policy,
        )
        self.assertEqual(
            command[command.index("--tools") + 1],
            "Bash,WebFetch,WebSearch",
        )
        self.assertIn("WebFetch", command)
        self.assertIn("WebSearch", command)
        self.assertIn("Bash(curl:*)", command)
        self.assertIn("Bash(ssh:*)", command)
        self.assertNotIn("Bash(gh:*)", command)
        self.assertNotIn("Bash(curl:*)", command[command.index("--disallowedTools") + 1:])

    def test_native_provider_network_opt_in_adds_explicit_copilot_rules(self):
        policy = {
            "read_files": False, "search_files": False, "edit_files": False,
            "create_files": False, "git_read": False, "run_checks": False,
            "network": True, "allow_dangerous": False,
        }
        with self.assertRaisesRegex(ValueError, "Copilot cannot represent network=true"):
            cli_provider_command(
                "copilot", "networked", permission_policy=policy,
            )

    def test_gemini_reviewer_never_receives_shell_tool_in_isolated_sandbox(self):
        command, _ = cli_provider_command(
            "gemini", "review", readonly=True, sandbox_enabled=True,
        )
        allowed = command[command.index("--allowed-tools") + 1]
        self.assertIn("read_file", allowed)
        self.assertNotIn("ShellTool", allowed)
        self.assertNotIn("run_shell_command", command)

    def test_gemini_default_policy_is_usable_in_trusted_mode(self):
        command, _ = cli_provider_command(
            "gemini", "develop", check_commands=[["python", "-m", "unittest"]],
        )
        self.assertIn("auto_edit", command)
        self.assertNotIn("run_shell_command", " ".join(command))

    def test_gemini_rejects_network_without_a_scoped_shell_translation(self):
        policy = {
            "read_files": True, "search_files": True, "edit_files": True,
            "create_files": True, "git_read": True, "run_checks": False,
            "network": True, "allow_dangerous": False,
        }
        for sandbox_enabled in (False, True):
            with self.subTest(sandbox_enabled=sandbox_enabled):
                with self.assertRaisesRegex(ValueError, "cannot represent network=true"):
                    cli_provider_command(
                        "gemini", "networked", permission_policy=policy,
                        sandbox_enabled=sandbox_enabled, sandbox_allow_network=True,
                    )

    def test_copilot_rejects_asymmetric_file_capabilities(self):
        for edit_files, create_files in ((True, False), (False, True)):
            with self.subTest(edit_files=edit_files, create_files=create_files):
                with self.assertRaisesRegex(ValueError, "independent edit_files"):
                    cli_provider_command(
                        "copilot", "develop",
                        permission_policy={
                            "edit_files": edit_files,
                            "create_files": create_files,
                        },
                    )

    def test_claude_rejects_create_only_file_capability(self):
        with self.assertRaisesRegex(ValueError, "Claude cannot represent create_files"):
            cli_provider_command(
                "claude", "develop",
                permission_policy={"edit_files": False, "create_files": True},
            )

    def test_dangerous_provider_modes_are_explicit(self):
        policy = {"allow_dangerous": True}
        claude, _ = cli_provider_command(
            "claude", "develop", permission_policy=policy,
        )
        copilot, _ = cli_provider_command(
            "copilot", "develop", permission_policy=policy,
        )
        self.assertIn("--dangerously-skip-permissions", claude)
        self.assertIn("--allow-all-tools", copilot)
        self.assertIn("--allow-all-paths", copilot)
        self.assertIn("bash", copilot[copilot.index("--available-tools") + 1])

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
