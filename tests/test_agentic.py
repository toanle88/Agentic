"""Offline tests: no GitHub or Codex credentials needed."""
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic
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


    def test_workflow_binding_supports_direct_script_execution(self):
        wrapper = agentic.preflight_test_commands
        original_module = wrapper.__module__
        try:
            wrapper.__module__ = '__main__'
            with tempfile.TemporaryDirectory() as dirname:
                agentic._workflow_call(
                    'preflight_test_commands',
                    Path(dirname), [[sys.executable, '-c', 'pass']],
                )
        finally:
            wrapper.__module__ = original_module


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

    def test_copilot_host_is_scoped_to_provider_environment_and_model_errors_are_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'copilot-dev.txt'
            captured = {}

            def fake_stream(argv, cwd, **kwargs):
                captured.update(kwargs)
                return CompletedProcess(argv, 1, stdout='Model "gpt-5.6-luna" is not available')

            with patch.dict(os.environ, {'GH_HOST': 'repository.example.com'}, clear=False), \
                 patch('agentic_execution.streamed_command', side_effect=fake_stream):
                ok, message = agentic.run_cli_provider(
                    root, 'implement', output=output, provider='copilot',
                    model='gpt-5.6-luna', host='copilot.example.com',
                    executable_path='/tmp/copilot',
                )

                self.assertFalse(ok)
                self.assertEqual(os.environ['GH_HOST'], 'repository.example.com')
            self.assertEqual(captured['env']['GH_HOST'], 'repository.example.com')
            self.assertEqual(captured['env']['COPILOT_GH_HOST'], 'copilot.example.com')
            self.assertIn("Copilot model 'gpt-5.6-luna' is unavailable", message)
            self.assertIn("'copilot.example.com'", message)


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

    def test_github_issue_commands_use_explicit_host(self):
        with patch('agentic.command', return_value=CompletedProcess(
            ['gh'], 0, stdout=json.dumps({'number': 2, 'title': 'Fix', 'body': '', 'state': 'OPEN'})
        )) as command_mock:
            agentic.get_issue(Path('.'), 'owner/repo', 2, 'github.example.com')
        argv = command_mock.call_args.args[0]
        self.assertEqual(argv[:2], ['gh', 'issue'])
        self.assertEqual(command_mock.call_args.kwargs['env']['GH_HOST'], 'github.example.com')


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
        chat = agentic.build_parser().parse_args(['--chat', '--issue', '42'])
        self.assertTrue(chat.chat)
        self.assertEqual(chat.issue, 42)
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
