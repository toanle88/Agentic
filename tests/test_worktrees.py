"""Git worktree lifecycle and resume integration tests."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests
from agentic_runner import cli as agentic
from agentic_runner import worktrees as agentic_worktrees


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class _GitRepositoryTestCase(unittest.TestCase):
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


class GitTest(_GitRepositoryTestCase):
    def test_github_remote_matching_uses_configured_enterprise_host(self):
        with patch('agentic_worktrees.git', return_value='ssh://git@github.example.com/owner/repo.git'):
            self.assertEqual(
                agentic_worktrees.github_repo_from_remote(Path('/repo'), 'github.example.com'),
                'owner/repo',
            )
            self.assertIsNone(agentic_worktrees.github_repo_from_remote(Path('/repo'), 'github.com'))

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

    def test_choose_existing_retries_invalid_input_and_confirms_selection(self):
        runs = [
            agentic.Run(42, 'agent/issue-42-aaaabbbb', self.repo / 'one'),
            agentic.Run(42, 'agent/issue-42-ccccdddd', self.repo / 'two'),
        ]
        with (patch('builtins.input', side_effect=['3', '2']),
              patch.object(agentic_worktrees, '_status_print') as status):
            selected = agentic.choose_existing(runs, prompt_prefix='PUBLISH')

        self.assertEqual(selected, runs[1])
        messages = [call.args[0] for call in status.call_args_list]
        self.assertIn(
            "[PUBLISH] Invalid worktree selection. Enter a number from 1 to 2 or 'q' to cancel.",
            messages,
        )
        self.assertIn(
            f"[PUBLISH] Worktree 2 selected: {runs[1].branch} ({runs[1].worktree})",
            messages,
        )

    def test_choose_existing_retries_non_ascii_and_oversized_digits(self):
        runs = [
            agentic.Run(42, 'agent/issue-42-aaaabbbb', self.repo / 'one'),
            agentic.Run(42, 'agent/issue-42-ccccdddd', self.repo / 'two'),
        ]
        with (patch('builtins.input', side_effect=['²', '9' * 5000, '1']),
              patch.object(agentic_worktrees, '_status_print') as status):
            selected = agentic.choose_existing(runs, prompt_prefix='PUBLISH')

        self.assertEqual(selected, runs[0])
        invalid_messages = [
            call.args[0] for call in status.call_args_list
            if 'Invalid worktree selection' in call.args[0]
        ]
        self.assertEqual(len(invalid_messages), 2)

    def test_choose_existing_q_and_ctrl_c_are_cancellation(self):
        run = agentic.Run(42, 'agent/issue-42-aaaabbbb', self.repo / 'one')
        runs = [run, agentic.Run(42, 'agent/issue-42-ccccdddd', self.repo / 'two')]
        for user_input in ('q', KeyboardInterrupt):
            with self.subTest(user_input=user_input), \
                    patch('builtins.input', side_effect=[user_input]), \
                    patch.object(agentic_worktrees, '_status_print'):
                with self.assertRaises(agentic_worktrees.UserCancelled):
                    agentic.choose_existing(runs, prompt_prefix='PUBLISH')


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


    def test_runner_source_allowed_only_for_agentic_self_development(self):
        run = agentic.create_worktree(self.repo, 'main', 10)
        (run.worktree / 'agentic.py').write_text('print("runner change")\n')
        with self.assertRaisesRegex(RuntimeError, 'Sensitive'):
            agentic.prepare_patch(run.worktree, 'main', 10)

        with patch.object(agentic, 'SCRIPT_DIR', self.repo):
            _base, _digest, summary = agentic.prepare_patch(run.worktree, 'main', 10)
        self.assertIn('agentic.py', summary)
        self.assertEqual(agentic.git(run.worktree, 'diff', '--cached', '--name-only'), 'agentic.py')



class ResumeIntegrationTests(_GitRepositoryTestCase):
    def test_resume_reuses_existing_worktree_without_stage_metadata(self):
        run = agentic.create_worktree(self.repo, 'main', 25)
        (run.worktree / 'partial.py').write_text('unfinished = True\n')
        folder = agentic.logs_path(self.repo, run)
        (folder / 'run.json').write_text(json.dumps({
            'instruction': 'Keep the migration backwards compatible',
        }))
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
            self.assertEqual(
                develop.call_args.kwargs['instruction'],
                'Keep the migration backwards compatible',
            )
            self.assertTrue((run.worktree / 'partial.py').exists())
