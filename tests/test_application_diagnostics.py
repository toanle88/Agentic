"""Regression coverage for Agentic's application-level diagnostic log."""
import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps the source-layout test path.
from agentic_runner import application_diagnostics as app_diagnostics
from agentic_runner import cli
from agentic_runner import shell


class ApplicationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "project"
        self.repo.mkdir()
        self.state = root / "state"

    def _events(self):
        path = app_diagnostics.diagnostic_path(self.repo, self.state)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_handled_cli_error_is_persisted_without_argument_values(self):
        with patch.object(cli, "execute", side_effect=RuntimeError("api_key=not-a-secret-value")), \
             patch.object(cli.sys, "stderr", new_callable=StringIO) as stderr:
            result = cli.main([
                "--repo", str(self.repo), "--state-dir", str(self.state),
                "--issue", "42", "--instruction", "do not persist this issue text",
            ])

        self.assertEqual(result, 1)
        self.assertIn("ERROR:", stderr.getvalue())
        event = self._events()[-1]
        self.assertEqual(event["category"], "handled_error")
        self.assertEqual(event["exception"]["type"], "builtins.RuntimeError")
        self.assertNotIn("not-a-secret-value", json.dumps(event))
        self.assertNotIn("do not persist this issue text", json.dumps(event))
        self.assertNotIn("message", event["exception"])
        self.assertIn("--issue", event["command"]["options"])
        self.assertNotIn("42", json.dumps(event["command"]))

    def test_provider_output_in_exception_text_is_not_persisted(self):
        secret_like_output = "provider-output-secret-123456789"
        with patch.object(
            cli, "execute", side_effect=RuntimeError(secret_like_output)
        ), patch.object(cli.sys, "stderr", new_callable=StringIO):
            result = cli.main([
                "--repo", str(self.repo), "--state-dir", str(self.state), "--issue", "42",
            ])

        self.assertEqual(result, 1)
        self.assertNotIn(secret_like_output, json.dumps(self._events()[-1]))

    def test_uncaught_cli_exception_is_recorded_and_still_propagates(self):
        with patch.object(cli, "execute", side_effect=LookupError("unexpected failure")):
            with self.assertRaises(LookupError):
                cli.main(["--repo", str(self.repo), "--state-dir", str(self.state), "--issue", "42"])

        event = self._events()[-1]
        self.assertEqual(event["category"], "crash")
        self.assertEqual(event["exception"]["type"], "builtins.LookupError")
        self.assertIn("cli.py", event["exception"]["traceback"])

    def test_uncaught_standalone_shell_exception_is_recorded(self):
        with patch.object(shell, "run_shell", side_effect=LookupError("shell failure")):
            with self.assertRaises(LookupError):
                shell.main(["--repo", str(self.repo), "--state-dir", str(self.state)])

        event = self._events()[-1]
        self.assertEqual(event["category"], "crash")
        self.assertEqual(event["command"]["entrypoint"], "shell")

    def test_interactive_shell_error_is_persisted(self):
        instance = shell.AgenticShell(
            self.repo, Path(sys.executable), session_dir=self.state / "session", state_dir=self.state,
        )
        with patch.object(instance, "show_status", side_effect=RuntimeError("status failed")):
            self.assertTrue(instance.execute("/status"))

        event = self._events()[-1]
        self.assertEqual(event["category"], "handled_error")
        self.assertEqual(event["command"]["entrypoint"], "shell")
        self.assertEqual(event["command"]["action"], "status")

    def test_interactive_shell_unlisted_exception_is_persisted_and_handled(self):
        instance = shell.AgenticShell(
            self.repo, Path(sys.executable), session_dir=self.state / "session", state_dir=self.state,
        )
        with patch.object(instance, "show_status", side_effect=LookupError("status lookup failed")), \
             patch.object(shell, "_shell_print") as shell_print:
            self.assertTrue(instance.execute("/status"))

        shell_print.assert_called_once_with("Error: status lookup failed")
        event = self._events()[-1]
        self.assertEqual(event["category"], "handled_error")
        self.assertEqual(event["exception"]["type"], "builtins.LookupError")
        self.assertEqual(event["command"]["action"], "status")

    def test_logging_failure_does_not_mask_handled_error(self):
        with patch.object(app_diagnostics, "_append_line", side_effect=OSError("disk full")), \
             patch.object(cli, "execute", side_effect=RuntimeError("expected failure")), \
             patch.object(cli.sys, "stderr", new_callable=StringIO) as stderr:
            result = cli.main(["--repo", str(self.repo), "--state-dir", str(self.state), "--issue", "42"])

        self.assertEqual(result, 1)
        self.assertIn("expected failure", stderr.getvalue())
        self.assertNotIn("disk full", stderr.getvalue())

    def test_diagnostic_path_rejects_unsafe_state_directory(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        unsafe = self.state / "unsafe"
        unsafe.mkdir(parents=True)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "group/other writable"):
            app_diagnostics.diagnostic_path(self.repo, unsafe)

    def test_standalone_diagnostic_initializes_a_validated_state_hierarchy(self):
        path = app_diagnostics.record_diagnostic(
            "handled_error", exc=RuntimeError("standalone failure"),
            state_dir=self.state,
        )

        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertTrue(path.parent.is_dir())

    def test_diagnostic_log_rotates_at_bounded_size(self):
        with patch.object(app_diagnostics, "DIAGNOSTIC_MAX_BYTES", 700), \
             patch.object(app_diagnostics, "DIAGNOSTIC_BACKUPS", 2):
            for _ in range(8):
                app_diagnostics.record_diagnostic(
                    "handled_error", exc=RuntimeError("bounded failure"),
                    argv=["--issue", "42"], repo=self.repo, state_dir=self.state,
                )

        path = app_diagnostics.diagnostic_path(self.repo, self.state)
        self.assertLessEqual(path.stat().st_size, 700)
        self.assertTrue(path.with_name("agentic.log.1").exists())
        self.assertFalse(path.with_name("agentic.log.3").exists())

    def test_diagnostic_redacts_short_configured_credentials(self):
        config = self.repo / "agent_config.json"
        config.write_text(json.dumps({
            "developer_agent": {"api_key_env": "SERVICE_VALUE"},
        }), encoding="utf-8")
        with patch.dict(os.environ, {"SERVICE_VALUE": "tiny"}, clear=True):
            app_diagnostics.record_diagnostic(
                "handled_error", exc=RuntimeError("token=tiny"),
                argv=["--config", str(config)], repo=self.repo, state_dir=self.state,
            )
        self.assertNotIn("tiny", app_diagnostics.diagnostic_path(self.repo, self.state).read_text())


if __name__ == "__main__":
    unittest.main()
