"""Git worktree lifecycle and resume integration tests."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

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

    def test_status_json_reports_worktree_without_parsing_terminal_output(self):
        run = agentic.create_worktree(self.repo, 'main', 44)
        from io import StringIO
        with patch('sys.stdout', new_callable=StringIO) as output:
            agentic.show_runs(self.repo, json_output=True)
        records = json.loads(output.getvalue())
        self.assertEqual(records[0]['issue'], 44)
        self.assertEqual(records[0]['branch'], run.branch)
        self.assertFalse(records[0]['dirty'])

    def test_local_task_worktree_has_deterministic_identity(self):
        task = agentic.Issue.from_local_text('Add CSV export\nInclude regression tests.')
        run = agentic.create_local_worktree(self.repo, 'main', task)
        found = agentic.runs_for_task(self.repo, task.task_key)
        self.assertEqual(found, [run])
        self.assertIsNone(run.issue_number)
        self.assertIn('agent/task-add-csv-export-', run.branch)
        self.assertEqual(task.reference, f'LOCAL TASK {task.task_key[:8]}')

    def test_central_state_dir_keeps_runtime_artifacts_outside_repository(self):
        state_dir = self.repo.parent / 'agentic-state'
        run = agentic.create_worktree(self.repo, 'main', 43, state_dir)
        canonical_state_dir = state_dir.resolve()
        self.assertTrue(run.worktree.is_relative_to(canonical_state_dir))
        folder = agentic.logs_path(self.repo, run, state_dir)
        self.assertTrue(folder.is_relative_to(canonical_state_dir))
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
        with self.assertRaisesRegex(RuntimeError, 'Patch changed since --mark-done'):
            agentic.verify_manual_approval(
                run, issue, 'main', config, folder, approval,
            )

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
                output.write_text(json.dumps({
                    'approved': True, 'summary': 'Looks good', 'findings': [],
                    'skipped': ['Browser check unavailable in this environment'],
                }))
            else:
                output.write_text('Made implementation changes.')
            return CompletedProcess(argv, 0, stdout='')

        with patch('agentic_execution.streamed_command', side_effect=fake_stream):
            agentic.run_codex(run.worktree, 'Implement it', output=folder / 'developer-001.txt')
            verdict = agentic.review(
                run.worktree, issue, folder, base, test_report='pytest passed',
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0:3], ['codex', 'exec', '--json'])
        self.assertEqual(calls[0][3:5], ['--sandbox', 'workspace-write'])
        self.assertEqual(calls[1][3:5], ['--sandbox', 'read-only'])
        self.assertEqual(verdict['approved'], True)
        self.assertEqual(verdict['skipped'], ['Browser check unavailable in this environment'])
        self.assertEqual(len(list(folder.glob('review-*.json'))), 2)  # review-schema + review-001
        self.assertIn('git diff --cached', calls[1][-1])
        self.assertIn('description', calls[1][-1])
        self.assertIn('pytest passed', calls[1][-1])
        self.assertIn('Made implementation changes.', calls[1][-1])
        self.assertIn('Use a blocking approval threshold', calls[1][-1])
        self.assertIn('the issue requirements are the scope boundary', calls[1][-1])
        self.assertIn('Omit unrelated concerns rather than placing them in findings', calls[1][-1])
        self.assertIn('Do not reject for style preferences', calls[1][-1])
        self.assertIn('if no blocker remains, set approved to true', calls[1][-1])

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

    def test_publish_commits_pushes_and_opens_draft_pr_after_confirmation(self):
        run = agentic.create_worktree(self.repo, 'main', 30)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(30, 'Publish fix', 'Make the fix')
        config = {
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
            'max_files': None,
            'reviewer_agent': {'provider': 'gemini'},
        }
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if argv[:4] == ['git', 'diff', '--cached', '--quiet']:
                return CompletedProcess(argv, 1, stdout='')
            return CompletedProcess(argv, 0, stdout='https://github.test/pr/30')

        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic.verify_and_review', return_value=(
                  True, 'tests passed',
                  {'approved': True, 'summary': 'Good', 'findings': []},
                  'digest', 'base-sha',
              )),
              patch('agentic.prepare_patch', return_value=('base-sha', 'digest', 'summary')),
              patch('agentic.git') as git_mock,
              patch('agentic.command', side_effect=fake_command),
              patch('builtins.input', return_value='publish')):
            agentic.publish(run, issue, 'owner/repo', 'main', config, folder)
        self.assertIn(call(run.worktree, 'commit', '-m', 'fix: resolve issue #30'), git_mock.call_args_list)
        self.assertIn(call(run.worktree, 'push', '-u', 'origin', run.branch), git_mock.call_args_list)
        pr_command = next(argv for argv in commands if argv[:3] == ['gh', 'pr', 'create'])
        self.assertIn('--draft', pr_command)
        self.assertIn('--base', pr_command)
        self.assertIn('main', pr_command)
        self.assertIn('Independent Gemini review', pr_command[-1])

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
              patch('agentic.verify_manual_approval', return_value=(
                  True, 'tests passed',
                  {'approved': True, 'summary': 'Manual', 'findings': [], 'manual': True},
                  approval['patch_digest'], approval['base_sha'],
              )) as manual_verify,
              patch('agentic.verify_and_review') as automated_verify,
              patch('agentic.prepare_patch', return_value=(
                  approval['base_sha'], approval['patch_digest'], 'summary',
              )),
              patch('agentic.git'),
              patch('agentic.command', side_effect=fake_command),
              patch('builtins.input', return_value='publish')):
            agentic.publish(
                run, issue, 'owner/repo', 'main', config, folder,
                manual_approval=approval,
            )
        manual_verify.assert_called_once()
        automated_verify.assert_not_called()
        pr_command = next(argv for argv in commands if argv[:3] == ['gh', 'pr', 'create'])
        self.assertIn('Manual completion approval', pr_command[-1])

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

    def test_verify_and_review_retries_invalid_reviewer_output(self):
        run = agentic.create_worktree(self.repo, 'main', 31)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(31, 'Validate review retry', '')
        config = {
            'test_commands': [['go', 'test', './...']],
            'max_files': None,
            'reviewer_agent': {'provider': 'claude'},
        }
        patch_value = ('base-sha', 'digest', 'one file changed')
        invalid = {'_tooling_error': True, 'approved': False, 'summary': 'invalid', 'findings': []}
        approved = {'approved': True, 'summary': 'Good', 'findings': []}
        with (patch('agentic.run_tests', return_value=(True, 'tests passed')),
              patch('agentic.prepare_patch', side_effect=[patch_value, patch_value]),
              patch('agentic.review', side_effect=[invalid, approved]) as review,
              patch('agentic.record_review_findings') as record):
            result = agentic.verify_and_review(run, issue, 'main', config, folder)
        self.assertTrue(result[0])
        self.assertEqual(review.call_count, 2)
        self.assertEqual(review.call_args.kwargs['agent'], config['reviewer_agent'])
        record.assert_called_once_with(folder, approved)

    def test_verify_and_review_stops_before_review_when_tests_fail(self):
        run = agentic.create_worktree(self.repo, 'main', 32)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(32, 'Tests fail', '')
        config = {'test_commands': [['go', 'test', './...']], 'max_files': None}
        with (patch('agentic.run_tests', return_value=(False, 'AssertionError: expected 2')),
              patch('agentic.prepare_patch') as prepare,
              patch('agentic.review') as review):
            passed, report, verdict, digest, base_sha = agentic.verify_and_review(
                run, issue, 'main', config, folder
            )
        self.assertFalse(passed)
        self.assertIn('AssertionError', report)
        self.assertEqual(verdict, {})
        self.assertEqual(digest, '')
        self.assertEqual(base_sha, '')
        prepare.assert_not_called()
        review.assert_not_called()

    def test_develop_returns_review_feedback_to_the_next_attempt(self):
        run = agentic.create_worktree(self.repo, 'main', 33)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(33, 'Retry review feedback', 'Implement it')
        config = {
            'max_retries': 1,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        agent_results = [
            (True, 'first'),
            (True, 'second\nReview finding actions:\n'
                   '- Finding: Add a regression test\n'
                   '  Action: fixed\n'
                   '  Evidence: added regression coverage'),
        ]
        review_results = [
            (False, 'tests passed', {
                'approved': False, 'summary': 'Missing regression test',
                'findings': ['Add a regression test'],
            }, '', ''),
            (True, 'tests passed', {
                'approved': True, 'summary': 'Good', 'findings': [],
            }, '', ''),
        ]
        with (patch('agentic.run_agent', side_effect=agent_results) as run_agent,
              patch('agentic.verify_and_review', side_effect=review_results) as verify):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        self.assertEqual(run_agent.call_count, 2)
        self.assertEqual(verify.call_args_list[0].kwargs['developer_report'], 'first')
        self.assertIn('Review finding actions:', verify.call_args_list[1].kwargs['developer_report'])
        second_prompt = run_agent.call_args_list[1].args[1]
        self.assertIn('first', second_prompt)
        self.assertIn('Missing regression test', second_prompt)
        self.assertIn('Add a regression test', second_prompt)
        self.assertIn('one list entry for every finding', second_prompt)
        self.assertIn('prior\n  review', second_prompt)
        self.assertIn('Finding:', second_prompt)
        self.assertIn('Action:', second_prompt)
        self.assertIn('Evidence:', second_prompt)

    def test_develop_does_not_retry_environment_failures(self):
        run = agentic.create_worktree(self.repo, 'main', 34)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(34, 'Environment failure', '')
        config = {'max_retries': 3, 'max_files': None, 'developer_agent': {'provider': 'codex'}}
        with (patch('agentic.run_agent', return_value=(True, 'implemented')) as run_agent,
              patch('agentic.verify_and_review', return_value=(
                  False, 'No module named pytest', {}, '', ''
              ))):
            with self.assertRaisesRegex(RuntimeError, 'environment or infrastructure'):
                agentic.develop(run, issue, 'main', config, folder)
        run_agent.assert_called_once()

    def test_develop_blocks_repeated_review_without_patch_change(self):
        run = agentic.create_worktree(self.repo, 'main', 35)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(35, 'Repeated review blocker', '')
        config = {
            'max_retries': 3,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        developer_reports = [
            'implemented',
            'implemented\nReview finding actions:\n'
            '- Finding: Wire the production adapter\n'
            '  Action: blocked\n'
            '  Evidence: adapter is unavailable in this slice',
        ]
        review = (
            False, 'tests passed', {
                'approved': False,
                'summary': 'Production adapter is unavailable',
                'findings': ['Wire the production adapter'],
            }, 'same-digest', 'base',
        )
        with (patch('agentic.run_agent', side_effect=[
                  (True, developer_reports[0]), (True, developer_reports[1]),
              ]) as run_agent,
              patch('agentic.verify_and_review', return_value=review)):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        self.assertEqual(run_agent.call_count, 2)
        blocked = json.loads((folder / 'blocked.json').read_text())
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['patch_digest'], 'same-digest')

        with (patch('agentic.prepare_patch', return_value=('base', 'same-digest', 'summary')),
              patch('agentic.run_agent') as resumed_agent):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        resumed_agent.assert_not_called()

    def test_develop_blocks_repeated_finding_after_patch_change(self):
        run = agentic.create_worktree(self.repo, 'main', 36)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(36, 'Repeated finding after edit', '')
        config = {
            'max_retries': 3,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        reports = [
            'implemented',
            'implemented\nReview finding actions:\n'
            '- Finding: Wire the production adapter\n'
            '  Action: skipped\n'
            '  Evidence: only a cosmetic edit was possible',
        ]
        reviews = [
            (False, 'tests passed', {
                'approved': False,
                'summary': 'Production adapter is unavailable',
                'findings': ['Wire the production adapter'],
            }, 'digest-one', 'base'),
            (False, 'tests passed', {
                'approved': False,
                'summary': 'Production adapter is unavailable',
                'findings': ['Wire the production adapter'],
                '_repeated_findings': [{'fingerprint': 'same'}],
            }, 'digest-two', 'base'),
        ]
        with (patch('agentic.run_agent', side_effect=[
                  (True, reports[0]), (True, reports[1]),
              ]) as run_agent,
              patch('agentic.verify_and_review', side_effect=reviews)):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        self.assertEqual(run_agent.call_count, 2)
        blocked = json.loads((folder / 'blocked.json').read_text())
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['patch_digest'], 'digest-two')

    def test_offline_fake_provider_retries_failed_tests_and_approves_review(self):
        issue = agentic.Issue.from_local_text("Add a generated feature file\n")
        run = agentic.create_local_worktree(self.repo, "main", issue)
        folder = agentic.logs_path(self.repo, run)
        config = {
            "max_retries": 1,
            "max_files": None,
            "developer_agent": {"provider": "fake"},
            "reviewer_agent": {"provider": "fake"},
        }
        test_command = [
            sys.executable, "-c",
            "from pathlib import Path; p=Path('feature.txt'); "
            "assert not p.exists() or p.read_text() == 'good\\n'",
        ]
        calls = []

        def fake_provider(worktree, prompt, *, output, schema=None, readonly=False, **kwargs):
            calls.append((readonly, schema.name if schema else None))
            if schema and schema.name == "investigation-schema.json":
                return True, json.dumps({"summary": "valid", "findings": []})
            if schema and schema.name == "review-schema.json":
                return True, json.dumps({"approved": True, "summary": "approved", "findings": []})
            content = "bad\n" if sum(not item[0] for item in calls) == 1 else "good\n"
            (worktree / "feature.txt").write_text(content, encoding="utf-8")
            output.write_text("fake developer", encoding="utf-8")
            return True, "fake developer"

        with patch("agentic.run_agent", side_effect=fake_provider):
            self.assertTrue(
                agentic.develop(
                    run, issue, "main", config, folder,
                    test_commands=[test_command],
                )
            )

        self.assertEqual((run.worktree / "feature.txt").read_text(), "good\n")
        self.assertEqual(sum(not readonly for readonly, _ in calls), 2)
        self.assertIn((True, "review-schema.json"), calls)

    def test_execute_local_task_dispatches_to_workflow(self):
        config_path = self.repo / 'local-agent-config.json'
        config_path.write_text(json.dumps({
            'task_source': 'local',
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
        }))
        args = agentic.build_parser().parse_args([
            '--repo', str(self.repo), '--config', str(config_path),
            '--state-dir', str(Path(self.tmp.name) / 'agentic-state'),
            '--task', 'Add CSV export\nAdd regression tests.',
        ])
        fake_run = agentic.Run(None, 'agent/task-csv-aabbccddeeff', Path('/tmp/fake-worktree'))
        with (patch('agentic.preflight_test_commands'),
              patch('agentic.preflight_agent_settings'),
              patch('agentic.create_local_worktree', return_value=fake_run) as create,
              patch('agentic._run_investigation', return_value={
                  'summary': 'Valid local task', 'findings': [],
              }),
              patch('agentic.run_tests', return_value=(True, 'ok')),
              patch('agentic.develop', return_value=True) as develop):
            self.assertEqual(agentic.execute_locked(args, self.repo), 0)
        task = create.call_args.args[2]
        self.assertTrue(task.is_local)
        self.assertIn('Add CSV export', task.body)
        self.assertEqual(develop.call_args.args[1], task)
        self.assertIn('Valid local task', develop.call_args.kwargs['initial_feedback'])

    def test_investigation_feedback_does_not_block_development(self):
        config_path = self.repo / 'local-agent-config.json'
        config_path.write_text(json.dumps({
            'task_source': 'local',
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
        }))
        args = agentic.build_parser().parse_args([
            '--repo', str(self.repo), '--config', str(config_path),
            '--state-dir', str(Path(self.tmp.name) / 'agentic-state'),
            '--task', 'Investigate before implementing.',
        ])
        fake_run = agentic.Run(None, 'agent/task-investigate-aabbccddeeff', Path('/tmp/fake-worktree'))
        with (patch('agentic.preflight_test_commands'),
              patch('agentic.preflight_agent_settings'),
              patch('agentic.create_local_worktree', return_value=fake_run),
              patch('agentic._run_investigation', return_value={
                  'summary': 'Not actionable', 'findings': [],
              }),
              patch('agentic.run_tests', return_value=(True, 'ok')),
              patch('agentic.develop', return_value=True) as develop):
            result = agentic.execute_locked(args, self.repo)
        self.assertEqual(result, 0)
        develop.assert_called_once()

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
              patch('agentic._run_investigation', return_value={
                  'summary': 'Valid issue', 'findings': [],
              }),
              patch('agentic.preflight_test_commands'),
              patch('agentic.preflight_agent_settings'),
              patch('agentic.develop', return_value=True) as develop):
            agentic.execute(args)
            create_new.assert_not_called()
            refresh_base.assert_called_once_with(self.repo, 'main')
            self.assertEqual(develop.call_args.args[0], run)
            self.assertIn('Previous review found a missing test', develop.call_args.kwargs['initial_feedback'])
            self.assertTrue((run.worktree / 'partial.py').exists())

