"""Offline tests: no GitHub or Codex credentials needed."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class GitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        parent = Path(self.tmp.name)
        bare = parent / 'remote.git'
        sh('git', 'init', '--bare', '--initial-branch=main', str(bare), cwd=parent)
        self.repo = parent / 'project'
        sh('git', 'clone', str(bare), str(self.repo), cwd=parent)
        sh('git', 'config', 'user.name', 'Example Tester', cwd=self.repo)
        sh('git', 'config', 'user.email', 'test@example.com', cwd=self.repo)
        (self.repo / 'readme.md').write_text('# demo\n')
        sh('git', 'add', 'readme.md', cwd=self.repo)
        sh('git', 'commit', '-m', 'initial', cwd=self.repo)
        sh('git', 'push', '-u', 'origin', 'main', cwd=self.repo)

    def test_create_discover_reuse_worktree_without_state_file(self):
        run = agentic.create_worktree(self.repo, 'main', 42)
        found = agentic.runs_for_issue(self.repo, 42)
        self.assertEqual(found, [run])
        self.assertTrue(run.worktree.is_dir())
        self.assertEqual(agentic.choose_existing(found), run)
        self.assertFalse((self.repo / 'new.py').exists())
        (run.worktree / 'new.py').write_text('x=2\n')
        self.assertFalse((self.repo / 'new.py').exists())
        self.assertEqual(agentic.pending_files(run.worktree), ['new.py'])

    def test_central_state_dir_keeps_runtime_artifacts_outside_repository(self):
        state_dir = self.repo.parent / 'agentic-state'
        run = agentic.create_worktree(self.repo, 'main', 43, state_dir)
        self.assertTrue(run.worktree.is_relative_to(state_dir))
        folder = agentic.logs_path(self.repo, run, state_dir)
        self.assertTrue(folder.is_relative_to(state_dir))
        self.assertFalse(run.worktree.is_relative_to(self.repo.parent / f'.{self.repo.name}-agent-worktrees'))
        agentic.cleanup(self.repo, issue=43, confirm=True, state_dir=state_dir)

    def test_repository_lock_blocks_concurrent_mutating_run(self):
        with agentic.repository_lock(self.repo):
            with self.assertRaisesRegex(RuntimeError, 'already active'):
                with agentic.repository_lock(self.repo):
                    pass

    def test_prepare_patch_across_commits_and_staged_changes(self):
        run = agentic.create_worktree(self.repo, 'main', 12)
        (run.worktree / 'first.py').write_text('print(1)\n')
        sh('git', 'add', '-A', cwd=run.worktree)
        sh('git', 'commit', '-m', 'first', cwd=run.worktree)
        (run.worktree / 'second.py').write_text('print(2)\n')
        base, digest1, summary = agentic.prepare_patch(run.worktree, 'main', 10)
        self.assertIn('first.py', summary)
        self.assertIn('second.py', summary)
        self.assertEqual(len(digest1), 64)
        self.assertEqual(agentic.prepare_patch(run.worktree, 'main', 10)[1], digest1)
        (run.worktree / 'second.py').write_text('print(99)\n')
        self.assertNotEqual(agentic.prepare_patch(run.worktree, 'main', 10)[1], digest1)

    def test_prepare_patch_allows_unlimited_file_count(self):
        run = agentic.create_worktree(self.repo, 'main', 17)
        for index in range(11):
            (run.worktree / f'file-{index}.txt').write_text(f'{index}\n')
        base, digest, summary = agentic.prepare_patch(run.worktree, 'main', None)
        self.assertTrue(base)
        self.assertEqual(len(digest), 64)
        self.assertEqual(summary.count('file-'), 11)

    def test_cleanup_previews_and_removes_only_clean_worktrees(self):
        run = agentic.create_worktree(self.repo, 'main', 18)
        self.assertEqual(agentic.cleanup(self.repo, issue=18), 0)
        self.assertTrue(run.worktree.is_dir())
        self.assertEqual(agentic.cleanup(self.repo, issue=18, confirm=True), 0)
        self.assertFalse(run.worktree.exists())

    def test_sensitive_files_refused_before_index(self):
        run = agentic.create_worktree(self.repo, 'main', 9)
        (run.worktree / '.env.production').write_text('SECRET=oops\n')
        with self.assertRaisesRegex(RuntimeError, 'Sensitive'):
            agentic.prepare_patch(run.worktree, 'main', 10)
        self.assertEqual(agentic.git(run.worktree, 'diff', '--cached', '--name-only'), '')

    def test_blocked_change_from_previous_commit_cannot_publish(self):
        run = agentic.create_worktree(self.repo, 'main', 13)
        (run.worktree / 'AGENTS.md').write_text('unexpected instructions\n')
        sh('git', 'add', '-A', cwd=run.worktree)
        sh('git', 'commit', '-m', 'bad', cwd=run.worktree)
        with self.assertRaisesRegex(RuntimeError, 'Forbidden'):
            agentic.prepare_patch(run.worktree, 'main', 10)

    def test_review_is_distinct_codex_readonly_invocation(self):
        run = agentic.create_worktree(self.repo, 'main', 14)
        (run.worktree / 'fixed.py').write_text('answer=42\n')
        base, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(14, 'A problem', 'description')
        calls = []

        def fake_stream(argv, cwd, **kwargs):
            calls.append(argv)
            output = Path(argv[argv.index('--output-last-message') + 1])
            if '--output-schema' in argv:
                output.write_text(json.dumps({'approved': True, 'summary': 'Looks good', 'findings': []}))
            else:
                output.write_text('Made implementation changes.')
            return CompletedProcess(argv, 0, stdout='')

        with patch('agentic.streamed_command', side_effect=fake_stream):
            agentic.run_codex(run.worktree, 'Implement it', output=folder / 'developer-001.txt')
            verdict = agentic.review(run.worktree, issue, folder, base, test_report='pytest passed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0:3], ['codex', 'exec', '--json'])
        self.assertEqual(calls[0][3:5], ['--sandbox', 'workspace-write'])
        self.assertEqual(calls[1][3:5], ['--sandbox', 'read-only'])
        self.assertEqual(verdict['approved'], True)
        self.assertEqual(len(list(folder.glob('review-*.json'))), 2)  # review-schema + review-001
        self.assertIn('git diff --cached', calls[1][-1])
        self.assertIn('description', calls[1][-1])
        self.assertIn('pytest passed', calls[1][-1])

    def test_publish_requires_new_verification_and_confirmation(self):
        run = agentic.create_worktree(self.repo, 'main', 15)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        issue = agentic.Issue(15, 'Fix feature', '')
        folder = agentic.logs_path(self.repo, run)
        cfg = {'test_commands': [[sys.executable, '-c', 'print("ok")']], 'max_retries': 1, 'max_files': 10}
        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.review', return_value={'approved': True, 'summary': 'Good', 'findings': []}),
              patch('builtins.input', return_value='no'),
              patch('agentic.command', wraps=agentic.command) as mock_command):
            agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)
            self.assertFalse(any(c.args[0][:2] == ['gh', 'pr'] for c in mock_command.call_args_list))
            self.assertFalse(any(c.args[0][:3] == ['git', 'push', '-u'] for c in mock_command.call_args_list))
        self.assertIn('fix.py', agentic.git(run.worktree, 'diff', '--cached', '--name-only'))

    def test_pub_blocks_mutation_after_approval(self):
        run = agentic.create_worktree(self.repo, 'main', 16)
        issue = agentic.Issue(16, 'Fix feature', '')
        folder = agentic.logs_path(self.repo, run)
        (run.worktree / 'fix.py').write_text('before\n')
        cfg = {'test_commands': [[sys.executable, '-c', 'print("ok")']], 'max_retries': 1, 'max_files': 10}

        def mutate_on_confirm(*args):
            (run.worktree / 'fix.py').write_text('after\n')
            return 'publish'

        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.review', return_value={'approved': True, 'summary': 'Good', 'findings': []}),
              patch('builtins.input', side_effect=mutate_on_confirm)):
            with self.assertRaisesRegex(RuntimeError, 'Patch changed'):
                agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)
        self.assertEqual(agentic.git(run.worktree, 'log', '-1', '--format=%s'), 'initial')


class PureTests(unittest.TestCase):
    def test_relative_worktree_paths_require_both_wsl_and_windows_git(self):
        with (patch.object(agentic, 'installed_git_version',
                           side_effect=[(2, 48, 0), (2, 48, 0)]),
              patch.object(agentic.shutil, 'which', return_value='git.exe'),
              patch.dict(agentic.os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'})):
            self.assertTrue(agentic.relative_worktree_paths_supported())

        with (patch.object(agentic, 'installed_git_version',
                           side_effect=[(2, 48, 0), (2, 47, 0)]),
              patch.object(agentic.shutil, 'which', return_value='git.exe'),
              patch.dict(agentic.os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'})):
            self.assertFalse(agentic.relative_worktree_paths_supported())

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
            self.assertIn('agent_config.json', (repo / '.gitignore').read_text())
            self.assertTrue((repo / 'AGENTS.md').is_file())

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

    def test_preflight_rejects_missing_test_dependency(self):
        with tempfile.TemporaryDirectory() as dirname:
            with self.assertRaisesRegex(RuntimeError, 'module.*not installed'):
                agentic.preflight_test_commands(
                    Path(dirname), [[sys.executable, '-m', 'module_that_is_not_installed']]
                )

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
            })
        output = out.getvalue()
        self.assertIn('[REVIEW] REJECTED', output)
        self.assertIn('missing recovery behavior', output)
        self.assertIn('Persist failure audit', output)
        self.assertNotIn('"approved": false', output)

    def test_codex_config_overrides_are_added_to_cli(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            output = root / 'message.txt'

            def fake_stream(argv, cwd, **kwargs):
                Path(argv[argv.index('--output-last-message') + 1]).write_text('done')
                return CompletedProcess(argv, 0, stdout='')

            with patch('agentic.streamed_command', side_effect=fake_stream) as streamed:
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
        self.assertTrue(agentic.forbidden('.env'))
        self.assertFalse(agentic.forbidden('src/main.py'))

        with tempfile.TemporaryDirectory() as temp:
            unlimited = Path(temp) / 'agent_config.json'
            unlimited.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'max_files': None,
            }))
            self.assertIsNone(agentic.load_config(unlimited)['max_files'])

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
        self.assertEqual(Path(args.repo), agentic.SCRIPT_DIR)
        self.assertIsNone(args.config)
        self.assertEqual(agentic.build_parser().parse_args(['help']).command, 'help')
        self.assertEqual(agentic.build_parser().parse_args(['shell']).command, 'shell')
        self.assertEqual(agentic.build_parser().parse_args(['dashboard']).command, 'dashboard')
        self.assertEqual(agentic.build_parser().parse_args(['--state-dir', '/tmp/agentic-state']).state_dir, '/tmp/agentic-state')
        self.assertEqual(agentic.__version__, '0.1.0')

    def test_no_arguments_open_interactive_shell(self):
        with patch('agentic_shell.run_shell', return_value=0) as run_shell:
            self.assertEqual(agentic.main([]), 0)
        run_shell.assert_called_once_with(
            Path.cwd(), agentic.SCRIPT_DIR / 'agentic.py', None,
            state_dir=agentic.default_state_dir(),
        )


if __name__ == '__main__':
    unittest.main()

class ResumeIntegrationTests(GitTest):
    def test_resume_reuses_existing_worktree_without_stage_metadata(self):
        run = agentic.create_worktree(self.repo, 'main', 25)
        (run.worktree / 'partial.py').write_text('unfinished = True\n')
        folder = agentic.logs_path(self.repo, run)
        (folder / 'review-001.json').write_text(json.dumps({
            'approved': False,
            'summary': 'Previous review found a missing test',
            'findings': ['Add regression coverage'],
        }))
        args = agentic.build_parser().parse_args(['--repo', str(self.repo),
                                                  '--config', str(Path(__file__).resolve().parents[1] / 'agent_config.example.json'),
                                                  '--issue', '25'])
        issue = agentic.Issue(25, 'Finish work', 'Do it')
        with (patch('agentic.github_metadata', return_value=('owner/repo','main')),
              patch('agentic.get_issue', return_value=issue),
              patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.create_worktree') as create_new,
              patch('agentic.refresh_base') as refresh_base,
              patch('agentic.preflight_test_commands'),
              patch('agentic.develop', return_value=True) as develop):
            agentic.execute(args)
            create_new.assert_not_called()
            refresh_base.assert_called_once_with(self.repo, 'main')
            self.assertEqual(develop.call_args.args[0], run)
            self.assertIn('Previous review found a missing test', develop.call_args.kwargs['initial_feedback'])
            self.assertTrue((run.worktree / 'partial.py').exists())


class StreamingTests(unittest.TestCase):
    def test_codex_events_become_readable_progress(self):
        event = json.dumps({'type': 'item.started', 'item': {
            'type': 'command_execution', 'command': 'python -m pytest -q'}})
        self.assertIn('Running command: python -m pytest -q', agentic._codex_progress(event))
        event = json.dumps({'type': 'item.completed', 'item': {
            'type': 'file_change', 'changes': [{'path': 'src/invoice.py'}]}})
        self.assertIn('src/invoice.py', agentic._codex_progress(event))
        self.assertIsNone(agentic._codex_progress(json.dumps({'type': 'item.updated',
                                                             'item': {'type': 'reasoning'}})))

    def test_streamed_codex_json_saves_logs_and_shows_progress(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            log = root / 'developer-001.log'
            script = ('import json,sys; '
                      'print(json.dumps({"type":"item.started","item":'
                      '{"type":"command_execution","command":"ls tests"}}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"command_execution","exit_code":0}}),flush=True)')
            with patch('sys.stdout', new_callable=StringIO) as out:
                result = agentic.streamed_command([sys.executable, '-c', script], root,
                                                  label='DEVELOPER', log_file=log,
                                                  timeout=10, codex_json=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn('Running command: ls tests', out.getvalue())
            self.assertIn('Command finished (exit 0)', log.read_text())
            self.assertIn('item.started', log.with_suffix('.jsonl').read_text())

    def test_streamed_command_reports_heartbeat_during_silence(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            with patch('sys.stdout', new_callable=StringIO) as out:
                result = agentic.streamed_command(
                    [sys.executable, '-c', 'import time; time.sleep(1.5); print("ok")'],
                    root, label='TESTS', log_file=root / 'tests.log',
                    timeout=10, heartbeat=0)
            self.assertEqual(result.returncode, 0)
            self.assertIn('Still running', out.getvalue())
            self.assertIn('ok', out.getvalue())

    def test_streamed_command_allows_no_timeout(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            result = agentic.streamed_command(
                [sys.executable, '-c', 'print("ok")'],
                root, label='TESTS', log_file=root / 'tests.log',
                timeout=None,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn('ok', result.stdout)

    def test_test_runner_writes_logs_outside_worktree(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            sh('git', 'init', cwd=root)
            ok, report = agentic.run_tests(root, [[sys.executable, '-c', 'print("hello")']])
            self.assertTrue(ok)
            self.assertIn('hello', report)
            self.assertFalse((root / '.agent-test-output.log').exists())

    def test_stream_timeout_does_not_hang(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            with patch('sys.stdout', new_callable=StringIO):
                result = agentic.streamed_command(
                    [sys.executable, '-c', 'import time; time.sleep(3)'],
                    root, label='TESTS', log_file=root / 'tests.log',
                    timeout=1, heartbeat=0)
            self.assertEqual(result.returncode, 124)
            self.assertIn('Time limit', (root / 'tests.log').read_text())
