"""Focused tests for configuration, setup, and diagnostics."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic
import agentic_config
import agentic_diagnostics

from test_agentic import sh


class ConfigurationTests(unittest.TestCase):
    def test_init_detects_go_and_ignores_personal_config(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            (repo / 'go.mod').write_text('module example.test/demo\n')
            self.assertEqual(agentic.init_repository(repo, create_agents=True), 0)
            config = agentic.load_config(repo / 'agent_config.json')
            self.assertEqual(config['test_commands'], [['go', 'test', './...']])
            self.assertEqual(config['security_mode'], 'trusted')
            self.assertFalse(config['sandbox']['enabled'])
            self.assertIn('agent_config.json', (repo / '.gitignore').read_text())
            self.assertTrue((repo / 'AGENTS.md').is_file())


    def test_init_can_generate_provider_specific_config(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            (repo / 'go.mod').write_text('module example.test/demo\n')
            self.assertEqual(
                agentic.init_repository(repo, provider='openrouter', reviewer_provider='claude'),
                0,
            )
            config = agentic.load_config(repo / 'agent_config.json')
            self.assertEqual(config['developer_agent']['provider'], 'openrouter')
            self.assertEqual(config['developer_agent']['model'], 'openai/gpt-5.4')
            self.assertEqual(config['developer_agent']['api_key_env'], 'OPENROUTER_API_KEY')
            self.assertEqual(config['reviewer_agent']['provider'], 'claude')


    def test_provider_init_options_are_exposed(self):
        args = agentic.build_parser().parse_args([
            'init', '--provider', 'gemini', '--security-mode', 'isolated'
        ])
        self.assertEqual(args.provider, 'gemini')
        self.assertIsNone(args.reviewer_provider)
        self.assertEqual(args.security_mode, 'isolated')
        self.assertFalse(args.secure)
        self.assertIsNone(args.sandbox_image)
        self.assertIsNone(args.sandbox_dockerfile)
        self.assertEqual(args.sandbox_context, '.')
        self.assertIsNone(args.sandbox_language)

        defaults = agentic.build_parser().parse_args(['init'])
        self.assertEqual(defaults.security_mode, 'trusted')
        self.assertFalse(defaults.secure)


    def test_secure_init_option_selects_isolated_mode(self):
        args = agentic.build_parser().parse_args(['init', '--secure'])
        self.assertTrue(args.secure)
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            with patch('agentic.init_repository', return_value=0) as init:
                self.assertEqual(
                    agentic.main([
                        'init', '--repo', str(repo), '--secure',
                        '--sandbox-image', 'test-image',
                    ]), 0
                )
            self.assertEqual(init.call_args.kwargs['security_mode'], 'isolated')
            self.assertEqual(init.call_args.kwargs['sandbox_image'], 'test-image')


    def test_init_can_select_isolated_security_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            (repo / 'go.mod').write_text('module example.test/demo\n')
            self.assertEqual(
                agentic.init_repository(
                    repo, security_mode='isolated', sandbox_image='test-image'
                ), 0
            )
            config = agentic.load_config(repo / 'agent_config.json')
        self.assertEqual(config['security_mode'], 'isolated')
        self.assertTrue(config['sandbox']['enabled'])
        self.assertEqual(config['sandbox']['image'], 'test-image')


    def test_init_can_build_a_language_specific_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            (repo / 'package.json').write_text('{"scripts": {"test": "npm test"}}\n')
            (repo / 'Dockerfile.agentic').write_text('FROM node:22.11.0-bookworm-slim\n')
            self.assertEqual(agentic.init_repository(
                repo, security_mode='isolated', sandbox_dockerfile='Dockerfile.agentic',
            ), 0)
            config = agentic.load_config(repo / 'agent_config.json')
        self.assertEqual(config['sandbox']['dockerfile'], 'Dockerfile.agentic')
        self.assertEqual(config['sandbox']['build_context'], '.')
        self.assertEqual(config['sandbox']['language'], 'node')
        self.assertEqual(config['sandbox']['image'], agentic.DEFAULT_SANDBOX_IMAGE)


    def test_init_trusted_mode_warns_about_host_account_permissions(self):
        from io import StringIO

        with tempfile.TemporaryDirectory() as temp, patch('sys.stdout', new_callable=StringIO) as output:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            self.assertEqual(agentic.init_repository(repo), 0)
        self.assertIn('trusted mode runs provider CLIs and checks with host account permissions', output.getvalue())


    def test_isolated_init_requires_a_sandbox_image(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            with self.assertRaisesRegex(ValueError, 'sandbox-image'):
                agentic.init_repository(repo, security_mode='isolated')


    def test_provider_config_supports_different_developer_and_reviewer(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'developer': {
                    'provider': 'gemini', 'model': 'gemini-3-pro-preview',
                    'timeout_seconds': None,
                },
                'reviewer': {
                    'provider': 'openrouter', 'model': 'openai/gpt-5.4',
                    'api_key_env': 'MY_OPENROUTER_KEY',
                },
            }))
            config = agentic.load_config(path)
        self.assertEqual(config['developer_agent']['provider'], 'gemini')
        self.assertEqual(config['developer_agent']['model'], 'gemini-3-pro-preview')
        self.assertEqual(config['reviewer_agent']['provider'], 'openrouter')
        self.assertEqual(config['reviewer_agent']['api_key_env'], 'MY_OPENROUTER_KEY')
        self.assertEqual(config['reviewer_agent']['base_url'], agentic.DEFAULT_OPENROUTER_BASE_URL)



    def test_provider_config_rejects_invalid_values_and_embedded_secrets(self):
        base = {'test_commands': [['go', 'test', './...']]}
        invalid = [
            ({'developer': {'provider': 'unknown'}}, 'provider'),
            ({'developer': []}, 'developer must be an object'),
            ({'developer': {'api_key': 'secret'}}, 'api_key'),
            ({'developer': {'timeout_seconds': 0}}, 'timeout_seconds'),
            ({'developer': {'model': ' '}}, 'model'),
            ({'developer': {'provider': 'openrouter', 'model': 'x', 'base_url': 'ftp://router'}}, 'base_url'),
            ({'developer': {'provider': 'gemini', 'api_key_env': 'KEY'}}, 'only valid for openrouter'),
            ({'security_mode': 'unsafe'}, 'security_mode'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            for extra, expected in invalid:
                with self.subTest(expected=expected):
                    path = Path(temp) / f'{len(list(Path(temp).glob("*.json")))}.json'
                    path.write_text(json.dumps({**base, **extra}))
                    with self.assertRaisesRegex(ValueError, expected):
                        agentic.load_config(path)


    def test_openrouter_limits_are_loaded_and_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'developer': {
                    'provider': 'openrouter', 'model': 'openai/test',
                    'max_tool_turns': 12, 'command_timeout_seconds': 7,
                    'allow_network': True, 'json_mode': True,
                },
            }))
            config = agentic.load_config(path)
        settings = config['developer_agent']
        self.assertEqual(settings['max_tool_turns'], 12)
        self.assertEqual(settings['command_timeout_seconds'], 7)
        self.assertTrue(settings['allow_network'])
        self.assertTrue(settings['json_mode'])


    def test_isolated_security_mode_requires_sandbox_and_supports_openrouter(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'missing.json'
            missing.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'security_mode': 'isolated',
            }))
            with self.assertRaisesRegex(ValueError, 'requires sandbox'):
                agentic.load_config(missing)

            isolated = Path(temp) / 'isolated.json'
            isolated.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'security_mode': 'isolated',
                'sandbox': {'enabled': True, 'engine': 'docker', 'image': 'test-image'},
                'developer': {'provider': 'openrouter', 'model': 'openai/test'},
            }))
            config = agentic.load_config(isolated)
        self.assertEqual(config['security_mode'], 'isolated')


    def test_preflight_security_fails_closed_when_sandbox_engine_is_missing(self):
        with patch('agentic.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                agentic.preflight_security(
                    'isolated', {'enabled': True, 'engine': 'docker'}
                )
        agentic.preflight_security('trusted', {'enabled': False})


    def test_legacy_codex_settings_are_applied_to_both_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'legacy.json'
            path.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'codex_model': 'gpt-5.6-luna',
                'codex_reasoning_effort': 'max',
                'codex_timeout_seconds': None,
            }))
            config = agentic.load_config(path)
        self.assertEqual(config['developer_agent']['model'], 'gpt-5.6-luna')
        self.assertEqual(config['reviewer_agent']['model'], 'gpt-5.6-luna')
        self.assertEqual(config['developer_agent']['reasoning_effort'], 'max')
        self.assertEqual(config['reviewer_agent']['reasoning_effort'], 'max')


    def test_preflight_agent_settings_checks_cli_and_openrouter_credentials(self):
        with patch.dict(agentic.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'OPENROUTER_API_KEY'):
                agentic.preflight_agent_settings({
                    'provider': 'openrouter', 'api_key_env': 'OPENROUTER_API_KEY'
                })
        with patch('agentic.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'gemini'):
                agentic.preflight_agent_settings({'provider': 'gemini'})
        with patch('agentic.shutil.which', return_value='/usr/bin/codex'):
            agentic.preflight_agent_settings(
                {'provider': 'codex'}, {'provider': 'codex'}
            )


    def test_isolated_preflight_uses_tools_from_the_sandbox_image(self):
        sandbox = {'enabled': True, 'engine': 'docker', 'image': 'test-image'}
        with patch('agentic.shutil.which', return_value=None):
            agentic.preflight_test_commands(Path('/tmp/repo'), [['missing-tool', '--check']], sandbox=sandbox)
            agentic.preflight_agent_settings({'provider': 'gemini'}, sandbox=sandbox)


    def test_sandbox_preflight_requires_configured_credentials_to_exist(self):
        sandbox = {
            'enabled': True, 'engine': 'docker', 'image': 'test-image',
            'pass_env': ['GEMINI_API_KEY'],
        }
        with patch.dict(agentic.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'GEMINI_API_KEY'):
                agentic.preflight_agent_settings({'provider': 'gemini'}, sandbox=sandbox)
        with patch.dict(agentic.os.environ, {'GEMINI_API_KEY': 'present'}, clear=True):
            agentic.preflight_agent_settings({'provider': 'gemini'}, sandbox=sandbox)


    def test_sandbox_credentials_are_selected_per_provider(self):
        sandbox = {
            'enabled': True, 'engine': 'docker', 'image': 'test-image',
            'pass_env': ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY'],
        }
        self.assertEqual(
            agentic.sandbox_env_for_provider('codex', sandbox), ['OPENAI_API_KEY']
        )
        self.assertEqual(
            agentic.sandbox_env_for_provider('claude', sandbox), ['ANTHROPIC_API_KEY']
        )
        self.assertEqual(agentic.sandbox_env_for_provider('openrouter', sandbox), [])


    def test_sandbox_image_and_environment_validation_is_strict(self):
        with self.assertRaisesRegex(ValueError, 'sandbox.image'):
            agentic.validate_sandbox({'enabled': True})
        with self.assertRaisesRegex(ValueError, 'pass_env'):
            agentic.validate_sandbox({
                'enabled': True, 'image': 'test-image', 'pass_env': ['bad-name'],
            })


    def test_sandbox_dockerfile_and_language_configuration_is_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['python', '-m', 'unittest']],
                'security_mode': 'isolated',
                'sandbox': {
                    'enabled': True, 'engine': 'docker', 'image': 'project:latest',
                    'dockerfile': 'Dockerfile.agentic', 'build_context': 'container',
                    'language': 'python',
                },
            }))
            config = agentic.load_config(path)
        self.assertEqual(config['sandbox']['dockerfile'], 'Dockerfile.agentic')
        self.assertEqual(config['sandbox']['build_context'], 'container')
        self.assertEqual(config['sandbox']['language'], 'python')

        base = {'test_commands': [['python', '-m', 'unittest']], 'sandbox': {
            'enabled': True, 'image': 'project:latest',
        }}
        with tempfile.TemporaryDirectory() as temp:
            for name, value in (('dockerfile', '../Dockerfile'), ('build_context', '/tmp')):
                path = Path(temp) / f'{name}.json'
                sandbox = {**base['sandbox'], name: value}
                path.write_text(json.dumps({**base, 'sandbox': sandbox}))
                with self.assertRaisesRegex(ValueError, f'sandbox.{name}'):
                    agentic.load_config(path)


    def test_preflight_builds_a_configured_sandbox_with_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            dockerfile = repo / 'Dockerfile.agentic'
            dockerfile.write_text('FROM python:3.12.7-slim-bookworm\n')
            sandbox = {
                'enabled': True, 'engine': 'docker', 'image': 'project:latest',
                'dockerfile': 'Dockerfile.agentic', 'build_context': '.',
            }
            completed = CompletedProcess([], 0, stdout='built\n')
            with (patch('agentic_config.shutil.which', return_value='/usr/bin/docker'),
                  patch('agentic_config.subprocess.run', return_value=completed) as run):
                agentic.preflight_security('isolated', sandbox, repo=repo)
        run.assert_called_once_with(
            ['docker', 'build', '--pull=false', '--tag', 'project:latest',
             '--file', str(dockerfile.resolve()), str(repo.resolve())],
            cwd=repo, stdout=agentic_config.subprocess.PIPE,
            stderr=agentic_config.subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', timeout=1800, check=False,
        )


    def test_project_language_detection_supports_explicit_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / 'global.json').write_text('{}')
            self.assertEqual(agentic.detect_project_language(repo)[0], 'dotnet')
            (repo / 'pom.xml').write_text('<project/>')
            self.assertEqual(agentic.detect_project_language(repo)[0], 'java')


    def test_doctor_reports_repository_config_and_provider_health(self):
        from io import StringIO

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'pass']],
            }))
            probed = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(repo)
                return True, 'ok'

            def fake_which(name):
                return '/usr/bin/' + Path(name).name

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', side_effect=fake_which),
                  patch('sys.stdout', new_callable=StringIO) as output):
                result = agentic.doctor(repo, config)
        self.assertEqual(result, 0)
        self.assertIn('Agentic doctor', output.getvalue())
        self.assertIn('Agentic config', output.getvalue())
        self.assertIn('provider (codex)', output.getvalue())
        self.assertIn(
            ['/usr/bin/gh', 'auth', 'status', '--active', '--hostname', 'github.com'],
            probed,
        )


    def test_doctor_json_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({'test_commands': [[sys.executable, '-c', 'pass']]}))

            def fake_probe(argv, cwd, **kwargs):
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(repo)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/codex'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload['ok'])
        self.assertTrue(any(item['name'] == 'Agentic config' for item in payload['checks']))


    def test_doctor_commands_runs_from_selected_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worktree = root / 'selected-worktree'
            worktree.mkdir()
            config = root / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[
                    sys.executable, '-c',
                    'import os, sys; '
                    'normal = lambda value: os.path.normcase(os.path.realpath(os.path.abspath(value))); '
                    'sys.exit(0 if normal(os.getcwd()) == normal(sys.argv[1]) else 1)',
                    str(worktree),
                ]],
            }))

            def fake_probe(argv, cwd, **kwargs):
                if argv[:3] == ['git', 'rev-parse', '--git-common-dir']:
                    return True, str(root / '.git')
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(cwd)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('agentic.worktree_path_lock', return_value=nullcontext()) as lock,
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(
                    root, config, json_output=True, run_commands=True,
                    worktree=worktree,
                )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['command_results'][0]['status'], 'passed')
        self.assertEqual(payload['command_results'][0]['exit_code'], 0)
        worktree_check = next(item for item in payload['checks'] if item['name'] == 'Command worktree')
        self.assertTrue(worktree_check['ok'])
        lock.assert_called_once()
        locked_repo, locked_worktree = lock.call_args.args
        def normalize_path(value):
            return os.path.normcase(
                os.path.realpath(os.path.abspath(os.fspath(value)))
            )
        self.assertEqual(normalize_path(locked_repo), normalize_path(root))
        self.assertEqual(normalize_path(locked_worktree), normalize_path(worktree))


    def test_doctor_commands_reports_command_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'import sys; sys.exit(3)']],
            }))

            def fake_probe(argv, cwd, **kwargs):
                if argv[:3] == ['git', 'rev-parse', '--git-common-dir']:
                    return True, str(root / '.git')
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(root)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('agentic.worktree_path_lock', return_value=nullcontext()),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(root, config, json_output=True, run_commands=True)

        self.assertEqual(result, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['command_results'][0]['status'], 'failed')
        self.assertEqual(payload['command_results'][0]['exit_code'], 3)
        execution = next(item for item in payload['checks'] if item['name'] == 'Configured command execution')
        self.assertFalse(execution['ok'])


    def test_doctor_commands_reject_unrelated_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            worktree = root / 'other-repository'
            worktree.mkdir()
            config = root / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'raise SystemExit(99)']],
            }))

            def fake_probe(argv, cwd, **kwargs):
                if argv[:3] == ['git', 'rev-parse', '--git-common-dir']:
                    same_root = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(cwd)))) == os.path.normcase(
                        os.path.realpath(os.path.abspath(os.fspath(root)))
                    )
                    common = root / ('.git' if same_root else 'other.git')
                    return True, str(common)
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(cwd)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(
                    root, config, json_output=True, run_commands=True,
                    worktree=worktree,
                )

        self.assertEqual(result, 1)
        payload = json.loads(output.getvalue())
        worktree_check = next(item for item in payload['checks'] if item['name'] == 'Command worktree')
        self.assertFalse(worktree_check['ok'])
        self.assertEqual(payload['command_results'], [])


    def test_doctor_sandboxed_commands_do_not_receive_credentials(self):
        sandbox = {
            'enabled': True,
            'engine': 'docker',
            'image': 'test-image',
            'pass_env': ['OPENAI_API_KEY'],
        }
        with (patch('agentic_diagnostics.sandbox_command', return_value=['tool']) as sandbox_command,
              patch('agentic_diagnostics.subprocess.run', return_value=CompletedProcess([], 0))):
            result = agentic_diagnostics._run_configured_command(
                ['tool'], Path('/tmp/worktree'), timeout=1, sandbox=sandbox,
            )
        self.assertEqual(result['status'], 'passed')
        sandbox_command.assert_called_once_with(
            ['tool'], Path('/tmp/worktree'), sandbox, pass_env=[],
        )


    def test_doctor_reports_network_policy_for_read_only_reviewers(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'pass']],
                'security_mode': 'isolated',
                'sandbox': {
                    'enabled': True,
                    'engine': 'docker',
                    'image': 'test-image',
                    'allow_network': True,
                },
            }))

            probed = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(repo)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.preflight_test_commands'),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        network = next(item for item in payload['checks'] if item['name'] == 'Sandbox network')
        self.assertEqual(network['status'], 'warn')
        self.assertIn('read-only reviewer', network['detail'])
        provider_probe = next(argv for argv in probed if '--entrypoint' in argv)
        self.assertEqual(provider_probe[provider_probe.index('--entrypoint') + 1], 'codex')
        self.assertEqual(provider_probe[-1], '--version')


    def test_all_provider_templates_are_valid_configs(self):
        templates = sorted((Path(__file__).resolve().parents[1] / 'examples' / 'provider-configs').glob(
            'agent_config.*.json'
        ))
        self.assertEqual(len(templates), len(agentic.SUPPORTED_PROVIDERS))
        for path in templates:
            with self.subTest(path=path.name):
                config = agentic.load_config(path)
                self.assertIn(config['developer_agent']['provider'], agentic.SUPPORTED_PROVIDERS)
                self.assertIn(config['reviewer_agent']['provider'], agentic.SUPPORTED_PROVIDERS)


    def test_detect_test_commands_for_common_project_types(self):
        cases = [
            ('Cargo.toml', None, [['cargo', 'test']]),
            ('package.json', 'pnpm-lock.yaml', [['pnpm', 'test']]),
            ('package.json', 'yarn.lock', [['yarn', 'test']]),
            ('package.json', None, [['npm', 'test']]),
            ('Makefile', None, [['make', 'check']]),
        ]
        with tempfile.TemporaryDirectory() as temp:
            for marker, extra, expected in cases:
                with self.subTest(marker=marker, extra=extra):
                    repo = Path(temp) / f'{marker}-{extra or "none"}'
                    repo.mkdir()
                    if marker == 'package.json':
                        (repo / marker).write_text('{"scripts":{"test":"echo test"}}')
                    elif marker == 'Makefile':
                        (repo / marker).write_text('check:\n\techo check\n')
                    else:
                        (repo / marker).write_text('placeholder\n')
                    if extra:
                        (repo / extra).write_text('')
                    commands, _ = agentic.detect_test_commands(repo)
                    self.assertEqual(commands, expected)

            python_repo = Path(temp) / 'python'
            python_repo.mkdir()
            (python_repo / 'pyproject.toml').write_text('[project]\nname="demo"\n')
            (python_repo / 'tests').mkdir()
            self.assertEqual(agentic.detect_test_commands(python_repo)[0][0][:3],
                             ['python', '-m', 'unittest'])

            fallback = Path(temp) / 'fallback'
            fallback.mkdir()
            self.assertEqual(agentic.detect_test_commands(fallback)[0], [['git', 'diff', '--check']])


    def test_config_validation(self):
        folder = Path(__file__).resolve().parents[1]
        config = agentic.load_config(folder / 'agent_config.example.json')
        self.assertEqual(config['max_retries'], 3)
        self.assertIsNone(config['max_files'])
        self.assertEqual(config['slow_test_commands'], [])
        self.assertIsNone(config['codex_model'])
        self.assertIsNone(config['codex_reasoning_effort'])
        self.assertIsNone(config['codex_timeout_seconds'])
        self.assertIsNone(config['test_timeout_seconds'])
        self.assertIsNone(config['effective_test_timeout_seconds'])
        self.assertEqual(config['task_source'], 'github')
        self.assertIsNone(config['system_prompt'])
        self.assertTrue(agentic.forbidden('.env'))
        self.assertFalse(agentic.forbidden('src/main.py'))

        with tempfile.TemporaryDirectory() as temp:
            unlimited = Path(temp) / 'agent_config.json'
            unlimited.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'max_files': None,
            }))
            unlimited_config = agentic.load_config(unlimited)
            self.assertIsNone(unlimited_config['max_files'])
            self.assertEqual(unlimited_config['effective_test_timeout_seconds'], 300)
            self.assertNotIn('system_prompt', unlimited_config)

            configured = Path(temp) / 'configured.json'
            configured.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'system_prompt': 'Prefer small focused changes.',
            }))
            self.assertEqual(
                agentic.load_config(configured)['system_prompt'],
                'Prefer small focused changes.',
            )

            for invalid_value in (42, [], '', '   '):
                invalid_prompt = Path(temp) / f'invalid-prompt-{type(invalid_value).__name__}.json'
                invalid_prompt.write_text(json.dumps({
                    'test_commands': [['go', 'test', './...']],
                    'system_prompt': invalid_value,
                }))
                with self.assertRaisesRegex(ValueError, 'system_prompt'):
                    agentic.load_config(invalid_prompt)

            no_test_timeout = Path(temp) / 'no-test-timeout.json'
            no_test_timeout.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'test_timeout_seconds': None,
            }))
            self.assertIsNone(agentic.load_config(no_test_timeout)['effective_test_timeout_seconds'])

            local = Path(temp) / 'local.json'
            local.write_text(json.dumps({
                'task_source': 'local',
                'test_commands': [['go', 'test', './...']],
            }))
            self.assertEqual(agentic.load_config(local)['task_source'], 'local')

            for retries in (0, 5):
                configured_retries = Path(temp) / f'retries-{retries}.json'
                configured_retries.write_text(json.dumps({
                    'test_commands': [['go', 'test', './...']],
                    'max_retries': retries,
                }))
                self.assertEqual(
                    agentic.load_config(configured_retries)['max_retries'], retries,
                )

            for index, retries in enumerate((-1, 6, 1.5, '5', True)):
                invalid_retries = Path(temp) / f'invalid-retries-{index}.json'
                invalid_retries.write_text(json.dumps({
                    'test_commands': [['go', 'test', './...']],
                    'max_retries': retries,
                }))
                with self.assertRaisesRegex(ValueError, 'max_retries'):
                    agentic.load_config(invalid_retries)

            invalid = Path(temp) / 'invalid.json'
            invalid.write_text(json.dumps({
                'task_source': 'jira',
                'test_commands': [['go', 'test', './...']],
            }))
            with self.assertRaisesRegex(ValueError, 'task_source'):
                agentic.load_config(invalid)


    def test_slow_profile_appends_opt_in_checks(self):
        config = {
            'test_commands': [['go', 'test', './...']],
            'slow_test_commands': [['make', 'persistence-check']],
        }
        self.assertEqual(
            agentic.test_commands_for_profile(config, 'slow'),
            [['go', 'test', './...'], ['make', 'persistence-check']],
        )
        with self.assertRaisesRegex(RuntimeError, 'slow test profile'):
            agentic.test_commands_for_profile({'test_commands': []}, 'slow')
