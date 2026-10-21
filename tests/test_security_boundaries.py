import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import execution as agentic_execution
from agentic_runner import dashboard_data as agentic_dashboard_data
from agentic_runner import openrouter as agentic_openrouter
from agentic_runner.openrouter_tools import _openrouter_tool_call
from agentic_runner.config import PROVIDER_API_KEY_ENV, provider_process_environment
from agentic_runner.sandbox import sandbox_command, validate_sandbox
from agentic_runner.security import redact_secrets
from agentic_runner.execution_stream import streamed_command
from agentic_runner.persistence import atomic_write_text
from agentic_runner.telemetry import record_issue_metadata, record_run_instruction, record_usage
from agentic_runner.review import record_review_findings
from agentic_runner.models import Issue, Run


class ReadOnlyBoundaryTests(unittest.TestCase):
    def test_secret_canary_formats_are_redacted(self):
        value = "canary-secret-value-123456"
        payload = (
            f'OPENAI_API_KEY={value}\n'
            f'{{"api_key": "{value}"}}\n'
            f'Authorization: Bearer {value}\n'
        )
        with patch.dict("os.environ", {}, clear=True):
            redacted = redact_secrets(payload, extra_values=[value])
        self.assertNotIn(value, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 3)

    def test_provider_specific_credentials_are_redacted_in_adversarial_forms(self):
        credentials = {
            "OPENAI_API_KEY": "sk-proj-synthetic-openai-123456",
            "GEMINI_API_KEY": "AIza-synthetic-gemini-123456",
            "ANTHROPIC_API_KEY": "sk-ant-synthetic-claude-123456",
            "GH_TOKEN": "ghp-synthetic-copilot-123456",
            "OPENROUTER_API_KEY": "or-synthetic-router-123456",
            "CUSTOM_PROVIDER_TOKEN": "custom-synthetic-token-123456",
        }
        payloads = (
            'OPENAI_API_KEY="{}"\nAuthorization:\n Bearer {}\n'.format(
                credentials["OPENAI_API_KEY"], credentials["OPENAI_API_KEY"]
            ),
            '{{"GEMINI_API_KEY": "{}"}}\ncommand output: token={}\n'.format(
                credentials["GEMINI_API_KEY"], credentials["GEMINI_API_KEY"]
            ),
            "ANTHROPIC_API_KEY: '{}'\nerror: api-key={}\n".format(
                credentials["ANTHROPIC_API_KEY"], credentials["ANTHROPIC_API_KEY"]
            ),
            "GH_TOKEN={}\nGITHUB_TOKEN: {}\n".format(
                credentials["GH_TOKEN"], credentials["GH_TOKEN"]
            ),
            "OPENROUTER_API_KEY={}\nBearer {}\n".format(
                credentials["OPENROUTER_API_KEY"], credentials["OPENROUTER_API_KEY"]
            ),
            "CUSTOM_PROVIDER_TOKEN={}\ncustom output: {}\n".format(
                credentials["CUSTOM_PROVIDER_TOKEN"], credentials["CUSTOM_PROVIDER_TOKEN"]
            ),
        )
        with patch.dict("os.environ", credentials, clear=True):
            self.assertEqual(
                {provider: PROVIDER_API_KEY_ENV[provider] for provider in (
                    "codex", "gemini", "claude", "copilot", "openrouter",
                )},
                {
                    "codex": "OPENAI_API_KEY",
                    "gemini": "GEMINI_API_KEY",
                    "claude": "ANTHROPIC_API_KEY",
                    "copilot": "GH_TOKEN",
                    "openrouter": "OPENROUTER_API_KEY",
                },
            )
            custom_environment = provider_process_environment(
                "claude", "CUSTOM_PROVIDER_TOKEN",
            )
            self.assertEqual(custom_environment["ANTHROPIC_API_KEY"], credentials["CUSTOM_PROVIDER_TOKEN"])
            for payload in payloads:
                redacted = redact_secrets(payload)
                for value in credentials.values():
                    self.assertNotIn(value, redacted)

    def test_configured_credential_with_nonsemantic_name_is_redacted_from_provider_streams(self):
        value = "custom-configured-credential-123456"
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"SERVICE_VALUE": value}, clear=True
        ):
            root = Path(temp)
            script = root / "provider.py"
            script.write_text(
                "import json\n"
                f"print(json.dumps({{'type': 'message', 'content': 'token={value}'}}), flush=True)\n",
                encoding="utf-8",
            )
            with patch(
                "agentic_execution.cli_provider_command",
                return_value=([sys.executable, str(script)], True),
            ):
                ok, message = agentic_execution.run_cli_provider(
                    root, "review", output=root / "review.txt", provider="claude",
                    readonly=True, api_key_env="SERVICE_VALUE", timeout=10,
                    permission_policy={"read_files": True},
                )

            self.assertTrue(ok)
            self.assertNotIn(value, message)
            for path in (root / "review.txt", root / "review.log", root / "review.jsonl"):
                self.assertNotIn(value, path.read_text(encoding="utf-8"))

    def test_synthetic_credentials_stay_out_of_streams_and_durable_channels(self):
        value = "sk-proj-synthetic-channel-123456"
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"OPENAI_API_KEY": value}, clear=True
        ):
            root = Path(temp)
            result = streamed_command(
                [sys.executable, "-c", f"print('api_key={value}')"],
                root, label="DEVELOPER", log_file=root / "developer-001.log",
                timeout=10, codex_json=True,
            )
            self.assertNotIn(value, result.stdout)
            for path in (root / "developer-001.log", root / "developer-001.jsonl"):
                self.assertNotIn(value, path.read_text(encoding="utf-8"))

            command_result = _openrouter_tool_call(
                root, "run_command",
                {"argv": [sys.executable, "-c", f"print('token={value}')"]},
                readonly=False,
            )
            self.assertNotIn(value, command_result)

            record_issue_metadata(
                root, number=146, title=f'api_key={value}',
                state="open", body=f"Bearer {value}",
            )
            record_run_instruction(root, f'{{"OPENAI_API_KEY": "{value}"}}')
            record_usage(
                root, "developer", {"provider": "codex"},
                invocation={"diagnostic": f"token={value}"},
            )
            record_review_findings(
                root, {"approved": False, "findings": [f"secret={value}"]},
            )
            run = Run(146, "agent/issue-146-synthetic123", root)
            issue = Issue(146, "Synthetic fixture", "description")
            agentic.record_review_approval(
                run, issue, "main", "base-sha", "patch-digest",
                {"summary": f"api_key={value}", "skipped": [f"token={value}"]},
                {"reviewer_agent": {"provider": "codex"}}, root,
            )
            with patch(
                "agentic.prepare_patch",
                return_value=("base-sha", "patch-digest", "summary"),
            ):
                agentic.record_manual_approval(
                    run, issue, "main", {"max_files": 10}, root, f"secret={value}",
                )
            atomic_write_text(root / "resume-task.md", f"command output: {value}\n")

            dashboard_run = root / "agent-issue-146-dashboard123"
            dashboard_run.mkdir()
            atomic_write_text(dashboard_run / "developer-001.txt", f"api_key={value}\n")
            self.assertNotIn(value, json.dumps(agentic_dashboard_data.collect_runs(root)))

            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(value, path.read_text(encoding="utf-8"), path)

    def test_readonly_rejects_mutating_or_escaping_commands(self):
        blocked = (
            ["sed", "-i", "s/x/y/", "file.txt"],
            ["find", ".", "-exec", "touch", "outside", ";"],
            ["cat", "../secret"],
            ["git", "-C", "../other", "status"],
            ["git", "diff", "--output=../outside"],
            ["git", "branch", "-D", "main"],
            ["git", "branch", "--move", "main", "renamed"],
            ["rg", "--pre=sh", "needle", "."],
            ["rg", "--hostname-bin=sh", "needle", "."],
        )
        for argv in blocked:
            allowed, reason = agentic_openrouter._openrouter_command_allowed(argv, readonly=True)
            self.assertFalse(allowed, argv)
            self.assertTrue(reason, argv)

    def test_readonly_allows_bounded_inspection(self):
        for argv in (["git", "status"], ["git", "diff"], ["rg", "needle", "."]):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(argv, readonly=True)
            self.assertTrue(allowed, reason)

    def test_git_read_is_an_explicit_allowlist_for_developer_and_reviewer(self):
        allowed_git = ("status", "diff", "log", "show")
        blocked_git = ("add", "config", "merge", "rebase", "commit", "push")
        for readonly in (False, True):
            for subcommand in allowed_git:
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    ["git", subcommand], readonly=readonly,
                )
                self.assertTrue(allowed, (readonly, subcommand, reason))
            for subcommand in blocked_git:
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    ["git", subcommand], readonly=readonly,
                )
                self.assertFalse(allowed, (readonly, subcommand))

    def test_openrouter_permission_policy_blocks_capabilities(self):
        restricted = {
            "read_files": True,
            "search_files": True,
            "edit_files": False,
            "create_files": False,
            "git_read": True,
            "run_checks": False,
            "network": False,
            "allow_dangerous": False,
        }
        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ["python", "-m", "unittest"], readonly=False,
            permission_policy=restricted,
        )
        self.assertFalse(allowed)
        self.assertIn("disabled", reason)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = agentic_openrouter._openrouter_tool_call(
                root, "write_file", {"path": "new.txt", "content": "nope"},
                readonly=False, permission_policy=restricted,
            )
        self.assertIn("disabled", result)

    def test_openrouter_commands_require_write_capabilities_or_configured_checks(self):
        restricted = {
            "read_files": True, "search_files": True,
            "edit_files": False, "create_files": False,
            "git_read": True, "run_checks": True,
            "network": False, "allow_dangerous": False,
        }
        for argv in (["touch", "new.txt"], ["rm", "existing.txt"]):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(
                list(argv), readonly=False, permission_policy=restricted,
            )
            self.assertFalse(allowed, (argv, reason))
            self.assertIn("edit_files", reason)
        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ["perl", "-i", "-e", "s/x/y/", "file.txt"],
            readonly=False, permission_policy=restricted,
        )
        self.assertFalse(allowed)
        self.assertIn("code-evaluation", reason)
        configured = [["python", "-m", "unittest"]]
        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            configured[0], readonly=False, permission_policy=restricted,
            check_commands=configured,
        )
        self.assertTrue(allowed, reason)
        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ["python", "-m", "unittest", "discover"], readonly=False,
            permission_policy=restricted, check_commands=configured,
        )
        self.assertFalse(allowed)
        self.assertIn("configured checks", reason)

    def test_openrouter_permission_policy_network_is_effective_without_legacy_flag(self):
        policy = {
            "read_files": True, "search_files": True, "edit_files": True,
            "create_files": True, "git_read": True, "run_checks": True,
            "network": True, "allow_dangerous": False,
        }
        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ["curl", "https://example.test"], readonly=False,
            permission_policy=policy,
        )
        self.assertTrue(allowed, reason)

    def test_openrouter_reviewer_checks_policy_before_readonly_classification(self):
        policy = {
            "read_files": True,
            "search_files": False,
            "edit_files": False,
            "create_files": False,
            "git_read": False,
            "run_checks": False,
            "network": False,
            "allow_dangerous": False,
        }
        for argv in (["git", "status"], ["git", "diff"], ["rg", "needle", "."]):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(
                list(argv), readonly=True, permission_policy=policy,
            )
            self.assertFalse(allowed, argv)
            self.assertIn("disabled", reason)

    def test_file_tool_resolves_before_sensitive_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("TOKEN=private\n", encoding="utf-8")
            result = agentic_openrouter._openrouter_tool_call(
                root, "read_file", {"path": "../secret"}, readonly=True
            )
            self.assertIn("ERROR:", result)
            result = agentic_openrouter._openrouter_tool_call(
                root, "read_file", {"path": ".env"}, readonly=True
            )
            self.assertIn("protected file", result)

    def test_sandbox_command_is_explicit_and_network_disabled_by_default(self):
        settings = validate_sandbox({"enabled": True, "engine": "docker", "image": "test-image"})
        command = sandbox_command(["python", "-m", "unittest"], Path("/tmp/worktree"), settings)
        self.assertEqual(command[0:2], ["docker", "run"])
        self.assertIn("--read-only", command)
        self.assertIn("--tmpfs", command)
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("test-image", command)
        self.assertEqual(command[command.index("--entrypoint") + 1], "python")
        self.assertEqual(command[command.index("test-image") + 1:], ["-m", "unittest"])

    def test_sandbox_read_only_and_network_policies_are_independent(self):
        settings = validate_sandbox({
            "enabled": True, "engine": "docker", "image": "test-image",
            "allow_network": True,
        })
        developer_command = sandbox_command(
            ["python", "-c", "pass"], Path("/tmp/worktree"), settings,
        )
        self.assertEqual(developer_command[developer_command.index("--network") + 1], "bridge")
        self.assertIn(f"{Path('/tmp/worktree').resolve()}:/workspace:rw", developer_command)

        command = sandbox_command(["rg", "needle", "."], Path("/tmp/worktree"), settings, read_only=True)
        self.assertEqual(command[command.index("--network") + 1], "bridge")
        self.assertIn(f"{Path('/tmp/worktree').resolve()}:/workspace:ro", command)

    def test_restricted_sandbox_forces_network_off(self):
        settings = validate_sandbox({
            "enabled": True, "engine": "docker", "image": "test-image",
            "allow_network": True,
        })
        command = sandbox_command(
            ["python", "-c", "pass"], Path("/tmp/worktree"), settings, restricted=True,
        )
        self.assertEqual(command[command.index("--network") + 1], "none")

        settings["allow_network"] = False
        command = sandbox_command(["rg", "needle", "."], Path("/tmp/worktree"), settings, read_only=True)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn(f"{Path('/tmp/worktree').resolve()}:/workspace:ro", command)

    def test_sandbox_mounts_linked_worktree_git_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            common = parent / "repo" / ".git"
            git_dir = common / "worktrees" / "task"
            worktree.mkdir()
            git_dir.mkdir(parents=True)
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            (git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            settings = validate_sandbox({
                "enabled": True, "engine": "docker", "image": "test-image",
            })
            command = sandbox_command(["git", "status"], worktree, settings)
        self.assertIn(f"{common.resolve()}:/agentic-git-common:ro", command)
        self.assertIn(f"{git_dir.resolve()}:/agentic-git-worktree:ro", command)
        self.assertNotIn(f"{git_dir.resolve()}:/agentic-git-worktree:rw", command)
        self.assertIn("GIT_DIR=/agentic-git-worktree", command)
        self.assertIn("GIT_COMMON_DIR=/agentic-git-common", command)
        self.assertIn("GIT_WORK_TREE=/workspace", command)

    def test_restricted_linked_worktree_git_pointer_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            common = parent / "repo" / ".git"
            git_dir = common / "worktrees" / "task"
            worktree.mkdir()
            git_dir.mkdir(parents=True)
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            (git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            settings = validate_sandbox({
                "enabled": True, "engine": "docker", "image": "test-image",
            })
            command = sandbox_command(
                ["git", "status"], worktree, settings, restricted=True,
            )
        self.assertIn(f"{(worktree / '.git').resolve()}:/workspace/.git:ro", command)

    def test_sandbox_ignores_unrelated_git_pointer(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            common = parent / "other-repo" / ".git"
            git_dir = common / "worktrees" / "task"
            worktree.mkdir()
            git_dir.mkdir(parents=True)
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            (git_dir / "gitdir").write_text(f"{parent / 'other-worktree' / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            settings = validate_sandbox({
                "enabled": True, "engine": "docker", "image": "test-image",
            })
            command = sandbox_command(["git", "status"], worktree, settings)
        self.assertNotIn("/agentic-git-common", command)
        self.assertNotIn("/agentic-git-worktree", command)

    def test_sandbox_passes_only_named_environment_credentials(self):
        settings = validate_sandbox({
            "enabled": True, "engine": "docker", "image": "test-image",
            "pass_env": ["OPENAI_API_KEY"],
        })
        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret-value"}, clear=True):
            command = sandbox_command(["codex", "--version"], Path("/tmp/worktree"), settings)
        self.assertIn("--env", command)
        env_index = command.index("--env")
        self.assertEqual(command[env_index + 1], "OPENAI_API_KEY")
        self.assertNotIn("secret-value", command)

    def test_sandbox_can_forward_a_provider_host_from_child_environment(self):
        settings = validate_sandbox({
            "enabled": True, "engine": "docker", "image": "test-image",
        })
        child_env = {"COPILOT_GH_HOST": "copilot.example.com"}
        command = sandbox_command(
            ["copilot", "--version"], Path("/tmp/worktree"), settings,
            pass_env=["COPILOT_GH_HOST"], env=child_env,
        )
        env_index = command.index("--env")
        self.assertEqual(command[env_index + 1], "COPILOT_GH_HOST")
        self.assertNotIn("copilot.example.com", command)

    def test_worktree_lock_rejects_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = agentic.Run(None, "agent/task-demo-abcdef123456", root / "worktree", "abcdef123456")
            state = root / "state"
            with agentic.worktree_lock(root, run, state):
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    with agentic.worktree_lock(root, run, state):
                        pass

    def test_sandbox_rewrites_nested_output_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            output_root = parent / "logs"
            worktree.mkdir()
            output_root.mkdir()
            settings = validate_sandbox({"enabled": True, "engine": "docker", "image": "test-image"})
            command = sandbox_command(
                ["codex", "--output-last-message", str(output_root / "developer-001.txt")],
                worktree, settings, output_root=output_root,
            )
            self.assertIn("/agentic-output/developer-001.txt", command)


if __name__ == "__main__":
    unittest.main()
