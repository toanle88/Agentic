"""Focused publication tests."""
import sys
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from test_worktrees import _GitRepositoryTestCase, sh


class PublicationTests(_GitRepositoryTestCase):
    def test_manual_approval_records_exact_patch_and_reason(self):
        run = agentic.create_worktree(self.repo, 'main', 19)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(19, 'Manual completion', 'Resolve the issue')
        approval = agentic.record_manual_approval(
            run, issue, 'main', {'max_files': 10}, folder,
            'Human reviewed; the remaining finding is outside this issue',
        )
        loaded = agentic.load_manual_approval(folder)
        self.assertEqual(loaded, approval)
        self.assertEqual(approval['status'], 'approved')
        self.assertEqual(approval['branch'], run.branch)
        self.assertEqual(len(approval['patch_digest']), 64)
        self.assertIn('outside this issue', approval['reason'])
        defaulted = agentic.record_manual_approval(
            run, issue, 'main', {'max_files': 10}, folder, '  ',
        )
        self.assertEqual(defaulted['reason'], 'Manually marked done by user')


    def test_manual_publish_rejects_patch_changed_after_mark_done(self):
        run = agentic.create_worktree(self.repo, 'main', 20)
        (run.worktree / 'fix.py').write_text('before\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(20, 'Changed manual completion', 'Resolve the issue')
        config = {
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
            'max_files': 10,
        }
        approval = agentic.record_manual_approval(
            run, issue, 'main', config, folder, 'Human approved the current patch',
        )
        (run.worktree / 'fix.py').write_text('after\n')
        with self.assertRaisesRegex(RuntimeError, 'Patch changed since approval'):
            agentic.validate_publication_approval(
                run, issue, 'main', config, approval,
            )


    def test_validate_publication_approval_keeps_patch_summary_plain(self):
        run = agentic.create_worktree(self.repo, 'main', 21)
        issue = agentic.Issue(21, 'Plain publication summary', '')
        config = {'max_files': 10}
        approval = {
            'branch': run.branch,
            'issue_digest': agentic.issue_fingerprint(issue),
            'base_sha': 'base-sha',
            'patch_digest': 'digest',
        }
        summary = (
            ' fix.py | 2 ++\n'
            ' 1 file changed, 2 insertions(+)\n'
        )
        console = StringIO()
        with (patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}),
              patch('sys.stdout', console),
              patch('agentic.prepare_patch', return_value=('base-sha', 'digest', summary))):
            result = agentic.validate_publication_approval(
                run, issue, 'main', config, approval,
            )

        self.assertEqual(result, ('base-sha', 'digest', summary))
        self.assertEqual(console.getvalue(), '[PATCH] Proposed changes:\n' + summary + '\n')
        self.assertNotIn('\033[', console.getvalue())


    def test_blocked_change_from_previous_commit_cannot_publish(self):
        run = agentic.create_worktree(self.repo, 'main', 13)
        (run.worktree / 'AGENTS.md').write_text('unexpected instructions\n')
        sh('git', 'add', '-A', cwd=run.worktree)
        sh('git', 'commit', '-m', 'bad', cwd=run.worktree)
        with self.assertRaisesRegex(RuntimeError, 'Forbidden'):
            agentic.prepare_patch(run.worktree, 'main', 10)


    def test_publish_uses_existing_approval_and_confirmation(self):
        run = agentic.create_worktree(self.repo, 'main', 15)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        issue = agentic.Issue(15, 'Fix feature', '')
        folder = agentic.logs_path(self.repo, run)
        cfg = {'test_commands': [[sys.executable, '-c', 'print("ok")']], 'max_retries': 1, 'max_files': 10}
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            cfg, folder,
        )
        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.verify_and_review') as verify,
              patch('agentic.run_tests') as run_tests,
              patch('agentic_shell_input._interactive_line', return_value='no') as confirm,
              patch('agentic.command', wraps=agentic.command) as mock_command):
            agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)
            confirm.assert_called_once_with(
                "\n[PUBLISH] Publish this patch? [y/N]: ",
                system_prefix='PUBLISH',
            )
            verify.assert_not_called()
            run_tests.assert_not_called()
            self.assertFalse(any(c.args[0][:2] == ['gh', 'pr'] for c in mock_command.call_args_list))
            self.assertFalse(any(c.args[0][:3] == ['git', 'push', '-u'] for c in mock_command.call_args_list))
        self.assertIn('fix.py', agentic.git(run.worktree, 'diff', '--cached', '--name-only'))


    def test_publish_retries_invalid_confirmation_and_allows_q_cancellation(self):
        run = agentic.create_worktree(self.repo, 'main', 151)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(151, 'Fix feature', '')
        cfg = {
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
            'max_retries': 1,
            'max_files': 10,
        }
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            cfg, folder,
        )
        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic_shell_input._interactive_line', side_effect=['later', 'q']) as confirm,
              patch('agentic.command', wraps=agentic.command) as mock_command,
              patch('agentic._status_print') as status):
            agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)

        self.assertEqual(confirm.call_count, 2)
        self.assertTrue(any('Invalid confirmation' in call.args[0] for call in status.call_args_list))
        self.assertFalse(any(c.args[0][:3] == ['git', 'push', '-u'] for c in mock_command.call_args_list))


    def test_publish_empty_and_n_confirmations_cancel_without_publishing(self):
        for issue_number, response in ((153, ''), (154, 'n')):
            with self.subTest(response=response):
                run = agentic.create_worktree(self.repo, 'main', issue_number)
                (run.worktree / 'fix.py').write_text('fixed=True\n')
                folder = agentic.logs_path(self.repo, run)
                issue = agentic.Issue(issue_number, 'Fix feature', '')
                cfg = {
                    'test_commands': [[sys.executable, '-c', 'print("ok")']],
                    'max_retries': 1,
                    'max_files': 10,
                }
                base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
                agentic.record_review_approval(
                    run, issue, 'main', base_sha, digest,
                    {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
                    cfg, folder,
                )
                with (patch('agentic.check_existing_pr', return_value=[]),
                      patch('agentic_shell_input._interactive_line', return_value=response),
                      patch('agentic.command', wraps=agentic.command) as mock_command):
                    agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)
                self.assertFalse(any(
                    call_item.args[0][:3] == ['git', 'push', '-u']
                    for call_item in mock_command.call_args_list
                ))


    def test_publish_ctrl_c_reports_cancellation_without_publishing(self):
        run = agentic.create_worktree(self.repo, 'main', 152)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(152, 'Fix feature', '')
        cfg = {'test_commands': [['missing-test-command']], 'max_files': 10}
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            cfg, folder,
        )
        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic_shell_input._interactive_line', side_effect=KeyboardInterrupt),
              patch('agentic._status_print') as status):
            agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)

        self.assertTrue(any('cancelled by Ctrl-C' in call.args[0] for call in status.call_args_list))


    def test_publish_requires_existing_approval(self):
        run = agentic.create_worktree(self.repo, 'main', 15)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(15, 'Fix feature', '')
        config = {'test_commands': [['missing-test-command']], 'max_files': 10}
        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.verify_and_review') as verify,
              patch('agentic.run_tests') as run_tests,
              patch('agentic._status_print') as status):
            with self.assertRaisesRegex(RuntimeError, 'No approval recorded'):
                agentic.publish(run, issue, 'fake/repo', 'main', config, folder)
        verify.assert_not_called()
        run_tests.assert_not_called()
        self.assertTrue(any(
            message.startswith('[PUBLISH] Failed: No approval recorded')
            for message in (call.args[0] for call in status.call_args_list)
        ))


    def test_pub_blocks_mutation_after_approval(self):
        run = agentic.create_worktree(self.repo, 'main', 16)
        issue = agentic.Issue(16, 'Fix feature', '')
        folder = agentic.logs_path(self.repo, run)
        (run.worktree / 'fix.py').write_text('before\n')
        cfg = {'test_commands': [[sys.executable, '-c', 'print("ok")']], 'max_retries': 1, 'max_files': 10}
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            cfg, folder,
        )

        def mutate_on_confirm(*args):
            (run.worktree / 'fix.py').write_text('after\n')
            return 'y'

        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('builtins.input', side_effect=mutate_on_confirm)):
            with self.assertRaisesRegex(RuntimeError, 'Patch changed'):
                agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)
        self.assertEqual(agentic.git(run.worktree, 'log', '-1', '--format=%s'), 'initial')


    def test_publish_accepts_legacy_alias_and_opens_draft_pr(self):
        run = agentic.create_worktree(self.repo, 'main', 30)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(30, 'Publish fix', 'Make the fix')
        config = {
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
            'max_files': None,
            'reviewer_agent': {'provider': 'gemini'},
        }
        agentic.record_review_approval(
            run, issue, 'main', 'base-sha', 'digest',
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            config, folder,
        )
        agentic.next_log(folder, 'tests').write_text(
            '\x1b[32mTest Files 2 passed (2)\x1b[0m\n'
            '\x1b[32mTests 4 passed <img src=x> `[details]`\x1b[0m',
            encoding='utf-8',
        )
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if argv[:4] == ['git', 'diff', '--cached', '--quiet']:
                return CompletedProcess(argv, 1, stdout='')
            return CompletedProcess(argv, 0, stdout='https://github.test/pr/30')

        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.prepare_patch', return_value=('base-sha', 'digest', 'summary')),
              patch('agentic.git') as git_mock,
              patch('agentic.command', side_effect=fake_command),
              patch('builtins.input', return_value='publish'),
              patch('agentic._status_print') as status):
            agentic.publish(run, issue, 'owner/repo', 'main', config, folder)
        self.assertTrue(any(
            message.startswith('[PUBLISH] Completed: Draft PR:')
            for message in (call.args[0] for call in status.call_args_list)
        ))
        self.assertIn(call(run.worktree, 'commit', '-m', 'fix: resolve issue #30'), git_mock.call_args_list)
        self.assertIn(call(run.worktree, 'push', '-u', 'origin', run.branch), git_mock.call_args_list)
        pr_command = next(argv for argv in commands if argv[:3] == ['gh', 'pr', 'create'])
        self.assertIn('--draft', pr_command)
        self.assertIn('--base', pr_command)
        self.assertIn('main', pr_command)
        self.assertIn('Independent Gemini review', pr_command[-1])
        self.assertIn(
            '- <code>&lt;configured test command: 3 argv values&gt;</code> — passed',
            pr_command[-1],
        )
        self.assertIn('Tests 4 passed &lt;img src=x&gt; &#96;[details]&#96;', pr_command[-1])
        self.assertNotIn('\x1b', pr_command[-1])
        self.assertNotIn('```text', pr_command[-1])


    def test_publish_reports_failure_when_publication_command_raises(self):
        run = agentic.create_worktree(self.repo, 'main', 31)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(31, 'Publish failure', 'Make the fix')
        config = {'test_commands': [[sys.executable, '-c', 'print("ok")']], 'max_files': 10}
        agentic.record_review_approval(
            run, issue, 'main', 'base-sha', 'digest',
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            config, folder,
        )

        def fail_on_push(_repo, *args, **_kwargs):
            if args[:2] == ('push', '-u'):
                raise RuntimeError('remote rejected the push')
            return ''

        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.prepare_patch', return_value=('base-sha', 'digest', 'summary')),
              patch('agentic.git', side_effect=fail_on_push),
              patch('builtins.input', return_value='yes'),
              patch('agentic._status_print') as status):
            with self.assertRaisesRegex(RuntimeError, 'remote rejected the push'):
                agentic.publish(run, issue, 'owner/repo', 'main', config, folder)

        self.assertTrue(any(
            message.startswith('[PUBLISH] Failed: remote rejected the push')
            for message in (call.args[0] for call in status.call_args_list)
        ))


    def test_publish_uses_manual_approval_without_repeating_reviewer(self):
        run = agentic.create_worktree(self.repo, 'main', 33)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(33, 'Manual publish', 'Make the fix')
        config = {
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
            'max_files': 10,
        }
        approval = agentic.record_manual_approval(
            run, issue, 'main', config, folder, 'Human accepted the remaining scope decision',
        )
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if argv[:4] == ['git', 'diff', '--cached', '--quiet']:
                return CompletedProcess(argv, 1, stdout='')
            return CompletedProcess(argv, 0, stdout='https://github.test/pr/33')

        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.verify_and_review') as automated_verify,
              patch('agentic.run_tests') as run_tests,
              patch('agentic.prepare_patch', return_value=(
                  approval['base_sha'], approval['patch_digest'], 'summary',
              )),
              patch('agentic.git'),
              patch('agentic.command', side_effect=fake_command),
              patch('builtins.input', return_value='y')):
            agentic.publish(
                run, issue, 'owner/repo', 'main', config, folder,
                manual_approval=approval,
            )
        automated_verify.assert_not_called()
        run_tests.assert_not_called()
        pr_command = next(argv for argv in commands if argv[:3] == ['gh', 'pr', 'create'])
        self.assertIn('## Review status', pr_command[-1])
        self.assertIn('Automated review was not run during publication', pr_command[-1])
        self.assertIn('Configured checks were not run during publication', pr_command[-1])
        self.assertNotIn('All configured checks passed', pr_command[-1])


    def test_publish_skips_existing_pr_and_rejects_closed_issue(self):
        run = agentic.create_worktree(self.repo, 'main', 35)
        folder = agentic.logs_path(self.repo, run)
        config = {'test_commands': [['go', 'test', './...']], 'max_files': None}
        existing = {'url': 'https://github.test/pr/35'}
        with (patch('agentic.check_existing_pr', return_value=[existing]) as check,
              patch('agentic.verify_and_review') as verify):
            agentic.publish(run, agentic.Issue(35, 'Existing', ''), 'owner/repo', 'main', config, folder)
        check.assert_called_once()
        verify.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            agentic.publish(
                run, agentic.Issue(35, 'Closed', '', state='CLOSED'),
                'owner/repo', 'main', config, folder,
            )

    def test_pr_repair_pushes_existing_branch_without_creating_a_pr(self):
        run = agentic.create_worktree(self.repo, 'main', 36)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(36, 'Repair', 'Fix CI')
        config = {'test_commands': [['python', '-c', 'pass']], 'max_files': 10}
        agentic.record_review_approval(
            run, issue, 'main', 'base-sha', 'patch-digest',
            {'approved': True, 'summary': 'Fresh review', 'findings': [], 'skipped': []},
            config, folder,
        )
        pull_request = agentic.PullRequest(
            123, 'Repair', 'Fix CI', 'OPEN', 'main', 'base-sha', run.branch,
            'head-sha', 'owner/repo', 'https://github.test/pull/123',
        )
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if argv[:4] == ['git', 'diff', '--cached', '--quiet']:
                return CompletedProcess(argv, 1, stdout='')
            return CompletedProcess(argv, 0, stdout='')

        with (patch('agentic.prepare_patch', return_value=('base-sha', 'patch-digest', 'summary')),
              patch('agentic.git') as git_mock,
              patch('agentic.command', side_effect=fake_command),
              patch('agentic._status_print')):
            agentic.push_existing_pr(
                run, issue, 'owner/repo', 'main', config, folder, pull_request,
                fetch_pr=lambda: pull_request,
            )
        self.assertIn(call(run.worktree, 'push', '-u', 'origin', run.branch), git_mock.call_args_list)
        self.assertFalse(any(argv[:3] == ['gh', 'pr', 'create'] for argv in commands))
