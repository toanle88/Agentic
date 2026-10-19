"""Offline tests: no GitHub or Codex credentials needed."""
from contextlib import nullcontext
import json
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic
import agentic_diagnostics
import agentic_execution


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)




class PureTests(unittest.TestCase):
    def test_relative_worktree_paths_require_both_wsl_and_windows_git(self):
        with (patch.object(agentic, 'installed_git_version',
                           side_effect=[(2, 48, 0), (2, 48, 0)]),
              patch.object(agentic.shutil, 'which', return_value='git.exe'),
              patch.object(agentic.os, 'name', 'posix'),
              patch.dict(agentic.os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'})):
            self.assertTrue(agentic.relative_worktree_paths_supported())

        with (patch.object(agentic, 'installed_git_version',
                           side_effect=[(2, 48, 0), (2, 47, 0)]),
              patch.object(agentic.shutil, 'which', return_value='git.exe'),
              patch.object(agentic.os, 'name', 'posix'),
              patch.dict(agentic.os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'})):
            self.assertFalse(agentic.relative_worktree_paths_supported())

    def test_run_summary_uses_ascii_on_legacy_console_encoding(self):
        class LegacyConsole(io.StringIO):
            encoding = 'cp1252'

        output = LegacyConsole()
        run = agentic.Run(1, 'agent/issue-1-test', Path('C:/agentic-worktree'))
        with patch.object(agentic.sys, 'stdout', output):
            agentic.print_run_summary(run, 'APPROVED', 'continue')
        self.assertIn('-' * 72, output.getvalue())
        self.assertNotIn('─', output.getvalue())

    def test_relative_worktree_configuration_is_not_written_when_unsupported(self):
        with (patch('agentic.relative_worktree_paths_supported', return_value=False),
              patch('agentic.git') as git_mock):
            self.assertFalse(agentic.configure_relative_worktree_paths(Path('/repo')))
        git_mock.assert_not_called()

    def test_existing_worktree_repair_uses_relative_paths_when_supported(self):
        repo = Path('/repo')
        worktree = Path('/repo/../.repo-agent-worktrees/issue-7-abcd1234')
        with (patch('agentic.relative_worktree_paths_supported', return_value=True),
              patch('agentic.git') as git_mock):
            self.assertTrue(agentic.repair_relative_worktree(repo, worktree))
        self.assertEqual(git_mock.call_args_list, [
            call(repo, 'config', 'worktree.useRelativePaths', 'true'),
            call(repo, 'worktree', 'repair', '--relative-paths', str(worktree)),
        ])

    def test_refresh_base_fetches_remote_branch(self):
        repo = Path('/repo')
        with patch('agentic.git') as git_mock:
            agentic.refresh_base(repo, 'main')
        git_mock.assert_called_once_with(repo, 'fetch', 'origin', 'main')

    def test_resume_feedback_loads_latest_failed_review_and_tests(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder = Path(dirname)
            (folder / 'review-schema.json').write_text('{}')
            (folder / 'review-001.json').write_text(json.dumps({
                'approved': False,
                'summary': 'Fix duplicate prevention',
                'findings': ['Use the canonical source key'],
            }))
            (folder / 'tests-001.txt').write_text('go test ./... failed: duplicate row')
            feedback = agentic.load_resume_feedback(folder)
        self.assertIn('Fix duplicate prevention', feedback)
        self.assertIn('Use the canonical source key', feedback)
        self.assertIn('duplicate row', feedback)

    def test_resume_feedback_ignores_approved_review(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder = Path(dirname)
            (folder / 'review-001.json').write_text(json.dumps({
                'approved': True, 'summary': 'Good', 'findings': [],
            }))
            self.assertEqual(agentic.load_resume_feedback(folder), '')

    def test_review_result_parser_accepts_markdown_wrapped_json(self):
        parsed = agentic.parse_review_result(
            'Review result:\n```json\n'
            '{"approved": true, "summary": "Looks good", "findings": []}\n'
            '```'
        )
        self.assertEqual(parsed["approved"], True)
        self.assertEqual(parsed["findings"], [])

    def test_repeated_review_findings_are_fingerprinted_and_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first = {
                'approved': False, 'summary': 'Fix it',
                'findings': ['[BLOCKER] Production wiring is unavailable at cmd/api/runtime.go:82'],
            }
            second = {
                'approved': False, 'summary': 'Still broken',
                'findings': ['BLOCKER — Production wiring is unavailable at cmd/api/runtime.go:119'],
            }
            agentic.record_review_findings(folder, first)
            result = agentic.record_review_findings(folder, second)
            self.assertEqual(result['_finding_analysis'][0]['severity'], 'BLOCKER')
            self.assertEqual(len(result['_repeated_findings']), 1)
            self.assertIn('Recurring review findings', agentic.review_history_feedback(folder))

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

    def test_invalid_review_result_is_marked_as_tooling_error(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            folder = worktree / 'logs'
            folder.mkdir()
            issue = agentic.Issue(19, 'Review', '')
            with patch('agentic.run_codex', return_value=(True, 'not json')):
                verdict = agentic.review(worktree, issue, folder, 'base')
        self.assertTrue(verdict['_tooling_error'])
        self.assertFalse(verdict['approved'])

    def test_review_wrong_json_shape_is_marked_as_tooling_error(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            folder = worktree / 'logs'
            folder.mkdir()
            issue = agentic.Issue(20, 'Review shape', '')
            with patch('agentic.run_agent', return_value=(True, json.dumps({
                'approved': 'yes', 'summary': 'bad', 'findings': [],
            }))):
                verdict = agentic.review(worktree, issue, folder, 'base')
        self.assertTrue(verdict['_tooling_error'])
        self.assertIn('wrong shape', verdict['summary'])

    def test_review_schema_requires_every_declared_property(self):
        self.assertEqual(
            set(agentic.REVIEW_SCHEMA['properties']),
            set(agentic.REVIEW_SCHEMA['required']),
        )

    def test_review_rejects_wrong_skipped_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            folder = worktree / 'logs'
            folder.mkdir()
            issue = agentic.Issue(20, 'Review skipped shape', '')
            with patch('agentic.run_agent', return_value=(True, json.dumps({
                'approved': True, 'summary': 'bad', 'findings': [], 'skipped': 'not a list',
            }))):
                verdict = agentic.review(worktree, issue, folder, 'base')
        self.assertTrue(verdict['_tooling_error'])
        self.assertIn('wrong shape', verdict['summary'])

    def test_developer_review_action_validation_requires_each_entry(self):
        self.assertIn(
            'missing Evidence',
            agentic.validate_developer_review_actions(
                'Finding: first\nAction: fixed\n', ['first'],
            ),
        )
        self.assertIn(
            'contains 2 findings',
            agentic.validate_developer_review_actions(
                'Finding: first\nAction: fixed\nEvidence: test\n',
                ['first', 'second'],
            ),
        )
        self.assertEqual(
            agentic.validate_developer_review_actions(
                'Finding: first\nAction: fixed\nEvidence: test\n', ['first'],
            ),
            '',
        )

    def test_issue_investigation_returns_structured_feedback(self):
        with tempfile.TemporaryDirectory() as temp:
            worktree = Path(temp)
            folder = worktree / 'logs'
            folder.mkdir()
            issue = agentic.Issue(21, 'Investigate', 'Confirm the behavior')
            message = json.dumps({
                'summary': 'The issue is actionable',
                'findings': ['The relevant code path exists'],
            })
            def fake_run_agent(*args, **kwargs):
                kwargs['output'].write_text(message)
                return True, message
            with patch('agentic.run_agent', side_effect=fake_run_agent) as run_agent:
                verdict = agentic.investigate_issue(worktree, issue, folder)
            self.assertNotIn('status', verdict)
            self.assertEqual(verdict['findings'], ['The relevant code path exists'])
            self.assertTrue(run_agent.call_args.kwargs['readonly'])
            prompt = run_agent.call_args.args[1]
            self.assertIn('practical implementation handoff', prompt)
            self.assertIn('What does the repository do today', prompt)
            self.assertIn('focused implementation and regression-test steps', prompt)
            self.assertIn('Do not return a status field', prompt)
            self.assertTrue((folder / 'investigation-001.json').is_file())
            self.assertTrue((folder / 'investigation-schema.json').is_file())
            schema = json.loads((folder / 'investigation-schema.json').read_text())
            self.assertNotIn('status', schema['properties'])
            self.assertEqual(schema['required'], ['summary', 'findings'])

    def test_issue_investigation_invalid_output_is_not_treated_as_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'logs'
            folder.mkdir()
            with patch('agentic.run_agent', return_value=(True, 'not json')):
                verdict = agentic.investigate_issue(
                    Path(temp), agentic.Issue(22, 'Investigate', ''), folder,
                )
        self.assertTrue(verdict['_tooling_error'])
        self.assertIn('invalid', verdict['summary'])

    def test_cached_investigation_is_invalidated_when_issue_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'logs'
            folder.mkdir()
            issue = agentic.Issue(23, 'Original', 'Original body')
            message = json.dumps({'summary': 'Valid', 'findings': []})
            with patch('agentic.run_agent', return_value=(True, message)):
                agentic.investigate_issue(Path(temp), issue, folder)
            self.assertIsNotNone(agentic._latest_investigation(folder, issue))
            changed = agentic.Issue(23, 'Changed', 'Original body')
            self.assertIsNone(agentic._latest_investigation(folder, changed))

    def test_investigation_feedback_does_not_require_confirmation(self):
        with patch('builtins.input') as input_mock:
            self.assertEqual(
                agentic.investigation_feedback({
                    'summary': 'Relevant code found',
                    'findings': ['Add a regression test'],
                }),
                'Independent issue investigation feedback:\n'
                'Summary: Relevant code found\n'
                'Findings:\n'
                '- Add a regression test',
            )
        input_mock.assert_not_called()

    def test_investigation_retry_recomputes_remaining_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'logs'
            folder.mkdir()
            run = agentic.Run(None, 'agent/issue-24-aabbccdd', Path(temp) / 'worktree')
            issue = agentic.Issue(24, 'Investigate retry', '')
            config = {
                'reviewer_agent': {'provider': 'codex'},
                'effective_reviewer_timeout_seconds': 30,
                'sandbox': None,
            }
            results = [
                {'summary': 'Invalid first result', 'findings': [], '_tooling_error': True},
                {'summary': 'Valid retry result', 'findings': []},
            ]
            with (patch('agentic._latest_investigation', return_value=None),
                  patch('agentic._remaining_timeout', side_effect=[17, 4]) as remaining,
                  patch('agentic.investigate_issue', side_effect=results) as investigate):
                verdict = agentic._run_investigation(run, issue, config, folder, deadline=100.0)
        self.assertEqual(verdict['summary'], 'Valid retry result')
        self.assertEqual(remaining.call_args_list, [call(100.0, 30), call(100.0, 30)])
        self.assertEqual([item.kwargs['timeout'] for item in investigate.call_args_list], [17, 4])

    def test_preflight_rejects_missing_test_dependency(self):
        with tempfile.TemporaryDirectory() as dirname:
            with self.assertRaisesRegex(RuntimeError, 'module.*not installed'):
                agentic.preflight_test_commands(
                    Path(dirname), [[sys.executable, '-m', 'module_that_is_not_installed']]
                )

    def test_preflight_rejects_missing_test_executable(self):
        with tempfile.TemporaryDirectory() as dirname, patch(
            'agentic.shutil.which', return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                agentic.preflight_test_commands(Path(dirname), [['missing-test-tool', '--check']])

    def test_environment_test_failures_are_not_retryable(self):
        self.assertFalse(agentic.retryable_test_failure('No module named pytest'))
        self.assertFalse(agentic.retryable_test_failure('listen tcp: address already in use'))
        self.assertTrue(agentic.retryable_test_failure('AssertionError: expected 2, got 3'))

    def test_review_summary_is_compact_and_actionable(self):
        from io import StringIO
        with patch('sys.stdout', new_callable=StringIO) as out:
            agentic.print_review_summary({
                'approved': False,
                'summary': 'Reject: missing recovery behavior',
                'findings': ['Add restart coverage', 'Persist failure audit'],
                'skipped': ['Browser check unavailable'],
            })
        output = out.getvalue()
        self.assertIn('[REVIEW] REJECTED', output)
        self.assertIn('missing recovery behavior', output)
        self.assertIn('Persist failure audit', output)
        self.assertIn('Skipped validation: 1', output)
        self.assertIn('Browser check unavailable', output)
        self.assertNotIn('"approved": false', output)

    def test_codex_config_overrides_are_added_to_cli(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            output = root / 'message.txt'

            def fake_stream(argv, cwd, **kwargs):
                Path(argv[argv.index('--output-last-message') + 1]).write_text('done')
                return CompletedProcess(argv, 0, stdout='')

            with patch('agentic_execution.streamed_command', side_effect=fake_stream) as streamed:
                ok, message = agentic.run_codex(
                    root, 'prompt', output=output,
                    model='gpt-5.6-luna', reasoning_effort='max', timeout=7,
                )
            self.assertTrue(ok)
            self.assertEqual(message, 'done')
            argv = streamed.call_args.args[0]
            self.assertIn('--model', argv)
            self.assertIn('gpt-5.6-luna', argv)
            self.assertIn('model_reasoning_effort="max"', argv)
            self.assertEqual(streamed.call_args.kwargs['timeout'], 7)

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


    def test_cli_provider_uses_headless_structured_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'developer-001.txt'

            def fake_stream(argv, cwd, **kwargs):
                kwargs['log_file'].with_suffix('.jsonl').write_text(
                    json.dumps({'type': 'result', 'response': 'finished'}) + '\n'
                )
                return CompletedProcess(argv, 0, stdout='')

            with patch('agentic_execution.streamed_command', side_effect=fake_stream) as streamed:
                ok, message = agentic.run_cli_provider(
                    root, 'prompt', output=output, provider='gemini', model='gemini-3-pro-preview'
                )
            self.assertTrue(ok)
            self.assertEqual(message, 'finished')
            argv = streamed.call_args.args[0]
            self.assertEqual(argv[:2], ['gemini', '-p'])
            self.assertIn('--output-format', argv)
            self.assertIn('stream-json', argv)
            self.assertIn('--approval-mode', argv)
            self.assertIn('yolo', argv)

    def test_cli_provider_modes_are_restricted_for_reviewers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_stream(argv, cwd, **kwargs):
                if kwargs.get('codex_json'):
                    kwargs['log_file'].with_suffix('.jsonl').write_text(
                        json.dumps({'type': 'message', 'content': 'done'}) + '\n'
                    )
                return CompletedProcess(argv, 0, stdout='done')

            with patch('agentic_execution.streamed_command', side_effect=fake_stream) as streamed:
                agentic.run_cli_provider(
                    root, 'review', output=root / 'claude-review.txt',
                    provider='claude', readonly=True, model='claude-sonnet',
                )
                claude_review = streamed.call_args.args[0]
                agentic.run_cli_provider(
                    root, 'implement', output=root / 'claude-dev.txt',
                    provider='claude', readonly=False, model='claude-sonnet',
                )
                claude_developer = streamed.call_args.args[0]
                agentic.run_cli_provider(
                    root, 'review', output=root / 'copilot-review.txt',
                    provider='copilot', readonly=True, model='gpt-5.6-luna',
                )
                copilot_review = streamed.call_args.args[0]
                agentic.run_cli_provider(
                    root, 'implement', output=root / 'copilot-dev.txt',
                    provider='copilot', readonly=False, model='gpt-5.6-luna',
                )
                copilot_developer = streamed.call_args.args[0]

            self.assertIn('--permission-mode', claude_review)
            self.assertIn('plan', claude_review)
            self.assertNotIn('--dangerously-skip-permissions', claude_review)
            self.assertIn('--dangerously-skip-permissions', claude_developer)
            self.assertNotIn('--permission-mode', claude_developer)
            self.assertIn('--available-tools', copilot_review)
            self.assertIn('--deny-tool', copilot_review)
            self.assertNotIn('--allow-all-tools', copilot_review)
            self.assertIn('--allow-all-tools', copilot_developer)
            self.assertIn('--allow-all-paths', copilot_developer)

    def test_provider_output_parsing_handles_mixed_events_and_copilot_text(self):
        raw = '\n'.join([
            'not json',
            json.dumps({'type': 'assistant', 'content': [{'text': 'part one'}]}),
            json.dumps({'type': 'tool_call', 'name': 'read_file'}),
            json.dumps({'type': 'result', 'result': {'content': [{'text': 'final'}]}}),
        ])
        self.assertEqual(agentic_execution._extract_provider_message(raw, 'claude'), 'final')
        self.assertEqual(agentic_execution._extract_provider_message('copilot output\n', 'copilot'), 'copilot output')
        self.assertIn('Using tool: read_file', agentic_execution._provider_progress(
            json.dumps({'type': 'tool_use', 'tool': {'name': 'read_file'}}), 'gemini'
        ))
        self.assertIn('ERROR:', agentic_execution._provider_progress(
            json.dumps({'type': 'turn.failed', 'message': 'failed'}), 'claude'
        ))

    def test_cli_provider_failure_is_returned_and_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_stream(argv, cwd, **kwargs):
                return CompletedProcess(argv, 7, stdout='provider failed')

            with patch('agentic_execution.streamed_command', side_effect=fake_stream):
                ok, message = agentic.run_cli_provider(
                    root, 'prompt', output=root / 'developer.txt', provider='copilot'
                )
            self.assertFalse(ok)
            self.assertEqual(message, 'provider failed')
            self.assertEqual((root / 'developer.txt').read_text(), 'provider failed')

    def test_run_agent_dispatches_settings_and_preserves_no_timeout(self):
        root = Path('/tmp/agentic-provider-dispatch-test')
        with (patch('agentic.run_cli_provider', return_value=(True, 'cli')) as cli,
              patch('agentic.run_codex', return_value=(True, 'codex')) as codex,
              patch('agentic.run_openrouter', return_value=(True, 'router')) as router):
            result = agentic.run_agent(
                root, 'prompt', output=root / 'message.txt',
                settings=agentic.AgentSettings(
                    provider='gemini', model='gemini-model',
                    timeout_seconds=None,
                ),
            )
            self.assertEqual(result, (True, 'cli'))
            cli.assert_called_once_with(
                root, 'prompt', output=root / 'message.txt', provider='gemini',
                readonly=False, timeout=None, model='gemini-model',
            )

            agentic.run_agent(
                root, 'prompt', output=root / 'message.txt',
                settings={'provider': 'codex', 'model': 'codex-model',
                          'reasoning_effort': 'max', 'timeout_seconds': None},
            )
            codex.assert_called_once_with(
                root, 'prompt', output=root / 'message.txt', readonly=False,
                schema=None, timeout=None, model='codex-model', reasoning_effort='max',
            )

            agentic.run_agent(
                root, 'prompt', output=root / 'message.txt',
                settings={'provider': 'openrouter', 'model': 'router-model',
                          'api_key_env': 'ROUTER_KEY', 'base_url': 'https://router.test'},
            )
            router.assert_called_once_with(
                root, 'prompt', output=root / 'message.txt', readonly=False,
                schema=None, timeout=None, model='router-model', reasoning_effort=None,
                api_key_env='ROUTER_KEY', base_url='https://router.test',
            )









    def test_issue_context_preserves_middle_acceptance_requirements(self):
        issue_body = (
            'opening context\n' + ('background ' * 900) +
            '\nAcceptance criteria:\n- preserve-this-requirement\n' +
            ('implementation detail ' * 900) + '\nclosing context\n'
        )
        context = agentic._issue_context(issue_body)
        self.assertLessEqual(len(context), agentic.ISSUE_CONTEXT_CHARS)
        self.assertIn('preserve-this-requirement', context)

    def test_compact_feedback_can_preserve_all_required_findings(self):
        findings = [f'finding-{index}' for index in range(9)]
        rendered = agentic._compact_feedback(
            {'summary': 'summary', 'findings': findings},
            limit=None, max_findings=None,
        )
        for finding in findings:
            self.assertIn(finding, rendered)

    def test_prompt_context_is_bounded_without_losing_tail(self):
        issue = agentic.Issue(22, 'task', 'head-' + ('x' * 8_000) + '-tail')
        prompt = agentic.agent_prompt(issue, 'feedback-' + ('y' * 8_000))
        self.assertLessEqual(len(prompt), 10_000 + agentic.ISSUE_CONTEXT_CHARS + agentic.FEEDBACK_CONTEXT_CHARS)
        self.assertIn('...[truncated]...', prompt)
        self.assertIn('-tail', prompt)

    def test_required_review_findings_do_not_expand_prompt_without_bound(self):
        issue = agentic.Issue(23, 'task', 'body')
        prompt = agentic.agent_prompt(
            issue, required_review_findings=['finding-' + ('x' * 700)] * 100,
        )
        self.assertLessEqual(len(prompt), 10_000 + agentic.ISSUE_CONTEXT_CHARS + agentic.FEEDBACK_CONTEXT_CHARS)




    def test_secret_values_are_redacted_from_output(self):
        with patch.dict(agentic.os.environ, {'TEST_API_KEY': 'super-secret-value'}, clear=True):
            value = agentic.redact_secrets(
                'Authorization: Bearer bearer-secret-value\n'
                'api_key=inline-secret-value\n'
                'env=super-secret-value'
            )
        self.assertNotIn('bearer-secret-value', value)
        self.assertNotIn('inline-secret-value', value)
        self.assertNotIn('super-secret-value', value)
        self.assertGreaterEqual(value.count('[REDACTED]'), 3)

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
                    'import os, sys; sys.exit(0 if os.getcwd() == sys.argv[1] else 1)',
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
        lock.assert_called_once_with(root, worktree)

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
                    common = root / ('.git' if cwd == root else 'other.git')
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

    def test_issue_eligibility_depends_only_on_builtin_status(self):
        self.assertTrue(agentic.eligible(agentic.Issue(1, 'Bug', 'hello')))
        self.assertTrue(agentic.eligible(agentic.Issue(2, 'Security task', '', state='OPEN')))
        self.assertFalse(agentic.eligible(agentic.Issue(3, 'Closed', '', state='CLOSED')))

    def test_issue_list_shows_every_open_issue_without_labels(self):
        issues_data = [
            {'number': 30, 'title': 'Feature', 'body': 'build it', 'state': 'OPEN'},
            {'number': 10, 'title': 'Urgent security fix', 'body': 'fix it', 'state': 'OPEN',
             'labels': [{'name': 'security'}], 'blockedBy': True},
            {'number': 20, 'title': 'Another fix', 'body': '', 'state': 'CLOSED'},
            {'number': 40, 'title': 'No labels', 'body': '', 'state': 'OPEN'},
        ]

        def fake_fetch(argv, cwd, **kwargs):
            self.assertIn('--state', argv)
            self.assertEqual(argv[argv.index('--state') + 1], 'open')
            self.assertEqual(argv[argv.index('--json') + 1], 'number,title,body,state')
            return CompletedProcess(argv, 0, stdout=json.dumps(issues_data))

        with patch('agentic.command', side_effect=fake_fetch):
            issues = agentic.fetch_issues(Path('.'), 'owner/repo')
        self.assertEqual([issue.number for issue in issues], [10, 30, 40])
        self.assertTrue(all(issue.state == 'OPEN' for issue in issues))

    def test_issue_view_omits_label_fetch(self):
        with patch('agentic.command', return_value=CompletedProcess(
            ['gh'], 0, stdout=json.dumps({'number': 2, 'title': 'Fix', 'body': '', 'state': 'OPEN'})
        )) as call:
            issue = agentic.get_issue(Path('.'), 'owner/repo', 2)
        self.assertEqual(issue.number, 2)
        self.assertEqual(call.call_args.args[0][-1], 'number,title,body,state')

    def test_issue_list_output_does_not_show_score(self):
        from io import StringIO
        with (patch('agentic.discover_runs', return_value=[]),
              patch('sys.stdout', new_callable=StringIO) as out):
            agentic.show_issues(Path('.'), [agentic.Issue(7, 'Simple task', '')])
        self.assertIn('Simple task', out.getvalue())
        self.assertNotIn('score=', out.getvalue())

    def test_review_prompt_inspects_current_changes(self):
        issue = agentic.Issue(22, 'task', 'details')
        prompt = agentic.agent_prompt(issue)
        self.assertIn('ALREADY been completed', prompt)
        self.assertIn('git diff --cached', prompt)

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

    def test_runs_and_issue_list_do_not_require_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            runs_args = agentic.build_parser().parse_args(['--repo', str(repo), '--runs'])
            with patch('agentic.show_runs') as show_runs:
                agentic.execute_locked(runs_args, repo)
            show_runs.assert_called_once_with(repo)

            list_args = agentic.build_parser().parse_args(['--repo', str(repo), '--list'])
            with (patch('agentic.github_metadata', return_value=('owner/repo', 'main')),
                  patch('agentic.fetch_issues', return_value=[]),
                  patch('agentic.show_issues') as show_issues):
                agentic.execute_locked(list_args, repo)
            show_issues.assert_called_once_with(repo, [])

    def test_cli_options(self):
        args = agentic.build_parser().parse_args(['--issue', '42', '--publish'])
        self.assertIsNone(args.command)
        self.assertEqual(args.issue, 42)
        self.assertTrue(args.publish)
        manual = agentic.build_parser().parse_args([
            '--issue', '42', '--mark-done', '--reason', 'human reviewed',
        ])
        self.assertTrue(manual.mark_done)
        self.assertEqual(manual.reason, 'human reviewed')
        self.assertIsNone(agentic.build_parser().parse_args([
            '--issue', '42', '--mark-done',
        ]).reason)
        self.assertEqual(Path(args.repo), agentic.SCRIPT_DIR)
        self.assertIsNone(args.config)
        self.assertEqual(agentic.build_parser().parse_args(['help']).command, 'help')
        self.assertEqual(agentic.build_parser().parse_args(['shell']).command, 'shell')
        self.assertEqual(agentic.build_parser().parse_args(['dashboard']).command, 'dashboard')
        self.assertEqual(agentic.build_parser().parse_args(['status']).command, 'status')
        self.assertTrue(agentic.build_parser().parse_args(['runs', '--json']).json)
        self.assertEqual(agentic.build_parser().parse_args(['--state-dir', '/tmp/agentic-state']).state_dir, '/tmp/agentic-state')
        self.assertRegex(agentic.__version__, r'^\d+\.\d+\.\d+$')

    def test_no_arguments_open_interactive_shell(self):
        with patch('agentic_shell.run_shell', return_value=0) as run_shell:
            self.assertEqual(agentic.main([]), 0)
        run_shell.assert_called_once_with(
            Path.cwd(), agentic.SCRIPT_DIR / 'agentic.py', None,
            state_dir=agentic.default_state_dir(),
        )

    def test_main_propagates_runner_exit_status(self):
        with patch('agentic.execute', return_value=1) as execute:
            self.assertEqual(agentic.main(['--issue', '42']), 1)
        execute.assert_called_once()


if __name__ == '__main__':
    unittest.main()
