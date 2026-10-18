"""Offline tests: no GitHub or Codex credentials needed."""
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
        agent_results = [(True, 'first'), (True, 'second')]
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
              patch('agentic.verify_and_review', side_effect=review_results)):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        self.assertEqual(run_agent.call_count, 2)
        second_prompt = run_agent.call_args_list[1].args[1]
        self.assertIn('Missing regression test', second_prompt)
        self.assertIn('Add a regression test', second_prompt)

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
        self.assertIsNone(args.sandbox_image)

        defaults = agentic.build_parser().parse_args(['init'])
        self.assertEqual(defaults.security_mode, 'trusted')

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
                'Findings and recommended next steps:\n'
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

    def test_openrouter_requires_model_and_never_reads_key_from_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'developer': {'provider': 'openrouter'},
            }))
            with self.assertRaisesRegex(ValueError, 'developer.model'):
                agentic.load_config(path)

    def test_cli_provider_uses_headless_structured_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'developer-001.txt'

            def fake_stream(argv, cwd, **kwargs):
                kwargs['log_file'].with_suffix('.jsonl').write_text(
                    json.dumps({'type': 'result', 'response': 'finished'}) + '\n'
                )
                return CompletedProcess(argv, 0, stdout='')

            with patch('agentic.streamed_command', side_effect=fake_stream) as streamed:
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

            with patch('agentic.streamed_command', side_effect=fake_stream) as streamed:
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
        self.assertEqual(agentic._extract_provider_message(raw, 'claude'), 'final')
        self.assertEqual(agentic._extract_provider_message('copilot output\n', 'copilot'), 'copilot output')
        self.assertIn('Using tool: read_file', agentic._provider_progress(
            json.dumps({'type': 'tool_use', 'tool': {'name': 'read_file'}}), 'gemini'
        ))
        self.assertIn('ERROR:', agentic._provider_progress(
            json.dumps({'type': 'turn.failed', 'message': 'failed'}), 'claude'
        ))

    def test_cli_provider_failure_is_returned_and_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def fake_stream(argv, cwd, **kwargs):
                return CompletedProcess(argv, 7, stdout='provider failed')

            with patch('agentic.streamed_command', side_effect=fake_stream):
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

    def test_openrouter_tool_loop_can_write_only_inside_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'developer-001.txt'
            responses = iter([
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': 'hello.txt', 'content': 'hello\n'}),
                    }}],
                }}]},
                {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]},
            ])

            class Response:
                def __init__(self, value):
                    self.value = value
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self):
                    return json.dumps(self.value).encode()

            def fake_urlopen(request, *args, **kwargs):
                return Response(next(responses))

            with (patch.dict(agentic.os.environ, {'MY_OPENROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic.urllib.request.urlopen', side_effect=fake_urlopen)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=output, model='openai/gpt-5.4',
                    api_key_env='MY_OPENROUTER_KEY',
                )
            self.assertTrue(ok)
            self.assertEqual(message, 'done')
            self.assertEqual((root / 'hello.txt').read_text(), 'hello\n')
            self.assertNotIn('secret', output.with_suffix('.log').read_text())
            with self.assertRaisesRegex(ValueError, 'worktree'):
                agentic._safe_worktree_path(root, '../outside')

    def test_openrouter_tools_cover_readonly_boundaries_and_file_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'src').mkdir()
            (root / 'src' / 'main.go').write_text('package main\n')
            (root / '.git').mkdir()

            names = [item['function']['name'] for item in agentic._openrouter_tools(True)]
            self.assertEqual(names, ['list_files', 'read_file', 'run_command'])
            self.assertIn('src/main.go', agentic._openrouter_tool_call(
                root, 'list_files', {'path': '.'}, readonly=True
            ))
            self.assertEqual(
                agentic._openrouter_tool_call(root, 'read_file', {'path': 'src/main.go'}, readonly=True),
                'package main\n',
            )
            self.assertIn('ERROR: write_file is unavailable', agentic._openrouter_tool_call(
                root, 'write_file', {'path': 'new.txt', 'content': 'nope'}, readonly=True
            ))
            self.assertFalse((root / 'new.txt').exists())
            self.assertIn('exit 0', agentic._openrouter_tool_call(
                root, 'run_command', {'argv': ['pwd']}, readonly=False
            ))
            self.assertIn('ERROR:', agentic._openrouter_tool_call(
                root, 'list_files', {'path': 'src/main.go'}, readonly=True
            ))
            self.assertIn('ERROR: protected file', agentic._openrouter_tool_call(
                root, 'write_file', {'path': '.env', 'content': 'secret'}, readonly=False
            ))
            (root / '.env').write_text('PRIVATE=do-not-send\n')
            self.assertIn('ERROR: protected file', agentic._openrouter_tool_call(
                root, 'read_file', {'path': '.env'}, readonly=False
            ))
            self.assertNotIn('.env', agentic._openrouter_tool_call(
                root, 'list_files', {'path': '.'}, readonly=False
            ))
            self.assertIn('ERROR: unknown tool', agentic._openrouter_tool_call(
                root, 'unknown', {}, readonly=False
            ))

    def test_openrouter_command_policy_rejects_shell_network_and_review_mutations(self):
        for argv in ([], ['bash', '-c', 'echo unsafe'], ['curl', 'https://example.test'],
                     ['git', 'push'], ['git', 'checkout', 'main'],
                     ['git', '-C', '../other', 'status'],
                     ['git', '-c', 'core.sshCommand=ssh', 'status'],
                     ['python', '-c', 'print(1)'],
                     ['node', '--eval', 'process.exit(1)']):
            allowed, reason = agentic._openrouter_command_allowed(argv, readonly=False)
            self.assertFalse(allowed, argv)
            self.assertTrue(reason)

        allowed, reason = agentic._openrouter_command_allowed(
            ['curl', 'https://example.test'], readonly=False, allow_network=True
        )
        self.assertTrue(allowed, reason)
        for argv in (['git', 'status'], ['git', 'diff'], ['rg', 'needle', '.']):
            allowed, reason = agentic._openrouter_command_allowed(argv, readonly=True)
            self.assertTrue(allowed, reason)
        for argv in (['python', '-c', 'print(1)'], ['git', 'fetch']):
            allowed, reason = agentic._openrouter_command_allowed(argv, readonly=True)
            self.assertFalse(allowed, argv)
            self.assertTrue(reason)

        allowed, reason = agentic._openrouter_command_allowed(
            ['python', '-m', 'unittest'], readonly=False
        )
        self.assertTrue(allowed, reason)

    def test_openrouter_commands_use_configured_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = {
                'enabled': True, 'engine': 'docker', 'image': 'test-image',
                'allow_network': False, 'pass_env': ['OPENAI_API_KEY'],
            }
            with (patch.dict(agentic.os.environ, {'OPENAI_API_KEY': 'secret'}, clear=True),
                  patch('agentic.subprocess.run', return_value=CompletedProcess(
                      ['docker'], 0, stdout='sandboxed\n'
                  )) as run):
                result = agentic._openrouter_tool_call(
                    root, 'run_command', {'argv': ['python', '-m', 'unittest']},
                    readonly=False, sandbox=settings,
                )
            self.assertIn('sandboxed', result)
            process_argv = run.call_args.args[0]
            self.assertEqual(process_argv[:2], ['docker', 'run'])
            self.assertIn('--network', process_argv)
            self.assertEqual(process_argv[process_argv.index('--network') + 1], 'none')
            self.assertNotIn('OPENAI_API_KEY', process_argv)
            self.assertNotIn('secret', process_argv)

    def test_openrouter_request_translates_http_network_and_payload_errors(self):
        from io import BytesIO

        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.body

        cases = [
            (agentic.urllib.error.HTTPError(
                'https://router.test', 401, 'unauthorized', {}, BytesIO(b'bad key')
            ), '401'),
            (agentic.urllib.error.URLError('offline'), 'offline'),
            (json.JSONDecodeError('bad json', '!', 0), 'bad json'),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected), patch(
                'agentic.urllib.request.urlopen', side_effect=error
            ):
                with self.assertRaisesRegex(RuntimeError, 'OpenRouter request failed') as raised:
                    agentic._openrouter_request(
                        'https://router.test/chat/completions', 'secret', {}, timeout=2
                    )
                self.assertIn(expected, str(raised.exception))

        for body, message in [(b'[]', 'non-object'), (b'{"error":"quota"}', 'quota')]:
            with self.subTest(message=message), patch(
                'agentic.urllib.request.urlopen', return_value=Response(body)
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    agentic._openrouter_request(
                        'https://router.test/chat/completions', 'secret', {}, timeout=None
                    )

    def test_openrouter_tool_turn_limit_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'developer.txt'
            response = {'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'call', 'function': {
                    'name': 'unknown', 'arguments': '{not-json',
                }}],
            }}]}
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch.object(agentic, 'OPENROUTER_MAX_TURNS', 2),
                  patch('agentic._openrouter_request', return_value=response)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=output, model='openai/test',
                    api_key_env='ROUTER_KEY', timeout=None,
                )
            self.assertFalse(ok)
            self.assertIn('exceeded 2 tool turns', message)
            self.assertIn('exceeded 2 tool turns', output.read_text())

    def test_openrouter_rejects_symlink_escape_and_invalid_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worktree'
            outside = Path(temp) / 'outside.txt'
            root.mkdir()
            outside.write_text('private')
            link = root / 'outside-link'
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                agentic._safe_worktree_path(root, 'outside-link')
            with self.assertRaisesRegex(ValueError, 'absolute'):
                agentic._safe_worktree_path(root, str(outside))
            with self.assertRaisesRegex(ValueError, 'non-empty'):
                agentic._safe_worktree_path(root, '')
            with self.assertRaisesRegex(ValueError, '\\.git'):
                agentic._safe_worktree_path(root, '.git/config', write=True)

    def test_openrouter_tool_command_timeout_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('agentic.subprocess.run', side_effect=subprocess.TimeoutExpired('python', 1)):
                result = agentic._openrouter_tool_call(
                    Path(temp), 'run_command',
                    {'argv': [sys.executable, '-m', 'unittest']},
                    readonly=False, command_timeout=1,
                )
        self.assertIn('timed out after 1s', result)

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
                    'allow_network': True,
                },
            }))
            config = agentic.load_config(path)
        settings = config['developer_agent']
        self.assertEqual(settings['max_tool_turns'], 12)
        self.assertEqual(settings['command_timeout_seconds'], 7)
        self.assertTrue(settings['allow_network'])

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

            def fake_probe(argv, cwd, **kwargs):
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

            def fake_probe(argv, cwd, **kwargs):
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
        self.assertEqual(config['task_source'], 'github')
        self.assertTrue(agentic.forbidden('.env'))
        self.assertFalse(agentic.forbidden('src/main.py'))

        with tempfile.TemporaryDirectory() as temp:
            unlimited = Path(temp) / 'agent_config.json'
            unlimited.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'max_files': None,
            }))
            self.assertIsNone(agentic.load_config(unlimited)['max_files'])

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

    def test_test_runner_reports_command_and_diff_check_failures(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            sh('git', 'init', cwd=root)
            failed, report = agentic.run_tests(
                root, [[sys.executable, '-c', 'print("bad"); raise SystemExit(2)']]
            )
            self.assertFalse(failed)
            self.assertIn('bad', report)

            (root / 'tracked.txt').write_text('clean\n')
            sh('git', 'add', 'tracked.txt', cwd=root)
            sh('git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
               'commit', '-m', 'tracked', cwd=root)
            (root / 'tracked.txt').write_text('trailing whitespace  \n')
            passed, report = agentic.run_tests(
                root, [[sys.executable, '-c', 'print("ok")']]
            )
            self.assertFalse(passed)
            self.assertIn('git diff --check failed', report)

    def test_test_runner_stops_when_deadline_expires_between_commands(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            sh('git', 'init', cwd=root)
            with patch('agentic.streamed_command', return_value=CompletedProcess([], 0, stdout='ok')) as streamed, \
                 patch('agentic.time.monotonic', side_effect=[100, 100, 102]):
                passed, report = agentic.run_tests(
                    root,
                    [[sys.executable, '-c', 'print("one")'], [sys.executable, '-c', 'print("two")']],
                    timeout=300,
                    deadline=101,
                )
            self.assertFalse(passed)
            self.assertIn('deadline', report.lower())
            streamed.assert_called_once()

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
