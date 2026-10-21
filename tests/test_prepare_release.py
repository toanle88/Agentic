import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import prepare_release


class PrepareReleaseTests(unittest.TestCase):
    def test_pull_request_guidance_describes_actions_release_and_local_fallback(self):
        body = prepare_release._pull_request_body("v1.0.1")

        self.assertIn("Actions → Release → Run workflow", body)
        self.assertIn("select `main`", body)
        self.assertIn("python scripts/release.py", body)

    def test_update_package_version_preserves_assignment_style(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "agentic.py"
            source.write_text("#!/usr/bin/env python3\n__version__ = '1.0.0'\n", encoding="utf-8")

            prepare_release.update_package_version(root, "1.0.1")

            self.assertEqual(source.read_text(encoding="utf-8"), "#!/usr/bin/env python3\n__version__ = '1.0.1'\n")

    def test_update_package_version_requires_one_literal_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "agentic.py").write_text("__version__ = '1.0.0'\n__version__ = '1.0.1'\n", encoding="utf-8")

            with self.assertRaises(prepare_release.ReleaseError):
                prepare_release.update_package_version(root, "1.0.2")

    def test_dry_run_does_not_mutate_or_require_github_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "agentic.py"
            source.write_text("__version__ = '1.0.0'\n", encoding="utf-8")
            output = StringIO()

            with patch.object(prepare_release, "_ensure_prepare_preconditions", return_value="abc123"), \
                    patch.object(prepare_release, "_run_local_checks") as checks, \
                    patch.object(prepare_release, "_ensure_github_auth") as auth, \
                    redirect_stdout(output):
                result = prepare_release.main(["1.0.1", "--dry-run"], root=root)

            self.assertEqual(result, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "__version__ = '1.0.0'\n")
            checks.assert_called_once_with(root)
            auth.assert_not_called()
            self.assertIn("Dry run complete", output.getvalue())

    def test_prepare_updates_commits_pushes_and_opens_pull_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "agentic.py"
            source.write_text("__version__ = '1.0.0'\n", encoding="utf-8")
            checked_calls = []

            def fake_checked(argv, *, root, capture_output=False):
                checked_calls.append(tuple(argv))
                output = "https://github.com/example/agentic/pull/12\n" if argv[:3] == ("gh", "pr", "create") else ""
                return subprocess.CompletedProcess(argv, 0, output, "")

            with patch.object(prepare_release, "_ensure_prepare_preconditions", return_value="abc123"), \
                    patch.object(prepare_release, "_ensure_github_auth"), \
                    patch.object(prepare_release, "_run_local_checks"), \
                    patch.object(prepare_release, "_run_checked", side_effect=fake_checked), \
                    patch.object(prepare_release, "_git", return_value=""), \
                    patch("builtins.input", return_value="y"), \
                    redirect_stdout(StringIO()):
                result = prepare_release.main(["1.0.1"], root=root)

            self.assertEqual(result, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "__version__ = '1.0.1'\n")
            self.assertIn(("git", "switch", "-c", "codex/release-v1.0.1"), checked_calls)
            self.assertIn(("git", "add", "agentic.py"), checked_calls)
            self.assertIn(("git", "push", "--set-upstream", "origin", "codex/release-v1.0.1"), checked_calls)
            self.assertTrue(any(command[:3] == ("gh", "pr", "create") for command in checked_calls))

    def test_prepare_release_checks_do_not_run_unit_tests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recorded_commands = []

            def fake_run(argv, *, root, capture_output=False):
                recorded_commands.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch("scripts.release._run", side_effect=fake_run), redirect_stdout(StringIO()):
                prepare_release._run_local_checks(root)

            self.assertFalse(any("unittest" in argv for argv in recorded_commands))
            self.assertTrue(any("py_compile" in argv for argv in recorded_commands))


if __name__ == "__main__":
    unittest.main()
