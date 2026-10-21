import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import release


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


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


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_manual_release_is_guarded_to_main_and_uses_package_version(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn('if [[ "$REF" != "refs/heads/main" ]]', self.workflow)
        self.assertIn("actual=\"$(python -c 'import agentic; print(agentic.__version__)')\"", self.workflow)
        self.assertIn('[[ ! "$actual" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]]', self.workflow)
        self.assertIn('tag="v$actual"', self.workflow)

    def test_manual_release_checks_collisions_and_pushes_non_force_tag(self):
        self.assertIn('git ls-remote --exit-code --refs origin "refs/tags/$RELEASE_TAG"', self.workflow)
        self.assertIn('repos/${GITHUB_REPOSITORY}/releases/tags/${RELEASE_TAG}', self.workflow)
        self.assertIn('git push origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"', self.workflow)
        self.assertNotIn("git push --force", self.workflow)
        self.assertIn('ref: ${{ github.sha }}', self.workflow)
        self.assertIn("name: Download verified artifacts", self.workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.workflow)
        self.assertIn("environment: release", self.workflow)

    def test_workflow_permissions_allow_verification_checkout_but_limit_writes_to_publish(self):
        self.assertIn("permissions: {}", self.workflow)
        verify_job = self.workflow.split("  publish:\n", 1)[0]
        publish_job = "  publish:\n" + self.workflow.split("  publish:\n", 1)[1]
        self.assertIn("    permissions:\n      contents: read", verify_job)
        self.assertIn("    permissions:\n      contents: write", publish_job)
        self.assertNotIn("      contents: write", verify_job)

    def test_tag_trigger_keeps_shared_checks_and_publish_path(self):
        self.assertIn('tags:\n      - "v*.*.*"', self.workflow)
        self.assertIn("Run offline test suite", self.workflow)
        self.assertIn("Build release artifacts", self.workflow)
        self.assertIn("startsWith(github.ref, 'refs/tags/')", self.workflow)
        self.assertIn("github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", self.workflow)


if __name__ == "__main__":
    unittest.main()
