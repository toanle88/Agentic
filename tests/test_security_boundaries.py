import tempfile
import unittest
from pathlib import Path

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
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("test-image", command)

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
