"""Focused workflow tests."""
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agentic
import agentic_workflow
from test_worktrees import _GitRepositoryTestCase


class WorkflowTests(_GitRepositoryTestCase):
    def test_no_code_changes_exception_preserves_public_identity(self):
        run = agentic.create_worktree(self.repo, 'main', 13)

        self.assertIs(agentic.NoCodeChangesError, agentic_workflow.NoCodeChangesError)
        with self.assertRaises(agentic.NoCodeChangesError):
            agentic.prepare_patch(run.worktree, 'main', 10)

    def test_workflow_module_is_directly_importable(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "from agentic_models import Issue; "
                    "import agentic_workflow; "
                    "assert agentic_workflow.next_log(Path('.'), 'test').name == 'test-001.txt'; "
                    "assert 'ISSUE #1' in agentic_workflow.agent_prompt(Issue(1, 'title', 'body'))"
                ),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)

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


    def test_verify_and_review_retries_invalid_reviewer_output(self):
        run = agentic.create_worktree(self.repo, 'main', 31)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(31, 'Validate review retry', '')
        config = {
            'test_commands': [['go', 'test', './...']],
            'max_files': None,
            'reviewer_agent': {'provider': 'claude'},
            'system_prompt': 'Review against our project standards.',
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
        self.assertEqual(review.call_args.kwargs['system_prompt'], config['system_prompt'])
        record.assert_called_once_with(folder, approved)


    def test_verify_and_review_stops_before_review_when_tests_fail(self):
        run = agentic.create_worktree(self.repo, 'main', 32)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(32, 'Tests fail', '')
        config = {'test_commands': [['go', 'test', './...']], 'max_files': None}
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        console = TTYBuffer()
        report = '[REVIEW] AssertionError: PASSED /tmp/[REVIEW]/traceback.txt\nERROR: PASSED'
        with (patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}),
              patch('sys.stdout', console),
              patch('agentic.run_tests', return_value=(False, report)),
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
        rendered = console.getvalue()
        self.assertIn('\033[34m[TESTS]\033[0m \033[31mFailed\033[0m', rendered)
        self.assertNotIn('\033[31mFAILED\033[0m', rendered)
        self.assertNotIn('\033[32mPASSED\033[0m', rendered)
        self.assertNotIn('\033[35m[REVIEW]\033[0m', rendered)


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


    def test_develop_blocks_passing_checks_without_code_changes(self):
        from io import StringIO

        run = agentic.create_worktree(self.repo, 'main', 37)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(37, 'No patch blocker', 'Implement the blocked task')
        config = {
            'max_retries': 3,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        developer_report = 'Blocked: the required upstream service is unavailable.'

        def fake_developer(_worktree, _prompt, *, output, **_kwargs):
            output.write_text(developer_report, encoding='utf-8')
            return True, developer_report

        with (patch('agentic.run_agent', side_effect=fake_developer),
              patch('agentic.run_tests', return_value=(True, 'All configured checks passed')),
              patch('agentic.review') as review,
              patch('sys.stdout', new_callable=StringIO) as output):
            result = agentic.develop(
                run, issue, 'main', config, folder,
                test_commands=[['python', '-c', 'print("ok")']],
            )

        self.assertTrue(result)
        self.assertIn('RESULT:   BLOCKED', output.getvalue())
        self.assertIn('Checks passed, but the developer produced no code changes', output.getvalue())
        self.assertIn(developer_report, output.getvalue())
        self.assertIn(str(folder), output.getvalue())
        review.assert_not_called()
        blocked = json.loads((folder / 'blocked.json').read_text(encoding='utf-8'))
        self.assertEqual(blocked['kind'], 'no-code-changes')
        self.assertEqual(blocked['developer_summary'], developer_report)
        self.assertEqual(blocked['developer_log'], 'developer-001.txt')
        self.assertEqual(blocked['logs_path'], str(folder))

        with patch('agentic.run_agent') as resumed_agent:
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        resumed_agent.assert_not_called()


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
            'system_prompt': 'Prefer focused changes and regression tests.',
        }))
        args = agentic.build_parser().parse_args([
            '--repo', str(self.repo), '--config', str(config_path),
            '--state-dir', str(Path(self.tmp.name) / 'agentic-state'),
            '--task', 'Add CSV export\nAdd regression tests.',
            '--instruction', 'Preserve the CSV header order',
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
        self.assertEqual(
            develop.call_args.kwargs['instruction'], 'Preserve the CSV header order',
        )
        self.assertEqual(
            develop.call_args.kwargs['system_prompt'],
            'Prefer focused changes and regression tests.',
        )
        run_folder = agentic.logs_path(
            self.repo, fake_run, Path(self.tmp.name) / 'agentic-state',
        )
        self.assertEqual(
            json.loads((run_folder / 'run.json').read_text())['instruction'],
            'Preserve the CSV header order',
        )


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
