"""Focused tests for assisted test-command discovery."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner.repository_metadata import MAX_REPOSITORY_METADATA_BYTES
from agentic_runner.test_discovery import (
    MAX_OUTPUT_TAIL_BYTES,
    MAX_DISCOVERY_TIMEOUT_SECONDS,
    DiscoveryResult,
    _kill_process_group,
    _python_module_available,
    _run_candidate,
    _safe_agent_argv,
    discover_and_validate,
    discover_test_candidates,
    validate_test_candidates,
)


class TestDiscovery(unittest.TestCase):
    def test_windows_timeout_cleanup_terminates_the_process_tree(self):
        process = type('Process', (), {'pid': 1234, 'kill': Mock()})()
        with patch('agentic_runner.test_discovery.os.name', 'nt'), \
                patch('agentic_runner.test_discovery.subprocess.run') as run:
            _kill_process_group(process)

        run.assert_called_once_with(
            ['taskkill', '/F', '/T', '/PID', '1234'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        process.kill.assert_called_once_with()

    def test_discovery_options_are_exposed_on_init(self):
        args = agentic.build_parser().parse_args([
            "init", "--discover-tests", "--discovery-timeout", "7",
        ])
        self.assertTrue(args.discover_tests)
        self.assertEqual(args.discovery_timeout, 7)

    def test_metadata_discovery_returns_argv_without_script_bodies(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "package.json").write_text(json.dumps({
                "scripts": {
                    "test": "node ./tests.js && rm -rf output",
                    "build": "webpack",
                },
            }))
            (repo / "package-lock.json").write_text("{}")
            candidates = discover_test_candidates(repo)

        commands = [item["argv"] for item in candidates]
        self.assertIn(["npm", "run", "test"], commands)
        self.assertIn(["npm", "run", "build"], commands)
        self.assertNotIn("node ./tests.js && rm -rf output", commands)
        self.assertTrue(all(isinstance(part, str) for argv in commands for part in argv))

    def test_metadata_discovery_skips_oversized_package_and_makefile(self):
        with tempfile.TemporaryDirectory() as temp:
            package_repo = Path(temp) / 'package'
            package_repo.mkdir()
            (package_repo / 'package.json').write_text(
                json.dumps({'scripts': {'test': 'true'}}) +
                ' ' * MAX_REPOSITORY_METADATA_BYTES,
            )
            package_commands = [
                item['argv'] for item in discover_test_candidates(package_repo)
            ]
            self.assertNotIn(['npm', 'run', 'test'], package_commands)

            make_repo = Path(temp) / 'make'
            make_repo.mkdir()
            (make_repo / 'Makefile').write_text(
                'check:\n\t@true\n' + ' ' * MAX_REPOSITORY_METADATA_BYTES,
            )
            make_commands = [item['argv'] for item in discover_test_candidates(make_repo)]
            self.assertNotIn(['make', 'check'], make_commands)

    def test_wrapper_discovery_and_validation_reject_symlinked_wrappers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            maven_repo = root / "maven"
            maven_repo.mkdir()
            external_maven = root / "external-mvnw"
            external_maven.write_text("#!/bin/sh\nexit 0\n")
            external_maven.chmod(0o700)
            (maven_repo / "mvnw").symlink_to(external_maven)

            maven_commands = [item["argv"] for item in discover_test_candidates(maven_repo)]
            self.assertNotIn(["./mvnw", "test"], maven_commands)
            maven_result = validate_test_candidates(
                maven_repo,
                [{"argv": ["./mvnw", "test"], "reason": "wrapper safety"}],
                timeout=2,
            )
            self.assertEqual(maven_result[0]["status"], "missing-dependency")

            gradle_repo = root / "gradle"
            gradle_repo.mkdir()
            (gradle_repo / "build.gradle").write_text("")
            external_gradle = root / "external-gradlew"
            external_gradle.write_text("#!/bin/sh\nexit 0\n")
            external_gradle.chmod(0o700)
            (gradle_repo / "gradlew").symlink_to(external_gradle)

            gradle_commands = [item["argv"] for item in discover_test_candidates(gradle_repo)]
            self.assertNotIn(["./gradlew", "test"], gradle_commands)
            self.assertIn(["gradle", "test"], gradle_commands)

    def test_validation_distinguishes_missing_tools_and_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            candidates = [
                {"argv": ["agentic-tool-that-is-not-installed"], "reason": "missing"},
                {"argv": [sys.executable, "-c", "import sys; sys.exit(3)"], "reason": "failure"},
                {"argv": [sys.executable, "-c", "import sys; sys.exit(127)"], "reason": "missing from PATH"},
            ]
            results = validate_test_candidates(repo, candidates, timeout=2)

        self.assertEqual(results[0]["status"], "missing-dependency")
        self.assertEqual(results[1]["status"], "failed")
        self.assertEqual(results[2]["status"], "missing-dependency")

    def test_validation_rejects_unbounded_timeout_and_bounds_module_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with self.assertRaisesRegex(ValueError, "from 1 to 86400"):
                validate_test_candidates(repo, [], timeout=0)
            with patch("agentic_runner.test_discovery.subprocess.run") as probe:
                probe.return_value.returncode = 1
                result = validate_test_candidates(
                    repo,
                    [{"argv": [sys.executable, "-m", "pytest"], "reason": "module"}],
                    timeout=1,
                )

        self.assertEqual(result[0]["status"], "missing-dependency")
        self.assertGreater(probe.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(probe.call_args.kwargs["timeout"], 1)
        self.assertEqual(MAX_DISCOVERY_TIMEOUT_SECONDS, 86_400)

    def test_unsandboxed_module_probe_receives_restricted_environment(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "semantic-secret-123456", "SERVICE_VALUE": "custom-secret-123456"},
            clear=True,
        ):
            repo = Path(temp)
            with patch("agentic_runner.test_discovery.subprocess.run") as probe:
                probe.return_value.returncode = 1
                validate_test_candidates(
                    repo,
                    [{"argv": [sys.executable, "-m", "pytest"], "reason": "module"}],
                    timeout=1,
                    credential_names=["SERVICE_VALUE"],
                )

        environment = probe.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("SERVICE_VALUE", environment)

    def test_unsandboxed_candidate_does_not_inherit_credentials(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "semantic-secret-123456", "SERVICE_VALUE": "custom-secret-123456"},
            clear=True,
        ):
            repo = Path(temp)
            script = (
                "import os,sys; "
                "sys.exit(0 if all(name not in os.environ for name in "
                "('OPENAI_API_KEY', 'SERVICE_VALUE')) else 1)"
            )
            results = validate_test_candidates(
                repo,
                [{"argv": [sys.executable, "-c", script], "reason": "environment"}],
                timeout=2,
                credential_names=["SERVICE_VALUE"],
            )

        self.assertEqual(results[0]["status"], "passed")

    def test_windows_python_executable_runs_module_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with patch("agentic_runner.test_discovery.subprocess.run") as probe:
                probe.return_value.returncode = 1
                available, timed_out = _python_module_available(
                    ["python.exe", "-m", "pytest"], repo, 1,
                )

        self.assertFalse(available)
        self.assertFalse(timed_out)
        probe.assert_called_once()

    def test_versioned_python_executable_runs_module_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with patch("agentic_runner.test_discovery.subprocess.run") as probe:
                probe.return_value.returncode = 1
                available, timed_out = _python_module_available(
                    ["python3.14", "-m", "pytest"], repo, 1,
                )

        self.assertFalse(available)
        self.assertFalse(timed_out)
        probe.assert_called_once()

    def test_python_module_probe_and_command_share_one_candidate_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with (patch("agentic_runner.test_discovery._python_module_available", return_value=(True, False)) as probe,
                  patch("agentic_runner.test_discovery._run_candidate", return_value=(0, "", False)) as run,
                  patch("agentic_runner.test_discovery.time.monotonic", side_effect=[100.0, 100.2, 100.4, 100.5, 100.6])):
                results = validate_test_candidates(
                    repo,
                    [{"argv": [sys.executable, "-m", "pytest"], "reason": "module"}],
                    timeout=1,
                )

        self.assertEqual(results[0]["status"], "passed")
        self.assertAlmostEqual(probe.call_args.args[2], 0.6)
        self.assertAlmostEqual(run.call_args.args[2], 0.5)

    def test_validation_uses_one_deadline_for_all_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            candidates = [
                {"argv": ["check-one"], "reason": "first"},
                {"argv": ["check-two"], "reason": "second"},
            ]
            with (patch("agentic_runner.test_discovery._executable_available", return_value=True),
                  patch("agentic_runner.test_discovery._run_candidate", return_value=(0, "", False)) as run,
                  patch("agentic_runner.test_discovery.time.monotonic",
                        side_effect=[0.0, 0.1, 0.2, 0.9, 0.95, 1.1, 1.1, 1.1])):
                results = validate_test_candidates(repo, candidates, timeout=1)

        self.assertEqual([item["status"] for item in results], ["passed", "timeout"])
        run.assert_called_once()

    def test_module_probe_timeout_is_not_reported_as_missing_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with patch(
                "agentic_runner.test_discovery.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="python", timeout=1),
            ):
                results = validate_test_candidates(
                    repo,
                    [{"argv": [sys.executable, "-m", "pytest"], "reason": "module"}],
                    timeout=1,
                )

        self.assertEqual(results[0]["status"], "timeout")

    def test_validation_records_failed_argv_command(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            results = validate_test_candidates(
                repo,
                [{"argv": [sys.executable, "-c", "import sys; sys.exit(3)"], "reason": "failure"}],
                timeout=2,
            )
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("exit code 3", results[0]["detail"])

    def test_validation_runs_candidates_through_enabled_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            settings = {"enabled": True, "engine": "docker", "image": "test-image"}
            with patch(
                "agentic_runner.test_discovery.sandbox_command",
                return_value=[sys.executable, "-c", "pass"],
            ) as sandbox:
                results = validate_test_candidates(
                    repo, [{"argv": ["tool-only-in-image"], "reason": "sandboxed"}],
                    timeout=2, sandbox=settings,
                )

        self.assertEqual(results[0]["status"], "passed")
        sandbox.assert_called_once_with(
            ["tool-only-in-image"], repo, settings, preserve_git_metadata=False,
        )

    def test_agent_assistance_is_read_only_and_unsafe_candidates_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "Makefile").write_text("check:\n\t@true\nlint:\n\t@true\n")
            marker = repo / "executed"
            evil_executable = repo / "untrusted" / "python"
            evil_executable.parent.mkdir()
            evil_executable.write_text(
                f"#!/bin/sh\nprintf executed > {marker}\n",
            )
            evil_executable.chmod(0o700)
            calls = []

            def fake_agent(*args, **kwargs):
                calls.append(kwargs)
                return True, json.dumps({"commands": [
                    {"argv": ["sh", "-c", "rm -rf ."], "profile": "fast"},
                    {"argv": [str(evil_executable), "-c", "pass"], "profile": "fast"},
                    {"argv": ["git", "diff", "--no-ext-diff", "--no-textconv", "--check"], "profile": "fast"},
                ]})

            result = discover_and_validate(
                repo, agent_runner=fake_agent, agent_settings={"provider": "test"},
                output=repo / "agent.txt", timeout=2,
            )

        self.assertEqual(result.agent_status, "agent assistance used")
        self.assertTrue(calls)
        self.assertTrue(calls[0]["readonly"])
        self.assertTrue(all(item["argv"][0] != "sh" for item in result.results))
        self.assertFalse(marker.exists())
        self.assertTrue(all(item["argv"][0] != str(evil_executable) for item in result.results))

    def test_agent_subcommands_use_restrictive_per_tool_allowlists(self):
        blocked = (
            ["npm", "exec", "eslint"],
            ["npm", "install", "eslint"],
            ["npm", "run", "install"],
            ["go", "get", "example.invalid/module"],
            ["composer", "require", "vendor/package"],
            ["cargo", "fetch"],
        )
        for argv in blocked:
            with self.subTest(argv=argv):
                self.assertIsNone(_safe_agent_argv(argv))
        self.assertEqual(_safe_agent_argv(["npm", "run", "test"]), ["npm", "run", "test"])
        self.assertEqual(_safe_agent_argv(["go", "test", "./..."]), ["go", "test", "./..."])

    def test_agent_delegated_scripts_must_match_metadata_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "package.json").write_text(json.dumps({
                "scripts": {"test": "true", "quality": "true", "deploy": "./cleanup"},
            }))
            (repo / "package-lock.json").write_text("{}")

            def fake_agent(*args, **kwargs):
                return True, json.dumps({"commands": [
                    {"argv": ["npm", "run", "deploy"], "profile": "fast"},
                    {"argv": ["bundle", "exec", "./cleanup"], "profile": "fast"},
                    {"argv": ["npm", "run", "quality"], "profile": "fast"},
                    {"argv": ["npm", "run", "test"], "profile": "fast"},
                ]})

            result = discover_and_validate(repo, agent_runner=fake_agent, timeout=2)

        commands = [item["argv"] for item in result.results]
        self.assertNotIn(["npm", "run", "deploy"], commands)
        self.assertNotIn(["bundle", "exec", "./cleanup"], commands)
        self.assertIn(["npm", "run", "quality"], commands)
        self.assertIn(["npm", "run", "test"], commands)

    def test_agent_project_task_aliases_must_match_metadata_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "package.json").write_text(json.dumps({
                "scripts": {"test": "true"},
            }))
            (repo / "package-lock.json").write_text("{}")

            def fake_agent(*args, **kwargs):
                return True, json.dumps({"commands": [
                    {"argv": ["npm", "test"], "profile": "fast"},
                    {"argv": ["npm", "build"], "profile": "fast"},
                    {"argv": ["make", "check"], "profile": "fast"},
                    {"argv": ["npm", "run", "test"], "profile": "fast"},
                ]})

            result = discover_and_validate(repo, agent_runner=fake_agent, timeout=2)

        commands = [item["argv"] for item in result.results]
        self.assertNotIn(["npm", "test"], commands)
        self.assertNotIn(["npm", "build"], commands)
        self.assertNotIn(["make", "check"], commands)
        self.assertIn(["npm", "run", "test"], commands)

    def test_dependency_text_requires_specific_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            results = validate_test_candidates(repo, [
                {
                    "argv": [sys.executable, "-c", "print('expected value not found'); raise SystemExit(1)"],
                    "reason": "assertion failure",
                },
                {
                    "argv": [sys.executable, "-c", "print(\"ModuleNotFoundError: No module named 'missing_pkg'\"); raise SystemExit(1)"],
                    "reason": "missing module",
                },
            ], timeout=2)

        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[1]["status"], "missing-dependency")

    def test_dependency_text_handles_prefixed_shell_errors_without_pathological_backtracking(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            outputs = [
                "tool: command not found",
                "shell: nested: no such file or directory",
                (" 9:" * 10_000) + " not a dependency error",
            ]
            candidates = [
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        f"import sys; print({output!r}); raise SystemExit(1)",
                    ],
                    "reason": "dependency diagnostic",
                }
                for output in outputs
            ]
            results = validate_test_candidates(repo, candidates, timeout=2)

        self.assertEqual(results[0]["status"], "missing-dependency")
        self.assertEqual(results[1]["status"], "missing-dependency")
        self.assertEqual(results[2]["status"], "failed")

    def test_verbose_dependency_failure_keeps_diagnostic_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            diagnostic = "\nModuleNotFoundError: No module named 'missing_pkg'\n"
            code = (
                "import sys; "
                "sys.stdout.write('x' * 10000); "
                f"sys.stdout.write({diagnostic!r}); "
                "sys.stdout.flush(); raise SystemExit(1)"
            )
            results = validate_test_candidates(
                repo,
                [{"argv": [sys.executable, "-c", code], "reason": "verbose missing module"}],
                timeout=2,
            )

        self.assertEqual(results[0]["status"], "missing-dependency")

    def test_validation_timeout_kills_descendant_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            marker = repo / "descendant-survived"
            child_code = (
                "import pathlib,time; time.sleep(2); "
                f"pathlib.Path({str(marker)!r}).write_text('survived')"
            )
            parent_code = (
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1]]); time.sleep(10)"
            )
            results = validate_test_candidates(
                repo,
                [{"argv": [sys.executable, "-c", parent_code, child_code], "reason": "child timeout"}],
                timeout=1,
            )
            self.assertEqual(results[0]["status"], "timeout")
            time.sleep(2.5)
            self.assertFalse(marker.exists())

    def test_validation_caps_high_volume_output_without_buffering_it(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            returncode, output, timed_out = _run_candidate(
                [
                    sys.executable, "-c",
                    "import sys; sys.stdout.write('x' * 8_000_000); sys.stdout.flush(); sys.exit(3)",
                ],
                repo,
                timeout=5,
            )

        self.assertEqual(returncode, 3)
        self.assertFalse(timed_out)
        self.assertLessEqual(len(output.encode("utf-8")), MAX_OUTPUT_TAIL_BYTES)

    def test_validation_times_out_continuous_output_with_bounded_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            results = validate_test_candidates(
                repo,
                [{
                    "argv": [
                        sys.executable, "-c",
                        "import sys\nwhile True:\n sys.stdout.write('x' * 8192); sys.stdout.flush()",
                    ],
                    "reason": "continuous output",
                }],
                timeout=1,
            )

        self.assertEqual(results[0]["status"], "timeout")

    def test_init_discovery_requires_confirmation_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            discovery = DiscoveryResult(
                results=[{
                    "argv": ["git", "diff", "--no-ext-diff", "--no-textconv", "--check"], "display": "git diff --no-ext-diff --no-textconv --check",
                    "status": "passed", "profile": "fast", "detail": "passed in 0.001s",
                }],
                fast_commands=[["git", "diff", "--no-ext-diff", "--no-textconv", "--check"]], slow_commands=[],
            )
            with (patch("agentic.discover_and_validate", return_value=discovery) as discovery_mock,
                  patch("agentic.request_repository_trust", return_value="trusted") as trust_prompt,
                  patch("builtins.input", return_value="n")):
                self.assertEqual(agentic.init_repository(repo, discover_tests=True), 0)
            self.assertFalse((repo / "agent_config.json").exists())
            discovery_mock.assert_not_called()
            trust_prompt.assert_called_once()

    def test_init_discovery_writes_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            discovery = DiscoveryResult(
                results=[{
                    "argv": ["git", "diff", "--no-ext-diff", "--no-textconv", "--check"], "display": "git diff --no-ext-diff --no-textconv --check",
                    "status": "passed", "profile": "fast", "detail": "passed in 0.001s",
                }],
                fast_commands=[["git", "diff", "--no-ext-diff", "--no-textconv", "--check"]], slow_commands=[],
            )
            with (patch("agentic.discover_and_validate", return_value=discovery),
                  patch("agentic.request_repository_trust", return_value="trusted") as trust_prompt,
                  patch("builtins.input", return_value="y")):
                self.assertEqual(agentic.init_repository(repo, discover_tests=True), 0)
            config = json.loads((repo / "agent_config.json").read_text())
            self.assertEqual(
                config["test_commands"],
                [["git", "diff", "--no-ext-diff", "--no-textconv", "--check"]],
            )
            trust_prompt.assert_called_once()

    def test_protected_existing_config_skips_assisted_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            config_path = repo / "agent_config.json"
            config_path.write_text("preserve this file\n")
            with patch("agentic.discover_and_validate") as discovery:
                self.assertEqual(agentic.init_repository(repo, discover_tests=True), 0)
            discovery.assert_not_called()
            self.assertEqual(config_path.read_text(), "preserve this file\n")

    def test_isolated_discovery_preflights_before_provider_or_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            discovery = DiscoveryResult(
                results=[], fast_commands=[["git", "diff", "--no-ext-diff", "--no-textconv", "--check"]], slow_commands=[],
            )
            events = []

            def fake_preflight(*args, **kwargs):
                events.append("preflight")

            def fake_discovery(*args, **kwargs):
                events.append(("discovery", kwargs["sandbox"], kwargs["credential_names"]))
                return discovery

            with (patch("agentic.preflight_security", side_effect=fake_preflight),
                  patch("agentic.discover_and_validate", side_effect=fake_discovery),
                  patch("builtins.input", side_effect=["y", "n"])):
                self.assertEqual(agentic.init_repository(
                    repo, security_mode="isolated", sandbox_image="test-image",
                    discover_tests=True,
                ), 0)

        self.assertEqual(events[0], "preflight")
        self.assertEqual(events[1][0], "discovery")
        self.assertTrue(events[1][1]["enabled"])
        self.assertIn("OPENAI_API_KEY", events[1][2])


if __name__ == "__main__":
    unittest.main()
