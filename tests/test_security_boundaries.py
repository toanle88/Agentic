import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import execution as agentic_execution
from agentic_runner import dashboard_data as agentic_dashboard_data
from agentic_runner import openrouter as agentic_openrouter
import agentic_runner.sandbox as agentic_sandbox
import agentic_runner.execution_stream as execution_stream
from agentic_runner import application_diagnostics as application_diagnostics
from agentic_runner import persistence as persistence
from agentic_runner import shell as agentic_shell
from agentic_runner.openrouter_tools import _openrouter_tool_call
from agentic_runner.git_policy import GIT_LOG_METADATA_FORMAT
from agentic_runner.execution_providers import (
    _read_projection_file,
    _read_provider_result,
    _remove_staging_tree,
    _stage_provider_input,
    _stage_worktree,
    _stage_readonly_worktree,
    _sync_provider_worktree,
    _write_projection_file,
)
from agentic_runner import file_boundaries
from agentic_runner import worktrees as agentic_worktrees
from agentic_runner.config import PROVIDER_API_KEY_ENV, provider_process_environment
from agentic_runner.policy import provider_forbidden
from agentic_runner.sandbox import sandbox_command, validate_sandbox
from agentic_runner.security import (
    configured_environment_values,
    redact_secrets,
    restricted_test_environment,
)
from agentic_runner.execution_stream import streamed_command
from agentic_runner.persistence import BoundedAppendWriter, atomic_write_text
from agentic_runner.worktrees import (
    default_state_dir,
    ensure_state_paths,
    repository_state_key,
    state_paths,
)
from agentic_runner.telemetry import record_issue_metadata, record_run_instruction, record_usage
from agentic_runner.review import record_review_findings
from agentic_runner.models import Issue, Run


