import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentic
from agentic_sandbox import sandbox_command, validate_sandbox


class ReadOnlyBoundaryTests(unittest.TestCase):
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
            allowed, reason = agentic._openrouter_command_allowed(argv, readonly=True)
            self.assertFalse(allowed, argv)
            self.assertTrue(reason, argv)

    def test_readonly_allows_bounded_inspection(self):
        for argv in (["git", "status"], ["git", "diff"], ["rg", "needle", "."]):
            allowed, reason = agentic._openrouter_command_allowed(argv, readonly=True)
            self.assertTrue(allowed, reason)

    def test_file_tool_resolves_before_sensitive_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("TOKEN=private\n", encoding="utf-8")
            result = agentic._openrouter_tool_call(
                root, "read_file", {"path": "../secret"}, readonly=True
            )
            self.assertIn("ERROR:", result)
            result = agentic._openrouter_tool_call(
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

    def test_sandbox_review_forces_network_off(self):
        settings = validate_sandbox({
            "enabled": True, "engine": "docker", "image": "test-image",
            "allow_network": True,
        })
        command = sandbox_command(["rg", "needle", "."], Path("/tmp/worktree"), settings, read_only=True)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("/tmp/worktree:/workspace:ro", command)

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
        self.assertIn(f"{common}:/agentic-git-common:ro", command)
        self.assertIn(f"{git_dir}:/agentic-git-worktree:rw", command)
        self.assertIn("GIT_DIR=/agentic-git-worktree", command)
        self.assertIn("GIT_COMMON_DIR=/agentic-git-common", command)
        self.assertIn("GIT_WORK_TREE=/workspace", command)

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
