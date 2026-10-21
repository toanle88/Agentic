import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentic
import agentic_openrouter
from agentic_sandbox import sandbox_command, validate_sandbox
from agentic_security import redact_secrets


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
