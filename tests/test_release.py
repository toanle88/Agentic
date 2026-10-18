import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import release


class ReleaseScriptTests(unittest.TestCase):
    def test_package_version_reads_literal_without_executing_module(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "agentic.py").write_text('__version__ = "1.2.3"\nraise RuntimeError("not executed")\n')
            self.assertEqual(release.package_version(root), "1.2.3")

    def test_release_tag_requires_workflow_compatible_semver(self):
        self.assertEqual(release.release_tag("0.2.0"), "v0.2.0")
        with self.assertRaises(release.ReleaseError):
            release.release_tag("0.2.0rc1")

    def test_dry_run_does_not_create_or_push_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "agentic.py").write_text('__version__ = "0.2.0"\n')
            calls = []

            def fake_run(argv, *, root, capture_output=False):
                calls.append(tuple(argv))
                command = tuple(argv)
                output = {
                    ("git", "rev-parse", "--show-toplevel"): str(root),
                    ("git", "branch", "--show-current"): "main",
                    ("git", "remote", "get-url", "origin"): "https://example.test/agentic.git",
                    ("git", "status", "--porcelain=v1", "--untracked-files=all"): "",
                    ("git", "rev-parse", "HEAD"): "abc123",
                    ("git", "rev-parse", "refs/remotes/origin/main"): "abc123",
                    ("git", "tag", "--list", "v0.2.0"): "",
                    ("git", "ls-remote", "--tags", "origin", "refs/tags/v0.2.0"): "",
                }.get(command, "")
                return release.subprocess.CompletedProcess(argv, 0, output, "")

            with patch.object(release, "_run", side_effect=fake_run), patch.object(
                release, "_run_local_checks"
            ) as checks:
                self.assertEqual(release.main(["--dry-run"], root=root), 0)

            checks.assert_called_once_with(root)
            self.assertNotIn(("git", "tag", "-a", "v0.2.0", "-m", "Release v0.2.0"), calls)
            self.assertNotIn(("git", "push", "origin", "v0.2.0"), calls)

    def test_local_checks_compile_runtime_modules_without_running_unit_tests(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recorded_commands = []

            def fake_run(argv, *, root, capture_output=False):
                recorded_commands.append(tuple(argv))
                return release.subprocess.CompletedProcess(argv, 0, "", "")

            output = StringIO()
            with patch.object(release, "_run", side_effect=fake_run), redirect_stdout(output):
                release._run_local_checks(root)

            rendered = output.getvalue()
            self.assertIn("Compile checks passed", rendered)
            self.assertFalse(any("unittest" in argv for argv in recorded_commands))
            self.assertTrue(any("py_compile" in argv for argv in recorded_commands))

    def test_local_checks_raise_on_compile_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_run(argv, *, root, capture_output=False):
                return release.subprocess.CompletedProcess(argv, 1, "", "SyntaxError: invalid syntax")

            with patch.object(release, "_run", side_effect=fake_run):
                with self.assertRaises(release.ReleaseError) as cm:
                    release._run_local_checks(root)
            self.assertIn("Python compilation failed with exit 1", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
