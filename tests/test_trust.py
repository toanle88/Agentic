"""Repository host-execution consent tests."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import tests  # noqa: F401
from agentic_runner import cli
from agentic_runner import dashboard
from agentic_runner import trust


class RepositoryTrustTests(unittest.TestCase):
    @staticmethod
    def _git(repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    @classmethod
    def _repository(cls, parent: Path) -> Path:
        repo = parent / "repo"
        repo.mkdir()
        cls._git(repo.parent, "init", "--initial-branch=main", str(repo))
        return repo

    @staticmethod
    def _local_task_args(repo: Path, state: Path):
        config = repo / "agent_config.json"
        config.write_text(json.dumps({
            "task_source": "local",
            "test_commands": [["python", "-c", "raise SystemExit(0)"],],
            "security_mode": "trusted",
            "sandbox": {"enabled": False},
        }), encoding="utf-8")
        return cli.build_parser().parse_args([
            "--repo", str(repo), "--state-dir", str(state), "--task", "exercise trust",
        ])

    def test_choices_are_distinguishable_and_only_host_choice_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            state = Path(temp) / "state"

            self.assertEqual(
                trust.request_repository_trust(
                    repo, state, input_fn=lambda _prompt: "2", interactive=True,
                    identity="identity-a",
                ),
                "isolated",
            )
            self.assertFalse(trust.is_repository_trusted(repo, state, identity="identity-a"))

            self.assertEqual(
                trust.request_repository_trust(
                    repo, state, input_fn=lambda _prompt: "1", interactive=True,
                    identity="identity-a",
                ),
                "trusted",
            )
            self.assertTrue(trust.is_repository_trusted(repo, state, identity="identity-a"))

    def test_default_and_noninteractive_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            state = Path(temp) / "state"
            for answer in ("", "no", "3"):
                with self.subTest(answer=answer):
                    with self.assertRaisesRegex(RuntimeError, "not granted"):
                        trust.request_repository_trust(
                            repo, state, input_fn=lambda _prompt, value=answer: value,
                            interactive=True, identity="identity-a",
                        )
            with self.assertRaisesRegex(RuntimeError, "required"):
                trust.request_repository_trust(
                    repo, state, interactive=False, identity="identity-a",
                )

    def test_identity_change_requires_consent_and_revoke_clears_record(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            state = Path(temp) / "state"
            trust.record_repository_trust(repo, state, identity="identity-a")
            path = trust.trust_record_path(repo, state)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(record), {"version", "repository_identity"})
            self.assertFalse(trust.is_repository_trusted(repo, state, identity="identity-b"))

            trust.revoke_repository_trust(repo, state)
            self.assertFalse(trust.is_repository_trusted(repo, state, identity="identity-a"))
            self.assertNotIn(str(repo), path.read_text(encoding="utf-8"))

    def test_cli_cancel_default_and_invalid_choices_start_no_execution(self):
        for answer in ("", "3", "invalid"):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = self._repository(root)
                state = root / "state"
                args = self._local_task_args(repo, state)
                with patch("builtins.input", return_value=answer), \
                     patch.object(trust, "_interactive_terminal", return_value=True), \
                     patch.object(cli, "preflight_security") as security, \
                     patch.object(cli, "preflight_test_commands") as tests_preflight, \
                     patch.object(cli, "preflight_agent_settings") as provider_preflight:
                    with self.assertRaisesRegex(RuntimeError, "not granted"):
                        cli.execute(args)
                security.assert_not_called()
                tests_preflight.assert_not_called()
                provider_preflight.assert_not_called()

    def test_cli_noninteractive_trusted_execution_fails_before_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repository(root)
            state = root / "state"
            args = self._local_task_args(repo, state)
            with patch.object(cli, "preflight_security") as security, \
                 patch.object(cli, "preflight_test_commands") as tests_preflight, \
                 patch.object(cli, "preflight_agent_settings") as provider_preflight:
                with patch.object(trust, "_interactive_terminal", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "trust is required"):
                        cli.execute(args)
            security.assert_not_called()
            tests_preflight.assert_not_called()
            provider_preflight.assert_not_called()

    def test_legacy_config_without_security_mode_still_requires_consent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repository(root)
            state = root / "state"
            (repo / "agent_config.json").write_text(json.dumps({
                "test_commands": [["python", "--version"]],
            }), encoding="utf-8")
            config = cli.load_config(repo / "agent_config.json")
            self.assertEqual(config["security_mode"], "trusted")
            with patch.object(trust, "_interactive_terminal", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "trust is required"):
                    cli._resolve_execution_security(repo, config, state)

    def test_host_consent_persists_across_cli_security_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repository(root)
            state = root / "state"
            with patch("builtins.input", return_value="1") as prompt, \
                 patch.object(trust, "_interactive_terminal", return_value=True):
                self.assertEqual(
                    cli._resolve_execution_security(
                        repo, {"security_mode": "trusted"}, state,
                    ),
                    "trusted",
                )
            prompt.assert_called_once()
            with patch("builtins.input") as second_prompt:
                self.assertEqual(
                    cli._resolve_execution_security(
                        repo, {"security_mode": "trusted"}, state,
                    ),
                    "trusted",
                )
            second_prompt.assert_not_called()

    def test_isolated_choice_reaches_isolated_preflight_without_host_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repository(root)
            state = root / "state"
            args = self._local_task_args(repo, state)
            config = json.loads((repo / "agent_config.json").read_text(encoding="utf-8"))
            config["sandbox"] = {"enabled": True, "engine": "docker", "image": "test-image"}
            (repo / "agent_config.json").write_text(json.dumps(config), encoding="utf-8")
            with patch("builtins.input", return_value="2"), \
                 patch.object(trust, "_interactive_terminal", return_value=True), \
                 patch.object(cli, "preflight_security", side_effect=RuntimeError("isolated preflight")) as security, \
                 patch.object(cli, "preflight_test_commands") as tests_preflight, \
                 patch.object(cli, "preflight_agent_settings") as provider_preflight:
                with self.assertRaisesRegex(RuntimeError, "isolated preflight"):
                    cli.execute(args)
            self.assertEqual(security.call_args.args[0], "isolated")
            tests_preflight.assert_not_called()
            provider_preflight.assert_not_called()
            self.assertFalse(trust.is_repository_trusted(repo, state))

    def test_actual_repository_identity_changes_invalidate_consent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repository(root)
            state = root / "state"
            trust.record_repository_trust(repo, state)
            original = trust.repository_identity(repo)

            self._git(repo, "remote", "add", "origin", "https://example.invalid/one.git")
            self.assertNotEqual(original, trust.repository_identity(repo))
            self.assertFalse(trust.is_repository_trusted(repo, state))

            remote_identity = trust.repository_identity(repo)
            git_directory = repo / ".git"
            external_git_directory = root / "external-git-common"
            git_directory.rename(external_git_directory)
            git_directory.write_text(f"gitdir: {external_git_directory}\n", encoding="utf-8")
            self.assertNotEqual(remote_identity, trust.repository_identity(repo))
            self.assertFalse(trust.is_repository_trusted(repo, state))

            moved = root / "moved-repo"
            repo.rename(moved)
            self.assertNotEqual(original, trust.repository_identity(moved))
            self.assertFalse(trust.is_repository_trusted(moved, state))

    def test_trust_revoke_cli_path_removes_matching_consent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self._repository(root)
            state = root / "state"
            trust.record_repository_trust(repo, state)
            with patch.object(cli, "_status_print"):
                self.assertEqual(cli.main([
                    "trust", "--repo", str(repo), "--state-dir", str(state), "--revoke",
                ]), 0)
            self.assertFalse(trust.is_repository_trusted(repo, state))

    def test_dashboard_generation_does_not_require_repository_consent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / "logs"
            logs.mkdir()
            output = root / "dashboard.html"
            with patch("agentic_runner.trust.request_repository_trust") as prompt:
                self.assertEqual(dashboard.main([
                    "--repo", str(root), "--logs", str(logs), "--output", str(output),
                ]), 0)
            prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
