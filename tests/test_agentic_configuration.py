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
import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner.policy import sensitive_read
from agentic_runner import config as agentic_config
from agentic_runner import diagnostics as agentic_diagnostics

from test_agentic import sh


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise doctor command execution; model the new
        # explicit consent rather than allowing a host check implicitly.
        self._trust_prompt = patch(
            'agentic.request_repository_trust', return_value='trusted',
        )
        self._trust_prompt.start()
        self.addCleanup(self._trust_prompt.stop)

    def test_doctor_status_labels_use_shared_tty_styling_and_plain_fallback(self):
        class TTYBuffer(io.StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'import sys; sys.exit(3)']],
            }))

            def fake_probe(argv, cwd, **kwargs):
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(repo)
                return False, 'unavailable'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('agentic.worktree_path_lock', return_value=nullcontext()),
                  patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}),
                  patch('sys.stdout', new=TTYBuffer()) as output):
                result = agentic.doctor(repo, config, run_commands=True)

            colored = output.getvalue()
            self.assertEqual(result, 1)
            self.assertIn('\033[31m[FAIL]\033[0m', colored)
            self.assertIn('\033[33m[WARN]\033[0m', colored)
            self.assertIn('\033[31m[FAILED     ]\033[0m', colored)
            self.assertNotIn('\033[31m[DOCTOR]', colored)

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('agentic.worktree_path_lock', return_value=nullcontext()),
                  patch.dict('os.environ', {'NO_COLOR': '1'}),
                  patch('sys.stdout', new=io.StringIO()) as plain_output):
                agentic.doctor(repo, config, run_commands=True)
            self.assertNotIn('\033[', plain_output.getvalue())
            self.assertIn('[FAILED     ]', plain_output.getvalue())

    def test_init_detects_go_and_ignores_personal_config(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            gitignore = repo / '.gitignore'
            gitignore.write_text('# Existing shared rules\nagent_config.json\n', encoding='utf-8')
            gitignore_before = gitignore.read_bytes()
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            (repo / 'go.mod').write_text('module example.test/demo\n')
            exclude = Path(sh(
                'git', 'rev-parse', '--git-path', 'info/exclude', cwd=repo,
            ).stdout.strip())
            if not exclude.is_absolute():
                exclude = (repo / exclude).resolve()
            exclude.write_bytes(b'# Existing local rules\r\n')
            self.assertEqual(agentic.init_repository(repo, create_agents=True), 0)
            config = agentic.load_config(repo / 'agent_config.json')
            self.assertEqual(config['test_commands'], [['go', 'test', './...']])
            self.assertEqual(config['security_mode'], 'trusted')
            self.assertFalse(config['sandbox']['enabled'])

            self.assertEqual(gitignore.read_bytes(), gitignore_before)
            self.assertEqual(
                exclude.read_bytes(),
                b'# Existing local rules\r\n'
                b'# Agentic local configuration\r\n'
                b'agent_config.json\r\n',
            )
            self.assertTrue((repo / 'AGENTS.md').is_file())
            self.assertEqual(agentic.init_repository(repo), 0)
            self.assertEqual(
                exclude.read_text(encoding='utf-8').splitlines().count('agent_config.json'),
                1,
            )

    def test_workflow_mode_defaults_and_validates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({'test_commands': [['true']]}))
            self.assertEqual(agentic.load_config(path)['workflow_mode'], 'standard')
            path.write_text(json.dumps({
                'test_commands': [['true']], 'workflow_mode': 'quick',
            }))
            self.assertEqual(agentic.load_config(path)['workflow_mode'], 'quick')
            path.write_text(json.dumps({
                'test_commands': [['true']], 'workflow_mode': 'unsafe',
            }))
            with self.assertRaisesRegex(ValueError, 'workflow_mode'):
                agentic.load_config(path)

    def test_auto_publish_defaults_to_false_and_requires_a_boolean(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({'test_commands': [['true']]}))
            self.assertEqual(
                agentic.load_config(path)['automation'], {'auto_publish': False}
            )
            path.write_text(json.dumps({
                'test_commands': [['true']],
                'automation': {'auto_publish': True},
            }))
            self.assertTrue(agentic.load_config(path)['automation']['auto_publish'])
            for value in ('true', 1, None, []):
                path.write_text(json.dumps({
                    'test_commands': [['true']],
                    'automation': {'auto_publish': value},
                }))
                with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, 'automation.auto_publish'
                ):
                    agentic.load_config(path)

    def test_generated_config_disables_automatic_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            self.assertEqual(agentic.init_repository(repo), 0)
            self.assertEqual(
                agentic.load_config(repo / 'agent_config.json')['automation'],
                {'auto_publish': False},
            )


    def test_init_uses_resolved_exclude_for_linked_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / 'base'
            worktree = root / 'worktree'
            sh('git', 'init', '--initial-branch=main', str(base), cwd=root)
            sh('git', 'config', 'user.email', 'agentic-test@example.invalid', cwd=base)
            sh('git', 'config', 'user.name', 'Agentic Test', cwd=base)
            (base / 'README.md').write_text('fixture\n')
            sh('git', 'add', 'README.md', cwd=base)
            sh('git', 'commit', '-m', 'fixture', cwd=base)
            sh('git', 'worktree', 'add', '-b', 'linked', str(worktree), cwd=base)
            self.assertTrue((worktree / '.git').is_file())
            (worktree / 'go.mod').write_text('module example.test/linked\n')

            self.assertEqual(agentic.init_repository(worktree), 0)

            exclude = Path(sh(
                'git', 'rev-parse', '--git-path', 'info/exclude', cwd=worktree,
            ).stdout.strip())
            if not exclude.is_absolute():
                exclude = (worktree / exclude).resolve()
            self.assertIn('agent_config.json', exclude.read_text(encoding='utf-8'))
            self.assertFalse((worktree / '.gitignore').exists())


    def test_init_reports_git_exclude_resolution_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sh('git', 'init', '--initial-branch=main', str(repo), cwd=repo.parent)
            failure = CompletedProcess(
                ['git', 'rev-parse', '--git-path', 'info/exclude'],
                128,
                stdout='fatal: not a git repository\n',
            )
            with (patch('agentic.probe', return_value=(True, str(repo))),
                  patch('agentic_config.subprocess.run', return_value=failure)):
                with self.assertRaisesRegex(RuntimeError, 'Could not resolve Git info/exclude'):
                    agentic.init_repository(repo)
            self.assertFalse((repo / '.gitignore').exists())


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
            ({'developer': {'provider': 'gemini', 'api_key_env': 'BAD-NAME'}}, 'valid environment variable'),
            ({'developer': {'provider': 'gemini', 'auth': 'unsupported'}}, 'auth'),
            ({'security_mode': 'unsafe'}, 'security_mode'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            for extra, expected in invalid:
                with self.subTest(expected=expected):
                    path = Path(temp) / f'{len(list(Path(temp).glob("*.json")))}.json'
                    path.write_text(json.dumps({**base, **extra}))
                    with self.assertRaisesRegex(ValueError, expected):
                        agentic.load_config(path)

    def test_openrouter_endpoint_requires_https_and_explicit_custom_opt_in(self):
        base = {'test_commands': [['true']]}
        cases = [
            ({'base_url': 'http://openrouter.ai/api/v1'}, 'https'),
            ({'base_url': 'https://router.example/api/v1'}, 'approved OpenRouter host'),
        ]
        with tempfile.TemporaryDirectory() as temp:
            for settings, expected in cases:
                with self.subTest(settings=settings):
                    path = Path(temp) / f'{len(list(Path(temp).glob("*.json")))}.json'
                    path.write_text(json.dumps({
                        **base, 'developer': {
                            'provider': 'openrouter', 'model': 'test', **settings,
                        },
                    }))
                    with self.assertRaisesRegex(ValueError, expected):
                        agentic.load_config(path)
            path = Path(temp) / 'custom.json'
            path.write_text(json.dumps({
                **base, 'developer': {
                    'provider': 'openrouter', 'model': 'test',
                    'base_url': 'https://router.example/api/v1',
                    'allow_custom_endpoint': True,
                },
            }))
            config = agentic.load_config(path)
        self.assertTrue(config['developer_agent']['allow_custom_endpoint'])

    def test_malformed_configuration_root_and_enum_values_raise_value_error(self):
        cases = [[], None, {'test_commands': [['true']], 'task_source': []},
                 {'test_commands': [['true']], 'security_mode': {}},
                 {'test_commands': [['true']], 'workflow_mode': []}]
        with tempfile.TemporaryDirectory() as temp:
            for index, value in enumerate(cases):
                with self.subTest(value=value):
                    path = Path(temp) / f'{index}.json'
                    path.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        agentic.load_config(path)

    def test_provider_config_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'unknown.json'
            path.write_text(json.dumps({
                'test_commands': [['true']], 'developer': {'unexpected': True},
            }))
            with self.assertRaisesRegex(ValueError, 'unsupported field'):
                agentic.load_config(path)


    def test_native_provider_api_key_auth_uses_provider_defaults_and_role_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['true']],
                'developer': {'provider': 'claude', 'auth': 'api_key'},
                'reviewer': {
                    'provider': 'copilot', 'api_key_env': 'REVIEW_GITHUB_TOKEN',
                },
            }))
            config = agentic.load_config(path)
        self.assertEqual(config['developer_agent']['auth'], 'api_key')
        self.assertEqual(config['developer_agent']['api_key_env'], 'ANTHROPIC_API_KEY')
        self.assertEqual(config['reviewer_agent']['auth'], 'api_key')
        self.assertEqual(config['reviewer_agent']['api_key_env'], 'REVIEW_GITHUB_TOKEN')


    def test_api_key_preflight_checks_trusted_and_isolated_credentials(self):
        settings = {'provider': 'claude', 'api_key_env': 'CLAUDE_TEST_KEY'}
        with patch.dict(agentic.os.environ, {}, clear=True), \
             patch('agentic.shutil.which', return_value='/usr/bin/claude'):
            with self.assertRaisesRegex(RuntimeError, r'Claude API key is missing; set \$CLAUDE_TEST_KEY'):
                agentic.preflight_agent_settings(settings)
        with patch.dict(agentic.os.environ, {'CLAUDE_TEST_KEY': 'secret'}, clear=True), \
             patch('agentic.shutil.which', return_value='/usr/bin/claude'):
            agentic.preflight_agent_settings(settings)
            with self.assertRaisesRegex(RuntimeError, 'sandbox.pass_env'):
                agentic.preflight_agent_settings(
                    settings,
                    sandbox={'enabled': True, 'pass_env': []},
                )
            agentic.preflight_agent_settings(
                settings,
                sandbox={'enabled': True, 'pass_env': ['CLAUDE_TEST_KEY']},
            )


    def test_sandbox_credential_filter_accepts_role_specific_environment_names(self):
        sandbox = {'enabled': True, 'pass_env': ['OPENAI_API_KEY', 'REVIEW_KEY']}
        self.assertEqual(
            agentic.sandbox_env_for_provider('claude', sandbox, 'REVIEW_KEY'),
            ['REVIEW_KEY'],
        )


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

    def test_permission_policy_defaults_are_provider_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'defaults.json'
            path.write_text(json.dumps({'test_commands': [['python', '--version']]}))
            config = agentic.load_config(path)
        developer = config['developer_agent']['permission_policy']
        reviewer = config['reviewer_agent']['permission_policy']
        self.assertTrue(developer['edit_files'])
        self.assertTrue(developer['create_files'])
        self.assertTrue(developer['run_checks'])
        self.assertFalse(developer['network'])
        self.assertFalse(developer['allow_dangerous'])
        self.assertTrue(reviewer['read_files'])
        self.assertFalse(reviewer['edit_files'])
        self.assertFalse(reviewer['create_files'])
        self.assertFalse(reviewer['run_checks'])

    def test_permission_policy_rejects_reviewer_writes_and_dangerous_trusted_mode(self):
        cases = [
            ({'reviewer': {'edit_files': True}}, 'read-only'),
            ({'developer': {'allow_dangerous': True}}, "requires security_mode 'isolated'"),
        ]
        for policy, expected in cases:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'invalid.json'
                path.write_text(json.dumps({
                    'test_commands': [['python', '--version']],
                    'permission_policy': policy,
                }))
                with self.assertRaisesRegex(ValueError, expected):
                    agentic.load_config(path)

    def test_permission_policy_rejects_unknown_top_level_role(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'invalid-role.json'
            path.write_text(json.dumps({
                'test_commands': [['python', '--version']],
                'permission_policy': {'develper': {'edit_files': False}},
            }))
            with self.assertRaisesRegex(ValueError, 'unsupported top-level key'):
                agentic.load_config(path)

    def test_permission_policy_requires_isolated_mode_for_dangerous_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'isolated.json'
            path.write_text(json.dumps({
                'test_commands': [['python', '--version']],
                'developer': {'provider': 'claude'},
                'security_mode': 'isolated',
                'sandbox': {'enabled': True, 'engine': 'docker', 'image': 'test-image'},
                'permission_policy': {'developer': {'allow_dangerous': True}},
            }))
            config = agentic.load_config(path)
        self.assertTrue(config['developer_agent']['permission_policy']['allow_dangerous'])

    def test_gemini_network_policy_is_rejected_without_a_scoped_translation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, extra in (
                ('trusted', {}),
                ('sandboxed', {
                    'sandbox': {'enabled': True, 'engine': 'docker', 'image': 'test-image'},
                }),
            ):
                with self.subTest(mode=name):
                    path = root / f'{name}.json'
                    path.write_text(json.dumps({
                        'test_commands': [['python', '--version']],
                        'developer': {'provider': 'gemini'},
                        'permission_policy': {'developer': {'network': True}},
                        **extra,
                    }))
                    with self.assertRaisesRegex(ValueError, 'cannot represent network=true'):
                        agentic.load_config(path)

    def test_claude_rejects_create_only_file_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'invalid-claude-policy.json'
            path.write_text(json.dumps({
                'test_commands': [['python', '--version']],
                'developer': {'provider': 'claude'},
                'permission_policy': {
                    'developer': {'edit_files': False, 'create_files': True},
                },
            }))
            with self.assertRaisesRegex(ValueError, 'Claude cannot represent create_files'):
                agentic.load_config(path)

    def test_copilot_rejects_asymmetric_file_capability_during_load(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'invalid-copilot-policy.json'
            path.write_text(json.dumps({
                'test_commands': [['python', '--version']],
                'developer': {'provider': 'copilot'},
                'permission_policy': {
                    'developer': {'edit_files': True, 'create_files': False},
                },
            }))
            with self.assertRaisesRegex(ValueError, 'Copilot cannot represent independent'):
                agentic.load_config(path)

    def test_codex_rejects_unrepresentable_permission_policy_during_load(self):
        cases = [
            ({'edit_files': True, 'create_files': False}, 'edit_files and create_files'),
            ({'run_checks': False}, 'run_checks must match'),
        ]
        for policy, expected in cases:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'invalid-codex-policy.json'
                path.write_text(json.dumps({
                    'test_commands': [['python', '--version']],
                    'developer': {'provider': 'codex'},
                    'permission_policy': {'developer': policy},
                }))
                with self.assertRaisesRegex(ValueError, 'Codex cannot represent.*' + expected):
                    agentic.load_config(path)


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

    def test_preflight_checks_distinct_copilot_paths_for_each_role(self):
        with tempfile.TemporaryDirectory() as temp:
            valid_path = Path(temp) / 'developer-copilot'
            valid_path.write_text('#!/bin/sh\n', encoding='utf-8')
            valid_path.chmod(0o755)
            missing_path = Path(temp) / 'reviewer-copilot'

            with self.assertRaisesRegex(RuntimeError, 'Configured provider .copilot.'):
                agentic.preflight_agent_settings(
                    {'provider': 'copilot', 'executable_path': str(valid_path)},
                    {'provider': 'copilot', 'executable_path': str(missing_path)},
                )

    def test_copilot_download_is_used_by_preflight_and_doctor_reports_remediation(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            data_home = repo / 'xdg-data'
            # The GitHub CLI download is an extensionless binary on every
            # host; Windows launchability must not be inferred from .exe.
            executable = data_home / 'gh' / 'copilot' / 'copilot'
            executable.parent.mkdir(parents=True)
            executable.write_text('#!/bin/sh\n', encoding='utf-8')
            executable.chmod(0o755)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'task_source': 'local',
                'test_commands': [['true']],
                'developer': {'provider': 'copilot'},
                'reviewer': {'provider': 'copilot'},
            }))

            with patch.dict(
                'os.environ', {'XDG_DATA_HOME': str(data_home), 'COPILOT_PATH': ''},
                clear=True,
            ), patch('agentic.shutil.which', return_value=None):
                agentic.preflight_agent_settings({'provider': 'copilot'})

                def fake_probe(argv, cwd, **kwargs):
                    if argv[:2] == ['git', 'rev-parse']:
                        return True, str(repo)
                    return True, 'ok'

                with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                      patch('agentic.probe', side_effect=fake_probe),
                      patch('agentic.preflight_test_commands'),
                      patch('sys.stdout', new_callable=io.StringIO) as output):
                    result = agentic.doctor(repo, config, json_output=True)

        payload = json.loads(output.getvalue())
        provider = next(
            item for item in payload['checks']
            if item['name'] == 'Developer provider (copilot)'
        )
        self.assertEqual(result, 0)
        self.assertTrue(provider['ok'])
        self.assertIn('Copilot CLI was found at', provider['detail'])
        self.assertIn('not on PATH', provider['detail'])

    def test_copilot_executable_path_is_configured_and_reaches_normalized_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'copilot'
            config_path = Path(temp) / 'agent_config.json'
            config_path.write_text(json.dumps({
                'test_commands': [['true']],
                'developer': {
                    'provider': 'copilot',
                    'executable_path': str(path),
                },
            }))
            config = agentic.load_config(config_path)
        self.assertEqual(config['developer_agent']['executable_path'], str(path))
        self.assertIsNone(config['reviewer_agent']['executable_path'])

    def test_github_and_role_specific_copilot_hosts_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / 'agent_config.json'
            config_path.write_text(json.dumps({
                'github_host': 'github.example.com',
                'test_commands': [['true']],
                'developer': {'provider': 'copilot', 'host': 'copilot.example.com'},
                'reviewer': {'provider': 'copilot', 'host': 'review.example.com'},
            }))
            config = agentic.load_config(config_path)
        self.assertEqual(config['github_host'], 'github.example.com')
        self.assertEqual(config['developer_agent']['host'], 'copilot.example.com')
        self.assertEqual(config['reviewer_agent']['host'], 'review.example.com')

    def test_hosts_reject_urls_and_non_copilot_provider_hosts(self):
        for field, value in (('github_host', 'https://github.example.com'),
                             ('github_host', 'github.example.com/path')):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'agent_config.json'
                path.write_text(json.dumps({'test_commands': [['true']], field: value}))
                with self.assertRaisesRegex(ValueError, field):
                    agentic.load_config(path)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['true']],
                'developer': {'provider': 'codex', 'host': 'copilot.example.com'},
            }))
            with self.assertRaisesRegex(ValueError, r'developer\.host'):
                agentic.load_config(path)


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


    def test_openrouter_sandbox_preflight_keeps_api_credential_on_host(self):
        sandbox = {
            'enabled': True, 'engine': 'docker', 'image': 'test-image',
            'pass_env': ['OPENROUTER_API_KEY'],
        }
        with patch.dict(agentic.os.environ, {'OPENROUTER_API_KEY': 'present'}, clear=True):
            agentic.preflight_agent_settings(
                {'provider': 'openrouter', 'api_key_env': 'OPENROUTER_API_KEY'},
                sandbox=sandbox,
            )

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
        for image in ('--privileged', 'image with spaces', 'image;echo unsafe', 'image\nlatest'):
            with self.subTest(image=image), self.assertRaisesRegex(ValueError, 'sandbox.image'):
                agentic.validate_sandbox({'enabled': True, 'image': image})
        with self.assertRaisesRegex(ValueError, 'sandbox.build_network'):
            agentic.validate_sandbox({'enabled': True, 'image': 'test-image', 'build_network': 'yes'})


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
        self.assertTrue(config['sandbox']['build_network'])

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
            ['docker', 'build', '--pull=false', '--network=default', '--tag', 'project:latest',
             '--file', str(dockerfile.resolve()), str(repo.resolve())],
            cwd=repo, stdout=agentic_config.subprocess.PIPE,
            stderr=agentic_config.subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', timeout=1800, check=False,
        )


    def test_preflight_can_disable_sandbox_build_network(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            dockerfile = repo / 'Dockerfile.agentic'
            dockerfile.write_text('FROM python:3.12.7-slim-bookworm\n')
            sandbox = {
                'enabled': True, 'engine': 'docker', 'image': 'project:latest',
                'dockerfile': 'Dockerfile.agentic', 'build_context': '.',
                'build_network': False,
            }
            completed = CompletedProcess([], 0, stdout='built\n')
            with (patch('agentic_config.shutil.which', return_value='/usr/bin/docker'),
                  patch('agentic_config.subprocess.run', return_value=completed) as run):
                agentic.preflight_security('isolated', sandbox, repo=repo)
        self.assertIn('--network=none', run.call_args.args[0])


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
            ['/usr/bin/gh', 'auth', 'status', '--active'],
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
        self.assertTrue(all('category' in item for item in payload['checks']))


    def test_doctor_reports_api_key_mode_without_secret_values_or_native_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'task_source': 'local',
                'test_commands': [[sys.executable, '-c', 'pass']],
                'developer': {
                    'provider': 'claude', 'api_key_env': 'CLAUDE_ROLE_KEY',
                },
                'reviewer': {
                    'provider': 'codex', 'auth': 'api_key',
                    'api_key_env': 'CODEX_ROLE_KEY',
                },
            }))
            probed = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                if argv[:2] == ['git', 'rev-parse']:
                    return True, str(repo)
                return True, 'provider-auth-secret-value'

            with (patch.dict(agentic.os.environ, {
                      'CLAUDE_ROLE_KEY': 'claude-secret',
                      'CODEX_ROLE_KEY': 'codex-secret',
                  }, clear=True),
                  patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/provider'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertNotIn('claude-secret', rendered)
        self.assertNotIn('codex-secret', rendered)
        payload = json.loads(rendered)
        auth_checks = {
            item['name']: item for item in payload['checks']
            if item['category'] == 'agent' and 'authentication' in item['name']
        }
        claude = auth_checks['Developer agent authentication (claude)']
        self.assertEqual(claude['auth_mode'], 'api_key')
        self.assertTrue(claude['credential_set'])
        self.assertIsNone(claude['sandbox_passed'])
        self.assertNotIn('api_key_env', claude)
        self.assertNotIn(['/usr/bin/provider', 'auth', 'status'], probed)
        codex = auth_checks['Reviewer agent authentication (codex)']
        self.assertTrue(codex['credential_set'])
        self.assertNotIn('api_key_env', codex)

    def test_doctor_reports_openrouter_host_credential_with_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'task_source': 'local',
                'security_mode': 'isolated',
                'test_commands': [[sys.executable, '-c', 'pass']],
                'sandbox': {
                    'enabled': True,
                    'engine': 'docker',
                    'image': 'test-image',
                    'pass_env': ['OPENROUTER_API_KEY'],
                },
                'developer': {
                    'provider': 'openrouter', 'model': 'test-model',
                },
                'reviewer': {
                    'provider': 'openrouter', 'model': 'test-model',
                },
            }))

            def fake_probe(argv, cwd, **kwargs):
                return True, 'ok'

            with (patch.dict(agentic.os.environ, {
                      'OPENROUTER_API_KEY': 'router-secret',
                  }, clear=True),
                  patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/docker'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)

        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertNotIn('router-secret', rendered)
        payload = json.loads(rendered)
        auth_checks = {
            item['name']: item for item in payload['checks']
            if item['category'] == 'agent' and 'authentication' in item['name']
        }
        for role in ('Developer', 'Reviewer'):
            check = auth_checks[f'{role} agent authentication (openrouter)']
            self.assertEqual(check['auth_mode'], 'api_key')
            self.assertTrue(check['credential_set'])
            self.assertIsNone(check['sandbox_passed'])
            self.assertNotIn('api_key_env', check)


    def test_doctor_separates_github_access_checks_and_local_mode_skips_them(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'task_source': 'local',
                'test_commands': [[sys.executable, '-c', 'pass']],
                'developer': {'provider': 'copilot'},
                'reviewer': {'provider': 'copilot'},
            }))
            probed = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                return True, 'ok'

            def fake_which(name):
                return '/usr/bin/' + Path(name).name

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', side_effect=fake_which),
                  patch.dict('os.environ', {}, clear=True),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)

        payload = json.loads(output.getvalue())
        github_checks = [item for item in payload['checks'] if item['category'] == 'github']
        self.assertEqual(result, 0)
        self.assertEqual(
            {item['name'] for item in github_checks},
            {'GitHub repository access', 'GitHub issue access'},
        )
        self.assertTrue(all(item['status'] == 'skip' for item in github_checks))
        self.assertTrue(all(item['required'] is False for item in github_checks))
        self.assertFalse(any(argv and argv[0].endswith('gh') for argv in probed))
        provider_checks = [
            item for item in payload['checks']
            if item['category'] == 'agent' and 'provider (copilot)' in item['name']
        ]
        self.assertEqual(
            [item['name'] for item in provider_checks],
            ['Developer provider (copilot)', 'Reviewer provider (copilot)'],
        )


    def test_doctor_probes_github_repository_and_issue_access_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({'test_commands': [[sys.executable, '-c', 'pass']]}))
            probed = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(any(item['name'] == 'GitHub repository access' for item in payload['checks']))
        self.assertTrue(any(item['name'] == 'GitHub issue access' for item in payload['checks']))
        self.assertIn(
            ['/usr/bin/tool', 'repo', 'view', '--json', 'nameWithOwner'], probed,
        )
        self.assertIn(
            ['/usr/bin/tool', 'issue', 'list', '--limit', '1', '--json', 'number'], probed,
        )

    def test_doctor_uses_configured_github_host_for_all_github_probes(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'github_host': 'github.example.com',
                'test_commands': [[sys.executable, '-c', 'pass']],
            }))
            probed = []
            probe_envs = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                probe_envs.append(kwargs.get('env'))
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', return_value='/usr/bin/gh'),
                  patch('sys.stdout', new_callable=io.StringIO)):
                self.assertEqual(agentic.doctor(repo, config, json_output=True), 0)

        gh_commands = [
            argv for argv in probed
            if argv and argv[0] == '/usr/bin/gh'
            and len(argv) > 1 and argv[1] in {'auth', 'repo', 'issue'}
        ]
        self.assertEqual(len(gh_commands), 3)
        self.assertTrue(all(env['GH_HOST'] == 'github.example.com' for env in probe_envs if env))


    def test_doctor_skips_undocumented_native_auth_status_probes(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'pass']],
                'developer': {'provider': 'gemini'},
                'reviewer': {'provider': 'copilot'},
            }))
            probed = []

            def fake_probe(argv, cwd, **kwargs):
                probed.append(argv)
                return True, 'ok'

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', side_effect=fake_probe),
                  patch('agentic.shutil.which', side_effect=lambda name: f'/usr/bin/{name}'),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)

        payload = json.loads(output.getvalue())
        auth_checks = {
            item['name']: item for item in payload['checks']
            if item['category'] == 'agent' and 'authentication' in item['name']
        }
        self.assertEqual(result, 0)
        for provider, role in (('gemini', 'Developer'), ('copilot', 'Reviewer')):
            check = auth_checks[f'{role} agent authentication ({provider})']
            self.assertEqual(check['status'], 'skip')
            self.assertFalse(check['required'])
            self.assertIn('no documented noninteractive', check['detail'])
        self.assertNotIn(['/usr/bin/gemini', 'auth', 'status'], probed)
        self.assertNotIn(['/usr/bin/copilot', 'auth', 'status'], probed)


    def test_doctor_reports_configuration_failure_reason_in_human_and_json_output(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'pass']],
                'developer': {'provider': 'copliot'},
            }))

            def fake_probe(argv, cwd, **kwargs):
                return True, 'ok'

            common_patches = (
                patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                patch('agentic.probe', side_effect=fake_probe),
                patch('agentic.shutil.which', return_value='/usr/bin/tool'),
            )
            with common_patches[0], common_patches[1], common_patches[2], \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                result = agentic.doctor(repo, config)
            human = output.getvalue()

            with common_patches[0], common_patches[1], common_patches[2], \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                result_json = agentic.doctor(repo, config, json_output=True)
            payload = json.loads(output.getvalue())

        expected = 'developer.provider must be one of: codex, gemini, claude, copilot, openrouter'
        self.assertEqual(result, 1)
        self.assertEqual(result_json, 1)
        self.assertIn(f'[FAIL] Agentic config: {expected}', human)
        config_check = next(item for item in payload['checks'] if item['name'] == 'Agentic config')
        self.assertEqual(config_check['detail'], expected)
        self.assertNotEqual(config_check['detail'], 'required check failed')


    def test_doctor_reports_missing_provider_executable_without_probe_output(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'pass']],
                'developer': {'provider': 'copilot'},
                'reviewer': {'provider': 'copilot'},
            }))

            def fake_probe(argv, cwd, **kwargs):
                if argv[-1:] == ['--version']:
                    return False, 'provider output contains private-provider-secret'
                return True, 'ok'

            def fake_which(name):
                return None if name == 'copilot' else '/usr/bin/' + Path(name).name

            patches = (
                patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                patch('agentic.probe', side_effect=fake_probe),
                patch('agentic.shutil.which', side_effect=fake_which),
                patch.dict('os.environ', {'XDG_DATA_HOME': str(repo / 'empty-xdg'), 'COPILOT_PATH': ''}),
            )
            with patches[0], patches[1], patches[2], patches[3], \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                result = agentic.doctor(repo, config, json_output=True)
            payload = json.loads(output.getvalue())
            provider_check = next(
                item for item in payload['checks']
                if item['name'] == 'Developer provider (copilot)'
            )

            with patches[0], patches[1], patches[2], patches[3], \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                human_result = agentic.doctor(repo, config)

        self.assertEqual(result, 1)
        self.assertEqual(human_result, 1)
        self.assertEqual(
            provider_check['detail'],
            "executable not found on PATH; 'gh copilot' is available, locate the "
            "downloaded binary and set COPILOT_PATH or copilot executable_path",
        )
        self.assertIn('executable not found on PATH', output.getvalue())
        self.assertNotIn('private-provider-secret', output.getvalue())


    def test_doctor_replaces_provider_probe_output_with_safe_failure_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[sys.executable, '-c', 'pass']],
                'developer': {'provider': 'copilot'},
                'reviewer': {'provider': 'copilot'},
            }))

            def fake_probe(argv, cwd, **kwargs):
                if argv[-1:] == ['--version']:
                    return False, 'private-provider-secret output'
                return True, 'ok'

            patches = (
                patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                patch('agentic.probe', side_effect=fake_probe),
                patch('agentic.shutil.which', return_value='/usr/bin/copilot'),
            )
            with patches[0], patches[1], patches[2], \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                result = agentic.doctor(repo, config, json_output=True)
            payload = json.loads(output.getvalue())
            provider_check = next(
                item for item in payload['checks']
                if item['name'] == 'Developer provider (copilot)'
            )

            with patches[0], patches[1], patches[2], \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                human_result = agentic.doctor(repo, config)

        expected = 'executable failed its --version check'
        self.assertEqual(result, 1)
        self.assertEqual(human_result, 1)
        self.assertEqual(provider_check['detail'], expected)
        self.assertIn(expected, output.getvalue())
        self.assertNotIn('private-provider-secret', output.getvalue())


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
        self.assertNotIn('normal = lambda value', output.getvalue())
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
                'test_commands': [[
                    sys.executable, '-c',
                    'print("configured-command-" + "secret"); import sys; sys.exit(3)',
                ]],
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
        self.assertEqual(execution['detail'], 'fast command failed (exit code 3)')
        self.assertNotIn('configured-command-secret', output.getvalue())
        self.assertNotIn('print(', output.getvalue())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [[
                    sys.executable, '-c',
                    'print("configured-command-" + "secret"); import sys; sys.exit(3)',
                ]],
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
                result = agentic.doctor(root, config, run_commands=True)

        self.assertEqual(result, 1)
        self.assertNotIn('configured-command-secret', output.getvalue())


    def test_doctor_command_failure_details_distinguish_timeout_and_start_failure(self):
        results = [
            {'argv': ['tool', '--timeout-secret'], 'status': 'timeout', 'exit_code': 124,
             'duration_seconds': 1.0},
            {'argv': ['tool', '--start-secret'], 'status': 'not-started', 'exit_code': None,
             'duration_seconds': 0.1},
        ]
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [['tool', '--secret'], ['tool', '--second-secret']],
            }))
            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', return_value=(True, 'ok')),
                  patch('agentic.shutil.which', return_value='/usr/bin/tool'),
                  patch('agentic.worktree_path_lock', return_value=nullcontext()),
                  patch('agentic_diagnostics._run_configured_command',
                        side_effect=results),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True, run_commands=True)

        payload = json.loads(output.getvalue())
        execution = next(item for item in payload['checks'] if item['name'] == 'Configured command execution')
        self.assertEqual(result, 1)
        self.assertEqual(
            execution['detail'],
            '2 of 2 fast commands failed (1 timed out, 1 could not start)',
        )
        self.assertNotIn('timeout-secret', execution['detail'])
        self.assertNotIn('start-secret', execution['detail'])


    def test_doctor_preflight_detail_does_not_include_configured_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            config = repo / 'agent_config.json'
            config.write_text(json.dumps({
                'test_commands': [['missing-test-tool', 'literal-secret-argument']],
            }))

            with (patch('agentic.installed_git_version', return_value=(2, 48, 0)),
                  patch('agentic.probe', return_value=(True, 'ok')),
                  patch('agentic.shutil.which', return_value=None),
                  patch('sys.stdout', new_callable=io.StringIO) as output):
                result = agentic.doctor(repo, config, json_output=True)

        payload = json.loads(output.getvalue())
        config_check = next(item for item in payload['checks'] if item['name'] == 'Agentic config')
        self.assertEqual(result, 1)
        self.assertIn("executable 'missing-test-tool' was not found on PATH", config_check['detail'])
        self.assertNotIn('literal-secret-argument', output.getvalue())


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
            self.assertEqual(
                agentic.detect_test_commands(fallback)[0],
                [['git', 'diff', '--no-ext-diff', '--no-textconv', '--check']],
            )


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

        for path in (
            'credentials.json', 'config/service-account.json',
            '.docker/config.json', 'keys/id_ecdsa', 'KEYS/ID_ED25519',
            'nested/private.ppk', 'secrets\\prod\\token.json',
            'nested\\AGENTS.md',
        ):
            self.assertTrue(agentic.forbidden(path), path)
            self.assertTrue(sensitive_read(path), path)
        self.assertFalse(sensitive_read('src/credentials.txt'))
        self.assertTrue(agentic.forbidden('.github\\workflows\\release.yml'))
        self.assertTrue(sensitive_read('.github\\workflows\\release.yml'))

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