class ReadOnlyBoundaryTests(unittest.TestCase):
    def test_bounded_append_writer_rejects_replaceable_log_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside.log"
            outside.write_text("outside\n", encoding="utf-8")
            link = root / "link.log"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            with self.assertRaises(OSError):
                BoundedAppendWriter(link, max_bytes=100)
            hard_link = root / "hard-link.log"
            hard_link.hardlink_to(outside)
            with self.assertRaises(OSError):
                BoundedAppendWriter(hard_link, max_bytes=100)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_bounded_append_writer_revalidates_descriptor_before_append(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "output.log"
            writer = BoundedAppendWriter(path, max_bytes=100)
            try:
                try:
                    (root / "other.log").hardlink_to(path)
                except OSError as exc:
                    self.skipTest(f"hard links unavailable: {exc}")
                with self.assertRaisesRegex(OSError, "hard-linked"):
                    writer.write("must not append\n")
            finally:
                (root / "other.log").unlink(missing_ok=True)
                writer.close()

    def test_bounded_append_writer_is_nonblocking_for_special_files(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO creation is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            fifo = Path(temp) / "output.log"
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                BoundedAppendWriter(fifo, max_bytes=100)

    def test_bounded_append_writer_rejects_symlinked_parent_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            redirected = root / "redirected"
            try:
                redirected.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with self.assertRaises(OSError):
                BoundedAppendWriter(redirected / "output.log", max_bytes=100)
            self.assertFalse((outside / "output.log").exists())

    def test_bounded_append_writer_anchors_parent_before_path_replacement(self):
        if not persistence._supports_anchored_log_open():
            self.skipTest("anchored directory-descriptor opens are unavailable")
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            nested = base / "nested"
            nested.mkdir()
            outside = base / "outside"
            outside.mkdir()
            target = nested / "output.log"
            original = persistence._open_log_directory_at

            def replace_after_open(parent, name):
                descriptor = original(parent, name)
                if name == nested.name:
                    nested.rename(base / "nested-original")
                    try:
                        nested.symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                return descriptor

            with patch.object(
                persistence, "_open_log_directory_at", side_effect=replace_after_open,
            ):
                writer = BoundedAppendWriter(target, max_bytes=100)
                writer.write("anchored\n")
                writer.close()

            self.assertEqual(
                (base / "nested-original" / "output.log").read_text(encoding="utf-8"),
                "anchored\n",
            )
            self.assertFalse((outside / "output.log").exists())

    def test_legacy_log_open_rejects_parent_replacement_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            nested = base / "nested"
            nested.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "output.log").write_text("outside\n", encoding="utf-8")
            target = nested / "output.log"
            original_open = persistence.os.open

            def replace_before_open(path, flags, *args, **kwargs):
                if Path(path) == target:
                    nested.rename(base / "nested-original")
                    try:
                        nested.symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                return original_open(path, flags, *args, **kwargs)

            with patch.object(persistence, "_supports_anchored_log_open", return_value=False), \
                 patch.object(persistence.os, "open", side_effect=replace_before_open):
                with self.assertRaises(OSError):
                    BoundedAppendWriter(target, max_bytes=100)

            self.assertEqual(
                (outside / "output.log").read_text(encoding="utf-8"),
                "outside\n",
            )

    def test_diagnostic_rotation_does_not_follow_replaceable_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside.log"
            outside.write_text("outside\n", encoding="utf-8")
            diagnostic = root / "agentic.log"
            diagnostic.write_text("old\n" * 200, encoding="utf-8")
            backup = root / "agentic.log.1"
            try:
                backup.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")

            with patch.object(application_diagnostics, "DIAGNOSTIC_MAX_BYTES", 700):
                application_diagnostics._append_line(diagnostic, "new")

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
            self.assertEqual(
                diagnostic.with_name("agentic.log.1").read_text(encoding="utf-8"),
                "old\n" * 200,
            )
            self.assertEqual(diagnostic.read_text(encoding="utf-8"), "new\n")

    def test_bounded_append_writer_redacts_configured_nonsemantic_credentials(self):
        value = "service-value-secret-123456"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.log"
            writer = BoundedAppendWriter(
                path, max_bytes=200, redaction_values=[value],
            )
            writer.write(f"token={value}\n")
            writer.close()
            saved = path.read_text(encoding="utf-8")
        self.assertNotIn(value, saved)
        self.assertIn("[REDACTED]", saved)

    def test_bounded_append_writer_uses_binary_open_flags_for_byte_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.log"
            real_open = os.open
            binary_flag = 1 << 60
            observed_flags = []

            def open_without_synthetic_binary(path_arg, flags, *args, **kwargs):
                observed_flags.append(flags)
                return real_open(path_arg, flags & ~binary_flag, *args, **kwargs)

            with patch.object(persistence.os, "O_BINARY", binary_flag, create=True), \
                 patch.object(
                     persistence.os, "open", side_effect=open_without_synthetic_binary,
                 ):
                writer = BoundedAppendWriter(path, max_bytes=3)
                writer.write("a\n")
                writer.write("b\n")
                writer.close()

            self.assertTrue(any(flags & binary_flag for flags in observed_flags))
            self.assertLessEqual(len(path.read_bytes()), 3)

    def test_bounded_append_writer_retains_existing_suffix_before_overflow(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.log"
            path.write_bytes(b"ABCDEFGH")
            writer = BoundedAppendWriter(
                path, max_bytes=12, truncation_marker="MARK", retain_suffix=True,
                truncate=False,
            )
            writer.write(b"IJKLM")
            writer.close()

            saved = path.read_bytes()
        self.assertEqual(saved, b"MARKFGHIJKLM")

    def test_bounded_human_log_reports_marker_only_append_as_truncated(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            execution_stream, "MAX_HUMAN_LOG_BYTES",
            len(execution_stream.HUMAN_LOG_TRUNCATION_MARKER.encode("utf-8")) + 1,
        ):
            path = Path(temp) / "output.log"
            self.assertFalse(
                execution_stream._append_bounded_human_log(path, "too much output"),
            )
            self.assertIn(
                execution_stream.HUMAN_LOG_TRUNCATION_MARKER,
                path.read_text(encoding="utf-8"),
            )

    def test_state_directory_rejects_group_writable_posix_roots(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            root.mkdir(mode=0o700)
            root.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "group/other writable"):
                state_paths(Path(temp), root)

    def test_state_directory_accepts_a_private_state_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            root.mkdir(mode=0o700)
            paths = state_paths(Path(temp), root)
            self.assertFalse(paths.root.exists())
            if os.name != "nt":
                self.assertEqual(root.stat().st_mode & 0o077, 0)

    def test_state_directory_rejects_attacker_writable_existing_ancestor(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            ancestor = Path(temp) / "shared"
            ancestor.mkdir(mode=0o700)
            ancestor.chmod(0o777)
            missing = ancestor / "state"
            with self.assertRaisesRegex(ValueError, "group/other writable"):
                state_paths(Path(temp), missing)

    def test_state_directory_rejects_sticky_writable_non_system_ancestor(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            ancestor = Path(temp) / "shared"
            ancestor.mkdir(mode=0o700)
            ancestor.chmod(0o1777)
            with self.assertRaisesRegex(ValueError, "group/other writable"):
                state_paths(Path(temp), ancestor / "state")

    def test_state_directory_rejects_sticky_writable_state_root(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            state.mkdir(mode=0o700)
            state.chmod(0o1777)
            with self.assertRaisesRegex(ValueError, "group/other writable"):
                state_paths(Path(temp), state)

    def test_state_directory_initializer_revalidates_precreated_missing_root(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "project"
            repo.mkdir()
            state = root / "state"
            state.mkdir(mode=0o700)
            state.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "group/other writable"):
                ensure_state_paths(repo, state)

    def test_state_directory_initializer_rejects_root_made_unsafe_during_creation(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "project"
            repo.mkdir()
            state = root / "state"
            repository_root_name = repository_state_key(repo)
            real_mkdir = agentic_worktrees.os.mkdir
            changed = False

            def create_then_make_unsafe(name, mode=0o777, *, dir_fd=None):
                nonlocal changed
                real_mkdir(name, mode, dir_fd=dir_fd)
                if name == repository_root_name and not changed:
                    changed = True
                    (state / repository_root_name).chmod(0o777)

            with patch.object(
                agentic_worktrees.os, "mkdir", side_effect=create_then_make_unsafe,
            ) as mkdir_mock, patch.object(
                agentic_worktrees.os, "supports_dir_fd",
                set(agentic_worktrees.os.supports_dir_fd) | {mkdir_mock},
            ):
                with self.assertRaisesRegex(ValueError, "group/other writable"):
                    ensure_state_paths(repo, state)
            self.assertTrue(changed)

    def test_state_directory_initializer_rejects_precreated_unsafe_repository_root(self):
        if os.name == "nt":
            self.skipTest("POSIX ownership and mode policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            state.mkdir(mode=0o700)
            repo = root / "project"
            repo.mkdir()
            unsafe = state / repository_state_key(repo)
            unsafe.mkdir(mode=0o700)
            unsafe.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "group/other writable"):
                ensure_state_paths(repo, state)

    def test_state_directory_initializer_creates_private_missing_hierarchy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "project"
            repo.mkdir()
            state = root / "missing" / "state"
            paths = ensure_state_paths(repo, state)
            self.assertTrue(paths.root.is_dir())
            self.assertTrue(paths.runs.is_dir())
            if os.name != "nt":
                for directory in (state, paths.root, paths.runs):
                    self.assertEqual(directory.stat().st_mode & 0o077, 0)

    def test_state_directory_rejects_configured_symlink_root(self):
        if os.name == "nt":
            self.skipTest("symlink policy is not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            configured = root / "configured"
            try:
                configured.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with patch.dict("os.environ", {"AGENTIC_STATE_DIR": str(configured)}, clear=False):
                self.assertEqual(default_state_dir(), configured)
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    state_paths(root, None)

    def test_provider_result_reader_stays_on_open_parent_when_directory_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "invocation"
            root.mkdir()
            (root / "nested").mkdir()
            (root / "nested" / "result.txt").write_text("safe", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            (outside / "result.txt").write_text("outside", encoding="utf-8")
            original = file_boundaries._open_directory_at

            def replace_parent(descriptor, name):
                opened = original(descriptor, name)
                if name == "nested":
                    (root / "nested").rename(root / "nested-original")
                    try:
                        (root / "nested").symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                return opened

            with patch.object(file_boundaries, "_open_directory_at", side_effect=replace_parent):
                result = _read_provider_result(root, "nested/result.txt")
        self.assertEqual(result, "safe")

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

    def test_explicit_short_credentials_are_redacted(self):
        with patch.dict("os.environ", {"SERVICE_VALUE": "tiny"}, clear=True):
            safe, removed = restricted_test_environment(
                blocked_names=["SERVICE_VALUE"],
            )
            self.assertNotIn("SERVICE_VALUE", safe)
            self.assertEqual(removed, ["tiny"])
            self.assertEqual(
                redact_secrets("credential=tiny", extra_values=removed),
                "credential=[REDACTED]",
            )

    def test_configured_environment_values_include_short_nonsemantic_credentials(self):
        self.assertEqual(
            configured_environment_values(
                {"SERVICE_VALUE": "tiny", "EMPTY_VALUE": "", "OTHER": "ordinary"},
                ["SERVICE_VALUE", "EMPTY_VALUE"],
            ),
            ["tiny"],
        )

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
            self.assertEqual(custom_environment["CUSTOM_PROVIDER_TOKEN"], credentials["CUSTOM_PROVIDER_TOKEN"])
            self.assertNotIn("OPENAI_API_KEY", custom_environment)
            self.assertNotIn("GH_TOKEN", custom_environment)
            canonical_environment = provider_process_environment("codex", None)
            self.assertEqual(canonical_environment["OPENAI_API_KEY"], credentials["OPENAI_API_KEY"])
            self.assertNotIn("ANTHROPIC_API_KEY", canonical_environment)
            for payload in payloads:
                redacted = redact_secrets(payload)
                for value in credentials.values():
                    self.assertNotIn(value, redacted)

    def test_provider_environment_does_not_inherit_empty_nonsemantic_credential(self):
        with patch.dict(
            "os.environ",
            {
                "SERVICE_VALUE": "",
            },
            clear=True,
        ):
            environment = provider_process_environment("codex", "SERVICE_VALUE")
        self.assertNotIn("SERVICE_VALUE", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_provider_environment_uses_runtime_allowlist(self):
        with patch.dict(
            "os.environ",
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp/provider-home",
                "TMPDIR": "/tmp",
                "LANG": "C.UTF-8",
                "COPILOT_GH_HOST": "copilot.example.com",
                "PYTHONPATH": "/untrusted/code",
                "BASH_ENV": "/untrusted/bashrc",
                "NODE_OPTIONS": "--require /untrusted/module",
                "HOST_SECRET": "host-secret-value",
                "UNRELATED_SETTING": "host-setting",
            },
            clear=True,
        ):
            environment = provider_process_environment("codex", None)

        self.assertEqual(
            {name: environment[name] for name in ("PATH", "HOME", "TMPDIR", "LANG")},
            {
                "PATH": "/usr/bin", "HOME": "/tmp/provider-home",
                "TMPDIR": "/tmp", "LANG": "C.UTF-8",
            },
        )
        self.assertEqual(environment["COPILOT_GH_HOST"], "copilot.example.com")
        for name in (
            "PYTHONPATH", "BASH_ENV", "NODE_OPTIONS", "HOST_SECRET",
            "UNRELATED_SETTING",
        ):
            self.assertNotIn(name, environment)

    def test_provider_environment_rejects_control_credential_names(self):
        with patch.dict("os.environ", {"PYTHONPATH": "/untrusted/code"}, clear=True):
            with self.assertRaisesRegex(ValueError, "runtime or control"):
                provider_process_environment("codex", "PYTHONPATH")

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

    def test_configured_service_value_is_redacted_across_durable_artifacts(self):
        value = "service-value-cross-artifact-123456"
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"SERVICE_VALUE": value}, clear=True
        ):
            root = Path(temp)
            config = root / "agent_config.json"
            config.write_text(
                json.dumps({"developer_agent": {"api_key_env": "SERVICE_VALUE"}}),
                encoding="utf-8",
            )

            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), config=config,
                session_dir=root / "shell", state_dir=root / "state",
            )
            with patch.object(agentic_shell, "_shell_print"):
                job = shell._launch([
                    sys.executable, "-c",
                    "import os; print('shell-token=' + os.environ['SERVICE_VALUE'])",
                ])
            job.process.wait(timeout=10)
            if job.log_thread is not None:
                job.log_thread.join(timeout=10)

            provider_script = root / "provider.py"
            provider_script.write_text(
                "import json, os\n"
                "print(json.dumps({'type': 'message', 'content': "
                "'provider-token=' + os.environ['ANTHROPIC_API_KEY']}), flush=True)\n",
                encoding="utf-8",
            )
            with patch(
                "agentic_execution.cli_provider_command",
                return_value=([sys.executable, str(provider_script)], True),
            ):
                ok, provider_message = agentic_execution.run_cli_provider(
                    root, "review", output=root / "provider.txt", provider="claude",
                    readonly=False, api_key_env="SERVICE_VALUE", timeout=10,
                )
            self.assertTrue(ok)
            self.assertNotIn(value, provider_message)

            with (
                patch.dict(agentic.os.environ, {"SERVICE_VALUE": value}, clear=True),
                patch(
                    "agentic_openrouter._openrouter_request",
                    return_value={"choices": [{"message": {
                        "role": "assistant", "content": f"router-token={value}",
                    }}]},
                ),
            ):
                ok, message = agentic.run_openrouter(
                    root, "prompt", output=root / "router.txt", model="openai/test",
                    api_key_env="SERVICE_VALUE",
                )
            self.assertTrue(ok)
            self.assertNotIn(value, message)

            diagnostic = application_diagnostics.record_diagnostic(
                "handled_error", argv=[
                    "--config", str(config), "--repo", str(root),
                    "--state-dir", str(root / "state"),
                ], entrypoint=f"diagnostic-token={value}",
                repo=root, state_dir=root / "state",
            )
            self.assertEqual(diagnostic, application_diagnostics.diagnostic_path(
                root, root / "state",
            ))

            artifact_paths = [
                job.log_path,
                root / "provider.txt",
                root / "provider.log",
                root / "provider.jsonl",
                root / "router.txt",
                root / "router.log",
                root / "router.jsonl",
                diagnostic,
            ]
            for path in artifact_paths:
                self.assertTrue(path.exists(), path)
                self.assertNotIn(value, path.read_text(encoding="utf-8"), path)

    def test_native_readonly_providers_use_a_protected_file_free_view(self):
        value = 'native-review-secret-123456'
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"SERVICE_VALUE": value}, clear=True
        ):
            root = Path(temp)
            (root / ".env").write_text(value, encoding="utf-8")
            (root / "safe.py").write_text("print('safe')\n", encoding="utf-8")
            provider_script = (
                "import pathlib; print(f'env={pathlib.Path(\".env\").exists()} "
                "safe={pathlib.Path(\"safe.py\").exists()}')"
            )
            for provider in ("gemini", "claude", "copilot"):
                with self.subTest(provider=provider), patch(
                    "agentic_execution.cli_provider_command",
                    return_value=([sys.executable, "-c", provider_script], False),
                ):
                    ok, message = agentic_execution.run_cli_provider(
                        root, "review", output=root / f"{provider}.txt",
                        provider=provider, readonly=True, api_key_env="SERVICE_VALUE",
                        permission_policy={"read_files": True}, timeout=10,
                    )
                self.assertTrue(ok)
                self.assertIn("env=False safe=True", message)
                self.assertNotIn(value, message)

    def test_native_developer_providers_use_a_sanitized_writable_projection(self):
        protected = {
            ".env": "env-secret",
            "private.pem": "private-key",
            "AGENTS.md": "instructions",
            "agent_config.json": "configuration",
            "agentic.py": "runner",
            ".git/config": "git-config",
            ".github/workflows/ci.yml": "workflow",
        }
        script = (
            "from pathlib import Path\n"
            "protected = ['.env', 'private.pem', 'AGENTS.md', 'agent_config.json', 'agentic.py', "
            "'.git/config', '.github/workflows/ci.yml']\n"
            "print('protected=' + str(any(Path(path).exists() for path in protected)))\n"
            "Path('safe.py').write_text('changed\\n')\n"
            "Path('new.py').write_text('new\\n')\n"
            "for path in protected:\n"
            "    try: Path(path).write_text('provider-must-not-publish')\n"
            "    except OSError: pass\n"
        )
        for provider in ("codex", "gemini", "claude", "copilot"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "safe.py").write_text("original\n", encoding="utf-8")
                for relative, content in protected.items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                output = root / f"{provider}.txt"
                if provider == "codex":
                    def fake_stream(argv, cwd, **kwargs):
                        self.assertNotEqual(cwd, root)
                        self.assertFalse((cwd / ".env").exists())
                        (cwd / "safe.py").write_text("changed\n", encoding="utf-8")
                        (cwd / "new.py").write_text("new\n", encoding="utf-8")
                        self.assertFalse((cwd / "AGENTS.md").exists())
                        self.assertFalse((cwd / "agentic.py").exists())
                        result_path = Path(argv[argv.index("--output-last-message") + 1])
                        result_path.write_text("codex done", encoding="utf-8")
                        return CompletedProcess(argv, 0, stdout="")

                    with patch("agentic_execution.streamed_command", side_effect=fake_stream):
                        ok, message = agentic_execution.run_codex(
                            root, "develop", output=output, timeout=10,
                        )
                else:
                    with patch(
                        "agentic_execution.cli_provider_command",
                        return_value=([sys.executable, "-c", script], False),
                    ):
                        ok, message = agentic_execution.run_cli_provider(
                            root, "develop", output=output, provider=provider, timeout=10,
                        )
                self.assertTrue(ok)
                if provider != "codex":
                    self.assertIn("protected=False", message)
                else:
                    self.assertEqual(message, "codex done")
                self.assertEqual((root / "safe.py").read_text(encoding="utf-8"), "changed\n")
                self.assertEqual((root / "new.py").read_text(encoding="utf-8"), "new\n")
                for relative, content in protected.items():
                    self.assertEqual((root / relative).read_text(encoding="utf-8"), content)

    def test_self_hosted_projection_includes_runner_sources_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            projection = Path(temp) / "projection"
            root.mkdir()
            (root / "agentic.py").write_text("original runner\n", encoding="utf-8")
            workflow = root / ".github/workflows/ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("original workflow\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
            (root / ".env").write_text("secret\n", encoding="utf-8")

            def skip(filename):
                return provider_forbidden(
                    filename, allow_self_hosted_sources=True,
                )
            baseline = _stage_worktree(root, projection, skip=skip, read_only=False)

            self.assertTrue((projection / "agentic.py").is_file())
            self.assertTrue((projection / ".github/workflows/ci.yml").is_file())
            self.assertNotIn("AGENTS.md", baseline)
            self.assertNotIn(".env", baseline)

            (projection / "agentic.py").write_text("changed runner\n", encoding="utf-8")
            (projection / ".github/workflows/ci.yml").write_text(
                "changed workflow\n", encoding="utf-8",
            )
            _sync_provider_worktree(
                root, projection, baseline, allow_self_hosted_sources=True,
            )

            self.assertEqual(
                (root / "agentic.py").read_text(encoding="utf-8"),
                "changed runner\n",
            )
            self.assertEqual(workflow.read_text(encoding="utf-8"), "changed workflow\n")
            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "instructions\n")
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "secret\n")

    def test_codex_readonly_uses_a_protected_file_free_worktree(self):
        value = "codex-review-secret-123456"
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ", {"OPENAI_API_KEY": value}, clear=True
        ):
            root = Path(temp)
            (root / ".env").write_text(value, encoding="utf-8")
            (root / "safe.py").write_text("print('safe')\n", encoding="utf-8")

            def fake_stream(argv, cwd, **kwargs):
                self.assertNotEqual(cwd, root)
                self.assertFalse((cwd / ".env").exists())
                self.assertTrue((cwd / "safe.py").is_file())
                result_path = Path(argv[argv.index("--output-last-message") + 1])
                result_path.write_text("reviewed", encoding="utf-8")
                return CompletedProcess(argv, 0, stdout="")

            with patch(
                "agentic_execution.streamed_command", side_effect=fake_stream
            ):
                ok, message = agentic_execution.run_codex(
                    root, "review", output=root / "review.txt", readonly=True,
                    api_key_env="OPENAI_API_KEY", timeout=10,
                )

            self.assertTrue(ok)
            self.assertEqual(message, "reviewed")
            self.assertNotIn(value, (root / "review.txt").read_text(encoding="utf-8"))

    def test_provider_staging_rejects_hardlinked_inputs_and_aliases(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worktree'
            root.mkdir()
            outside = Path(temp) / 'outside-secret.txt'
            outside.write_text('external-secret\n', encoding='utf-8')
            alias = root / 'public.py'
            try:
                os.link(outside, alias)
            except OSError as exc:
                self.skipTest(f'hardlinks unavailable: {exc}')

            with self.assertRaisesRegex(RuntimeError, 'provider input'):
                _stage_provider_input(alias, Path(temp) / 'staged.txt')

            with self.assertRaisesRegex(RuntimeError, 'complete read-only worktree'):
                _stage_readonly_worktree(root, Path(temp) / 'review-worktree')

    def test_provider_staging_fails_closed_on_operational_copy_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worktree'
            root.mkdir()
            (root / 'public.py').write_text('print(1)\n', encoding='utf-8')
            with patch(
                'agentic_runner.execution_providers.open_relative_regular',
                side_effect=OSError('simulated staging failure'),
            ):
                with self.assertRaisesRegex(RuntimeError, 'complete read-only worktree'):
                    _stage_readonly_worktree(root, Path(temp) / 'review-worktree')

    def test_provider_staging_rejects_oversized_single_file_before_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            (root / "public.py").write_text("12345", encoding="utf-8")
            destination = Path(temp) / "review-worktree"
            with patch(
                "agentic_runner.execution_providers.MAX_PROVIDER_STAGE_BYTES", 4,
            ):
                with self.assertRaisesRegex(RuntimeError, "complete read-only worktree"):
                    _stage_readonly_worktree(root, destination)
            self.assertFalse(destination.exists())

    def test_provider_staging_cleanup_retries_after_clearing_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "review-worktree"
            destination.mkdir()
            partial = destination / "partial.py"
            partial.write_text("partial\n", encoding="utf-8")
            partial.chmod(0o444)
            attempts = []

            def remove_after_cleanup(path):
                attempts.append(path)
                self.assertTrue(Path(path).stat().st_mode & 0o200)
                Path(path).unlink()

            with patch(
                "agentic_runner.execution_providers.shutil.rmtree",
            ) as rmtree:
                _remove_staging_tree(destination)
                onerror = rmtree.call_args.kwargs["onerror"]
                onerror(remove_after_cleanup, str(partial), None)

            self.assertEqual(attempts, [str(partial)])
            self.assertFalse(partial.exists())

    def test_provider_staging_counts_bytes_written_after_metadata_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            (root / "public.py").write_text("12345", encoding="utf-8")
            destination = Path(temp) / "review-worktree"
            with patch(
                "agentic_runner.execution_providers.MAX_PROVIDER_STAGE_BYTES", 4,
            ), patch(
                "agentic_runner.execution_providers.revalidate_single_link_regular",
                return_value=SimpleNamespace(st_size=1),
            ):
                with self.assertRaisesRegex(RuntimeError, "complete read-only worktree"):
                    _stage_readonly_worktree(root, destination)
            self.assertFalse(destination.exists())

    def test_provider_sync_enforces_cumulative_file_and_byte_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            projection = Path(temp) / "projection"
            root.mkdir()
            projection.mkdir()
            (projection / "first.py").write_text("123\n", encoding="utf-8")
            (projection / "second.py").write_text("456\n", encoding="utf-8")

            with patch(
                "agentic_runner.execution_providers.MAX_PROVIDER_STAGE_FILES", 1,
            ):
                with self.assertRaisesRegex(RuntimeError, "synchronization.*file-count"):
                    _sync_provider_worktree(root, projection, {})
            self.assertFalse((root / "first.py").exists())
            self.assertFalse((root / "second.py").exists())

            with patch(
                "agentic_runner.execution_providers.MAX_PROVIDER_STAGE_BYTES", 6,
            ):
                with self.assertRaisesRegex(RuntimeError, "synchronization.*byte"):
                    _sync_provider_worktree(root, projection, {})
            self.assertFalse((root / "first.py").exists())
            self.assertFalse((root / "second.py").exists())

    def test_provider_sync_preserves_mode_only_changes(self):
        if os.name == "nt":
            self.skipTest("permission mode changes are not portable on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            projection = Path(temp) / "projection"
            root.mkdir()
            (root / "script.py").write_text("print(1)\n", encoding="utf-8")
            (root / "script.py").chmod(0o644)
            baseline = _stage_worktree(root, projection, skip=lambda _: False, read_only=False)

            (projection / "script.py").chmod(0o755)
            _sync_provider_worktree(root, projection, baseline)

            self.assertEqual((root / "script.py").stat().st_mode & 0o777, 0o755)

    def test_provider_write_rejects_same_inode_source_content_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            target = root / "source.py"
            target.write_text("original\n", encoding="utf-8")
            expected, _ = _read_projection_file(root, "source.py")
            target.write_text("concurrent\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed"):
                _write_projection_file(
                    root, "source.py", b"provider change\n", expected.mode, expected,
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "concurrent\n")

    def test_provider_write_revalidates_digest_immediately_before_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            target = root / "source.py"
            target.write_text("original\n", encoding="utf-8")
            expected, _ = _read_projection_file(root, "source.py")

            with patch(
                "agentic_runner.execution_providers._descriptor_digest",
                side_effect=[expected.digest, "concurrent-digest"],
            ):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    _write_projection_file(
                        root, "source.py", b"provider change\n", expected.mode, expected,
                    )
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")

    def test_atomic_replace_closes_existing_target_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            target.write_text("original\n", encoding="utf-8")
            observed_descriptor = []
            original_revalidate = file_boundaries.revalidate_single_link_regular
            original_replace = file_boundaries.os.replace

            def capture_revalidation(descriptor, expected_identity, *, description):
                observed_descriptor.append(descriptor)
                return original_revalidate(
                    descriptor, expected_identity, description=description,
                )

            def assert_closed_before_replace(*args, **kwargs):
                self.assertEqual(len(observed_descriptor), 1)
                with self.assertRaises(OSError):
                    os.fstat(observed_descriptor[0])
                return original_replace(*args, **kwargs)

            with patch.object(
                file_boundaries,
                "revalidate_single_link_regular",
                side_effect=capture_revalidation,
            ), patch.object(
                file_boundaries.os,
                "replace",
                side_effect=assert_closed_before_replace,
            ):
                file_boundaries.atomic_replace_relative_regular(
                    root, "target.txt", b"replacement\n", allow_existing=True,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "replacement\n")

    def test_atomic_replace_create_only_does_not_clobber_concurrent_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            original_link = file_boundaries.os.link

            def create_target_then_link(*args, **kwargs):
                target.write_text("concurrent\n", encoding="utf-8")
                return original_link(*args, **kwargs)

            with patch.object(
                file_boundaries.os, "link", side_effect=create_target_then_link,
            ), self.assertRaises(FileExistsError):
                file_boundaries.atomic_replace_relative_regular(
                    root, "target.txt", b"replacement\n",
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "concurrent\n")
            self.assertEqual(list(root.glob(".target.txt.agentic-*.tmp")), [])

    def test_atomic_replace_fails_closed_when_target_swaps_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.txt"
            unrelated = root / "unrelated.txt"
            target.write_text("original\n", encoding="utf-8")
            swapped = False
            original_stat = file_boundaries.os.stat

            def swap_target(path, *args, **kwargs):
                nonlocal swapped
                if not swapped and path == "target.txt" and kwargs.get("dir_fd") is not None:
                    target.unlink()
                    unrelated.write_text("unrelated\n", encoding="utf-8")
                    unrelated.replace(target)
                    swapped = True
                return original_stat(path, *args, **kwargs)

            with patch.object(file_boundaries.os, "stat", side_effect=swap_target):
                with self.assertRaisesRegex(ValueError, "changed while it was being replaced"):
                    file_boundaries.atomic_replace_relative_regular(
                        root, "target.txt", b"replacement\n", allow_existing=True,
                    )

            self.assertEqual(target.read_text(encoding="utf-8"), "unrelated\n")
            self.assertEqual(list(root.glob(".target.txt.agentic-*.tmp")), [])

    def test_provider_write_holds_mutation_lock_until_sync_is_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            target = root / "source.py"
            target.write_text("original\n", encoding="utf-8")
            expected, _ = _read_projection_file(root, "source.py")
            observed = []

            @contextmanager
            def lock(_descriptor):
                observed.append("acquire")
                yield
                observed.append("release")

            with patch(
                "agentic_runner.execution_providers.exclusive_regular_file_lock",
                side_effect=lock,
            ):
                _write_projection_file(
                    root, "source.py", b"provider change\n", expected.mode, expected,
                )

            self.assertEqual(observed, ["acquire", "release"])
            self.assertEqual(target.read_text(encoding="utf-8"), "provider change\n")

    def test_provider_write_opens_new_files_read_write_for_cross_platform_locking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            with patch(
                "agentic_runner.execution_providers.open_relative_regular",
                wraps=file_boundaries.open_relative_regular,
            ) as open_relative:
                _write_projection_file(
                    root, "new.py", b"provider change\n", 0o644, None,
                )

            flags = open_relative.call_args.args[2]
            self.assertEqual(flags & os.O_RDWR, os.O_RDWR)
            self.assertEqual(
                (root / "new.py").read_text(encoding="utf-8"),
                "provider change\n",
            )

    def test_provider_delete_revalidates_digest_before_unlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            target = root / "source.py"
            target.write_text("original\n", encoding="utf-8")
            expected, _ = _read_projection_file(root, "source.py")
            target.write_text("concurrent\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "changed"):
                file_boundaries.unlink_relative_regular(
                    root,
                    "source.py",
                    expected.identity,
                    expected_digest=expected.digest,
                    expected_size=expected.size,
                    description="provider worktree file",
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "concurrent\n")

    def test_provider_delete_fails_closed_when_target_swaps_before_unlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "worktree"
            root.mkdir()
            target = root / "source.py"
            target.write_text("original\n", encoding="utf-8")
            expected, _ = _read_projection_file(root, "source.py")
            swapped = False
            original_stat = file_boundaries.os.stat

            def swap_target(path, *args, **kwargs):
                nonlocal swapped
                if not swapped and path == "source.py" and kwargs.get("dir_fd") is not None:
                    target.unlink()
                    target.write_text("unrelated\n", encoding="utf-8")
                    swapped = True
                return original_stat(path, *args, **kwargs)

            with patch.object(file_boundaries.os, "stat", side_effect=swap_target):
                with self.assertRaisesRegex(ValueError, "changed while it was being removed"):
                    file_boundaries.unlink_relative_regular(
                        root,
                        "source.py",
                        expected.identity,
                        expected_digest=expected.digest,
                        expected_size=expected.size,
                        description="provider worktree file",
                    )

            self.assertEqual(target.read_text(encoding="utf-8"), "unrelated\n")

    def test_streamed_command_opens_logs_before_starting_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "not-a-file.log"
            log.mkdir()
            with patch.object(execution_stream.subprocess, "Popen") as popen:
                with self.assertRaises(RuntimeError):
                    streamed_command(
                        [sys.executable, "-c", "print('unexpected')"], root,
                        label="TESTS", log_file=log, timeout=10,
                    )
            popen.assert_not_called()

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
        for argv in (
            ["git", "status"], ["git", "diff", "--", "README.md"],
            ["git", "grep", "needle", "--", "README.md"], ["rg", "needle", "README.md"],
        ):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(argv, readonly=True)
            self.assertTrue(allowed, reason)

        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ["rg", "needle", "."], readonly=True,
        )
        self.assertFalse(allowed)
        self.assertIn("directory scopes", reason)

    def test_git_read_is_an_explicit_allowlist_for_developer_and_reviewer(self):
        allowed_git = ("status", "diff", "log", "show")
        blocked_git = ("add", "config", "merge", "rebase", "commit", "push")
        for readonly in (False, True):
            for subcommand in allowed_git:
                argv = ["git", subcommand]
                if subcommand == "diff":
                    argv.extend(["--", "README.md"])
                elif subcommand == "log":
                    argv.append(f"--format={GIT_LOG_METADATA_FORMAT}")
                elif subcommand == "show":
                    argv.append("HEAD:README.md")
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    argv, readonly=readonly,
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

    def test_openrouter_rejects_launcher_commands_before_check_allowlisting(self):
        policy = {
            "read_files": True, "search_files": True,
            "edit_files": True, "create_files": True,
            "git_read": True, "run_checks": True,
            "network": True, "allow_dangerous": False,
        }
        blocked = (
            ["env", "bash", "-c", "echo unsafe"],
            ["/usr/bin/env", "python", "-c", "print(unsafe)"],
            ["env", "curl", "https://example.test"],
            ["busybox", "sh", "-c", "echo unsafe"],
        )
        for argv in blocked:
            allowed, reason = agentic_openrouter._openrouter_command_allowed(
                argv, readonly=False, permission_policy=policy,
                check_commands=[argv],
            )
            self.assertFalse(allowed, argv)
            self.assertIn("launcher", reason)
        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ["python", "-m", "unittest"], readonly=False,
            permission_policy={**policy, "network": False},
            check_commands=[["python", "-m", "unittest"]],
        )
        self.assertTrue(allowed, reason)

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
        self.assertEqual(command[command.index("--tmpfs") + 1], "/tmp:rw,nosuid,exec,size=256m")
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("HOME=/tmp", command)
        self.assertIn("test-image", command)
        self.assertEqual(command[command.index("--entrypoint") + 1], "python")
        self.assertEqual(command[command.index("test-image") + 1:], ["-m", "unittest"])

    def test_sandbox_git_command_disables_repository_controlled_helpers(self):
        settings = validate_sandbox({"enabled": True, "engine": "docker", "image": "test-image"})
        command = sandbox_command(
            ["git", "status"], Path("/tmp/worktree"), settings,
            pass_env=["GIT_CONFIG_GLOBAL", "GIT_EXTERNAL_DIFF"],
            env={"GIT_CONFIG_GLOBAL": "/tmp/unsafe", "GIT_EXTERNAL_DIFF": "/tmp/helper"},
        )
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", command)
        self.assertIn(f"GIT_CONFIG_GLOBAL={os.devnull}", command)
        self.assertIn("GIT_ATTR_NOSYSTEM=1", command)
        self.assertEqual(
            sum(part.startswith("GIT_CONFIG_GLOBAL=") for part in command), 1,
        )
        self.assertNotIn("GIT_EXTERNAL_DIFF", command)

    def test_sandbox_git_metadata_probe_uses_hardened_git_execution(self):
        root = Path("/tmp/worktree")
        with patch.object(
            agentic_sandbox.subprocess, "run",
            return_value=CompletedProcess([], 0, stdout=".git\n"),
        ) as run:
            self.assertEqual(
                agentic_sandbox._authoritative_git_common_dir(root),
                root.resolve() / ".git",
            )
        argv = run.call_args.args[0]
        self.assertIn("core.fsmonitor=false", argv)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_sandbox_preserves_relative_argv_values_for_current_worktree(self):
        settings = validate_sandbox({"enabled": True, "engine": "docker", "image": "test-image"})
        code = "import os; print(os.getuid())"
        command = sandbox_command(
            ["python", "-c", code, "."], Path.cwd(), settings, restricted=True,
        )
        image_index = command.index("test-image")
        self.assertEqual(command[command.index("--entrypoint") + 1], "python")
        self.assertEqual(command[image_index + 1:], ["-c", code, "."])

    def test_sandbox_can_leave_git_metadata_environment_unset_for_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            (worktree / ".git").write_text("gitdir: /host-only/git\n", encoding="utf-8")
            settings = validate_sandbox({"enabled": True, "engine": "docker", "image": "test-image"})
            command = sandbox_command(
                ["python", "-m", "unittest"], worktree, settings,
                preserve_git_metadata=False,
            )
        self.assertNotIn("GIT_DIR=/agentic-git", command)
        self.assertNotIn("GIT_WORK_TREE=/workspace", command)
        self.assertIn(f"{os.devnull}:/workspace/.git:ro", command)

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
            with patch.object(agentic_sandbox, '_authoritative_git_common_dir', return_value=common.resolve()):
                command = sandbox_command(["git", "status"], worktree, settings)
        self.assertIn(f"{common.resolve()}:/agentic-git-common:ro", command)
        self.assertIn(f"{git_dir.resolve()}:/agentic-git-common/worktrees/agentic:ro", command)
        self.assertNotIn(f"{git_dir.resolve()}:/agentic-git-common/worktrees/agentic:rw", command)
        self.assertIn("GIT_DIR=/agentic-git-common/worktrees/agentic", command)
        self.assertNotIn("GIT_COMMON_DIR=/agentic-git-common", command)
        self.assertIn("GIT_WORK_TREE=/workspace", command)

    def test_sandbox_rejects_structurally_valid_pointer_to_unrelated_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            expected_common = parent / "repo" / ".git"
            unrelated_common = parent / "unrelated" / ".git"
            git_dir = unrelated_common / "worktrees" / "task"
            worktree.mkdir()
            git_dir.mkdir(parents=True)
            expected_common.mkdir(parents=True)
            (git_dir / "commondir").write_text("../../../../unrelated/.git\n", encoding="utf-8")
            (git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            settings = validate_sandbox({
                "enabled": True, "engine": "docker", "image": "test-image",
            })
            with patch.object(
                agentic_sandbox, '_authoritative_git_common_dir',
                return_value=expected_common.resolve(),
            ):
                command = sandbox_command(["git", "status"], worktree, settings)
        self.assertNotIn("/agentic-git", " ".join(command))

    def test_sandbox_rejects_symlinked_git_directory_without_host_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            host_target = parent / "host-target"
            worktree.mkdir()
            host_target.mkdir()
            try:
                (worktree / ".git").symlink_to(host_target, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            settings = validate_sandbox({
                "enabled": True, "engine": "docker", "image": "test-image",
            })
            command = sandbox_command(["git", "status"], worktree, settings)
        self.assertNotIn(f"{host_target.resolve()}:/agentic-git:ro", command)
        self.assertNotIn("/agentic-git", " ".join(command))
        self.assertNotIn("/workspace/.git:ro", " ".join(command))

    def test_sandbox_rejects_mocked_reparse_git_directory_without_host_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            (worktree / ".git").mkdir()
            settings = validate_sandbox({
                "enabled": True, "engine": "docker", "image": "test-image",
            })
            with patch.object(
                agentic_sandbox,
                "_is_reparse_or_symbolic_link",
                side_effect=lambda path: path.name == ".git",
            ):
                command = sandbox_command(["git", "status"], worktree, settings)
        self.assertNotIn("/agentic-git", " ".join(command))
        self.assertNotIn("/workspace/.git:ro", " ".join(command))

    def test_sandbox_overlays_normal_checkout_git_directory_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            (worktree / '.git').mkdir()
            settings = validate_sandbox({
                'enabled': True, 'engine': 'docker', 'image': 'test-image',
            })
            command = sandbox_command(['git', 'status'], worktree, settings)
        self.assertIn(f'{(worktree / ".git").resolve()}:/workspace/.git:ro', command)

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
            with patch.object(agentic_sandbox, '_authoritative_git_common_dir', return_value=common.resolve()):
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
            worktree = root / "worktree"
            worktree.mkdir()
            run = agentic.Run(None, "agent/task-demo-abcdef123456", worktree, "abcdef123456")
            state = root / "state"
            with patch.object(agentic_worktrees, "is_registered_worktree", return_value=True):
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

    def test_sandbox_output_mount_is_separate_from_durable_state(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            worktree = parent / "worktree"
            durable = parent / "runs" / "run-001"
            invocation = parent / "invocation-001"
            worktree.mkdir()
            durable.mkdir(parents=True)
            invocation.mkdir()
            settings = validate_sandbox({"enabled": True, "engine": "docker", "image": "test-image"})
            command = sandbox_command(
                ["codex", "--output-last-message", str(invocation / "result.txt")],
                worktree, settings, output_root=invocation, read_only=True,
            )
            mounts = [value for index, value in enumerate(command) if command[index - 1] == "-v"]
            self.assertIn(f"{invocation.resolve()}:/agentic-output:rw", mounts)
            self.assertNotIn(f"{durable}:/agentic-output:rw", mounts)
            self.assertNotIn(f"{durable}:/workspace:rw", mounts)


if __name__ == "__main__":
    unittest.main()
