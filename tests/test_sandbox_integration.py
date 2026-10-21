import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests
from agentic_runner import config as agentic_config
from agentic_runner.sandbox import sandbox_command, validate_sandbox


@unittest.skipUnless(
    os.environ.get("AGENTIC_SANDBOX_TEST_IMAGE"),
    "Docker sandbox integration tests require AGENTIC_SANDBOX_TEST_IMAGE",
)
class DockerSandboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.image = os.environ["AGENTIC_SANDBOX_TEST_IMAGE"]

    def run_sandbox(self, worktree: Path, code: str, *, read_only=False, allow_network=False):
        settings = validate_sandbox({
            "enabled": True,
            "engine": "docker",
            "image": self.image,
            "allow_network": allow_network,
        })
        command = sandbox_command(
            ["python", "-c", code], worktree, settings, read_only=read_only,
        )
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def test_developer_can_write_worktree_but_reviewer_cannot(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            (worktree / ".git").mkdir()
            developer = self.run_sandbox(
                worktree,
                "from pathlib import Path; Path('/workspace/developer.txt').write_text('ok')",
            )
            self.assertEqual(developer.returncode, 0, developer.stderr)
            reviewer = self.run_sandbox(
                worktree,
                "from pathlib import Path; Path('/workspace/reviewer.txt').write_text('blocked')",
                read_only=True,
            )
            self.assertNotEqual(reviewer.returncode, 0)
            self.assertFalse((worktree / "reviewer.txt").exists())

    def test_container_root_and_git_metadata_are_read_only_but_tmp_is_writable(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            (worktree / ".git").mkdir()
            result = self.run_sandbox(
                worktree,
                "from pathlib import Path; "
                "Path('/tmp/allowed.txt').write_text('ok'); "
                "Path('/etc/blocked.txt').write_text('blocked')",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((worktree / ".git" / "blocked.txt").exists())

            metadata = self.run_sandbox(
                worktree,
                "from pathlib import Path; Path('/agentic-git/blocked.txt').write_text('blocked')",
            )
            self.assertNotEqual(metadata.returncode, 0)

    def test_linked_worktree_git_metadata_is_read_only_for_developers(self):
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

            result = self.run_sandbox(
                worktree,
                "from pathlib import Path; "
                "Path('/agentic-git-worktree/blocked.txt').write_text('blocked')",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((git_dir / "blocked.txt").exists())

    def test_network_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_sandbox(
                Path(temp),
                "import socket; socket.create_connection(('198.51.100.1', 80), timeout=2)",
            )
            self.assertNotEqual(result.returncode, 0)

    def test_isolated_mode_fails_closed_when_engine_is_unavailable(self):
        with patch.object(agentic_config.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cannot continue"):
                agentic_config.preflight_security(
                    "isolated", {"enabled": True, "engine": "docker"},
                )


if __name__ == "__main__":
    unittest.main()
