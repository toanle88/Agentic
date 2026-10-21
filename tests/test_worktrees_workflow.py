"""Focused workflow tests."""
import json
import contextlib
import shlex
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
    def test_offline_resume_uses_cached_issue_without_github_metadata(self):
        config_path = self.repo / "agent-config.json"
        config_path.write_text(json.dumps({
            "test_commands": [[sys.executable, "-c", "print('ok')"]],
            "developer_agent": {"provider": "codex"},
            "reviewer_agent": {"provider": "codex"},
        }))
        state_dir = Path(self.tmp.name) / "agentic-state"
        fake_run = agentic.Run(88, "agent/issue-88-aabbccdd", self.repo / "worktree")
        folder = agentic.logs_path(self.repo, fake_run, state_dir)
        from agentic_telemetry import record_issue_metadata
        record_issue_metadata(
            folder, number=88, title="Cached issue", state="OPEN",
            body="Implement the offline resume path.",
        )
        args = agentic.build_parser().parse_args([
            "--repo", str(self.repo), "--config", str(config_path),
            "--state-dir", str(state_dir), "--issue", "88", "--offline",
        ])
        with (
            patch("agentic.preflight_test_commands"),
            patch("agentic.preflight_agent_settings"),
            patch("agentic.default_base_branch", return_value="main"),
            patch("agentic.runs_for_issue", return_value=[fake_run]),
            patch("agentic.github_metadata", side_effect=AssertionError("GitHub metadata was requested")),
            patch("agentic.choose_existing", return_value=fake_run),
            patch("agentic.worktree_lock", return_value=contextlib.nullcontext()),
            patch("agentic._run_investigation", return_value={"summary": "cached", "findings": []}),
            patch("agentic.run_tests", return_value=(True, "ok")),
            patch("agentic.develop", return_value=True) as develop,
        ):
            self.assertEqual(agentic.execute_locked(args, self.repo), 0)
        self.assertEqual(develop.call_args.args[1].title, "Cached issue")
        self.assertEqual(develop.call_args.args[1].body, "Implement the offline resume path.")

    def test_local_resume_action_materializes_task_file_invocation(self):
        task_file = self.repo / "task.md"
        task_file.write_text("Implement the local task.\n")
        issue = agentic.Issue.from_local_text(task_file.read_text(), task_file)

        action = agentic_workflow._resume_action(issue, self.repo / "logs")

        resume_file = self.repo / "logs" / "resume-task.md"
        self.assertIn(shlex.join([
            "python", "agentic.py", "--task-file", str(resume_file.resolve()),
        ]), action)
        self.assertEqual(resume_file.read_text(encoding="utf-8"), issue.body)
        self.assertNotIn("rerun the local task", action)

    def test_local_resume_action_materializes_inline_task_without_displaying_body(self):
        body = "Add CSV export\n\x1b[31mPreserve the header order.\n"
        issue = agentic.Issue.from_local_text(body)
        folder = self.repo / "logs"
        folder.mkdir()

        action = agentic_workflow._resume_action(issue, folder)

        task_file = folder / "resume-task.md"
        command = shlex.join(["python", "agentic.py", "--task-file", str(task_file.resolve())])
        self.assertIn(command, action)
        self.assertNotIn(issue.body, action)
        self.assertNotIn("\x1b", action)
        self.assertNotIn("--task ", action)
        self.assertEqual(task_file.read_text(encoding="utf-8"), issue.body)

    def test_local_resume_action_reuses_materialized_task_file(self):
        issue = agentic.Issue.from_local_text("Add CSV export\nPreserve the header order.")
        folder = self.repo / "logs"
        folder.mkdir()
        task_file = folder / "resume-task.md"
        task_file.write_text("original task\n", encoding="utf-8")

        action = agentic_workflow._resume_action(issue, folder)

        self.assertIn(shlex.join([
            "python", "agentic.py", "--task-file", str(task_file.resolve()),
        ]), action)
        self.assertEqual(task_file.read_text(encoding="utf-8"), "original task\n")

    def test_no_code_changes_exception_preserves_public_identity(self):
        run = agentic.create_worktree(self.repo, 'main', 13)

        self.assertIs(agentic.NoCodeChangesError, agentic_workflow.NoCodeChangesError)
        with self.assertRaises(agentic.NoCodeChangesError):
            agentic.prepare_patch(run.worktree, 'main', 10)

    def test_self_hosted_exception_allows_ci_workflows_but_not_policy_files(self):
        with patch.object(agentic_workflow, 'SCRIPT_DIR', self.repo):
            self.assertTrue(agentic_workflow._runner_source_change_allowed(
                self.repo, 'agentic.py',
            ))
            self.assertTrue(agentic_workflow._runner_source_change_allowed(
                self.repo, '.github/workflows/ci.yml',
            ))
            self.assertFalse(agentic_workflow._runner_source_change_allowed(
                self.repo, 'AGENTS.md',
            ))
            self.assertFalse(agentic_workflow._runner_source_change_allowed(
                self.repo, '.github/AGENTS.md',
            ))

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
        self.assertIn('git diff --no-ext-diff --no-textconv --cached', calls[1][-1])
        self.assertIn('<staged_patch>', calls[1][-1])
        self.assertIn('answer=42', calls[1][-1])
        self.assertNotIn('git diff --cached', calls[1][-1])
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


    def test_successful_review_clears_obsolete_terminal_markers(self):
        run = agentic.create_worktree(self.repo, 'main', 40)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(40, 'Clear terminal markers', '')
        (folder / 'blocked.json').write_text(json.dumps({
            'status': 'blocked',
            'result_status': 'BLOCKED',
            'patch_digest': 'old-digest',
        }))
        (folder / 'result.json').write_text(json.dumps({
            'status': 'RETRY_EXHAUSTED',
            'attempts': 4,
            'max_retries': 3,
        }))
        config = {
            'test_commands': [['python', '-c', 'print("ok")']],
            'max_files': None,
            'reviewer_agent': {'provider': 'codex'},
        }
        patch_value = ('base-sha', 'new-digest', 'one file changed')
        approved = {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []}
        with (patch('agentic.run_tests', return_value=(True, 'tests passed')),
              patch('agentic.prepare_patch', side_effect=[patch_value, patch_value]),
              patch('agentic.review', return_value=approved)):
            result = agentic.verify_and_review(run, issue, 'main', config, folder)

        self.assertTrue(result[0])
        self.assertFalse((folder / 'blocked.json').exists())
        self.assertFalse((folder / 'result.json').exists())
        self.assertTrue((folder / 'review-approval.json').exists())

    def test_rejected_review_clears_obsolete_terminal_markers(self):
        run = agentic.create_worktree(self.repo, 'main', 41)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(41, 'Clear rejected terminal markers', '')
        (folder / 'blocked.json').write_text(json.dumps({
            'status': 'blocked',
            'result_status': 'BLOCKED',
            'patch_digest': 'old-digest',
        }))
        (folder / 'result.json').write_text(json.dumps({
            'status': 'RETRY_EXHAUSTED',
            'attempts': 4,
            'max_retries': 3,
        }))
        config = {
            'test_commands': [['python', '-c', 'print("ok")']],
            'max_files': None,
            'reviewer_agent': {'provider': 'codex'},
        }
        rejected = {
            'approved': False, 'summary': 'Still missing the contract',
            'findings': ['Define the contract'], 'skipped': [],
        }
        with (patch('agentic.run_tests', return_value=(True, 'tests passed')),
              patch('agentic.prepare_patch', return_value=('base-sha', 'new-digest', 'changed')),
              patch('agentic.review', return_value=rejected)):
            result = agentic.verify_and_review(run, issue, 'main', config, folder)

        self.assertFalse(result[0])
        self.assertFalse((folder / 'blocked.json').exists())
        self.assertFalse((folder / 'result.json').exists())

    def test_failed_review_clears_obsolete_terminal_markers(self):
        run = agentic.create_worktree(self.repo, 'main', 42)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(42, 'Clear failed terminal markers', '')
        (folder / 'blocked.json').write_text(json.dumps({
            'status': 'blocked', 'patch_digest': 'old-digest',
        }))
        (folder / 'result.json').write_text(json.dumps({'status': 'BLOCKED'}))
        config = {'test_commands': [['python', '-c', 'print("ok")']], 'max_files': None}
        with (patch('agentic.run_tests', return_value=(False, 'tests failed')),
              patch('agentic.prepare_patch') as prepare,
              patch('agentic.review') as review):
            result = agentic.verify_and_review(run, issue, 'main', config, folder)

        self.assertFalse(result[0])
        self.assertFalse((folder / 'blocked.json').exists())
        self.assertFalse((folder / 'result.json').exists())
        prepare.assert_not_called()
        review.assert_not_called()


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
        self.assertIn('\033[31m[TESTS]\033[0m \033[31mFailed\033[0m', rendered)
        self.assertNotIn('\033[31mFAILED\033[0m', rendered)
        self.assertNotIn('\033[32mPASSED\033[0m', rendered)
        self.assertNotIn('\033[35m[REVIEW]\033[0m', rendered)

    def test_verify_and_review_colors_patch_diff_stat_on_tty(self):
        run = agentic.create_worktree(self.repo, 'main', 122)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(122, 'Color patch summary', '')
        config = {'test_commands': [['python', '-c', 'print("ok")']], 'max_files': None}

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        console = TTYBuffer()
        summary = (
            ' src/App.tsx | 4 ++--\n'
            ' 1 file changed, 2 insertions(+), 2 deletions(-)\n'
        )
        verdict = {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []}
        with (patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}),
              patch('sys.stdout', console),
              patch('agentic.run_tests', return_value=(True, 'tests passed')),
              patch('agentic.prepare_patch', side_effect=[
                  ('base-sha', 'digest', summary), ('base-sha', 'digest', summary),
              ]),
              patch('agentic.review', return_value=verdict)):
            passed, *_ = agentic.verify_and_review(run, issue, 'main', config, folder)

        self.assertTrue(passed)
        rendered = console.getvalue()
        self.assertIn('\033[95m[PATCH]\033[0m Proposed changes:', rendered)
        self.assertIn(' src/App.tsx | 4 \033[32m++\033[0m\033[31m--\033[0m', rendered)
        self.assertIn('\033[32m2 insertions(+)\033[0m, \033[31m2 deletions(-)\033[0m', rendered)

        for path in folder.iterdir():
            if path.is_file():
                self.assertNotIn('\033[', path.read_text(encoding='utf-8'))


    def test_verify_and_review_keeps_patch_diff_stat_plain_when_redirected(self):
        run = agentic.create_worktree(self.repo, 'main', 123)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(123, 'Plain patch summary', '')
        config = {'test_commands': [['python', '-c', 'print("ok")']], 'max_files': None}
        summary = (
            ' src/App.tsx | 4 ++--\n'
            ' 1 file changed, 2 insertions(+), 2 deletions(-)\n'
        )
        verdict = {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []}
        console = StringIO()
        with (patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}),
              patch('sys.stdout', console),
              patch('agentic.run_tests', return_value=(True, 'tests passed')),
              patch('agentic.prepare_patch', side_effect=[
                  ('base-sha', 'digest', summary), ('base-sha', 'digest', summary),
              ]),
              patch('agentic.review', return_value=verdict)):
            passed, *_ = agentic.verify_and_review(run, issue, 'main', config, folder)

        self.assertTrue(passed)
        rendered = console.getvalue()
        self.assertIn('[PATCH] Proposed changes:\n' + summary, rendered)
        self.assertNotIn('\033[', rendered)


    def test_verify_and_review_keeps_patch_diff_stat_plain_with_no_color_on_tty(self):
        run = agentic.create_worktree(self.repo, 'main', 124)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(124, 'No color patch summary', '')
        config = {'test_commands': [['python', '-c', 'print("ok")']], 'max_files': None}

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        console = TTYBuffer()
        summary = (
            ' src/App.tsx | 4 ++--\n'
            ' 1 file changed, 2 insertions(+), 2 deletions(-)\n'
        )
        verdict = {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []}
        with (patch.dict('os.environ', {'NO_COLOR': '1', 'AGENTIC_PLAIN': ''}),
              patch('sys.stdout', console),
              patch('agentic.run_tests', return_value=(True, 'tests passed')),
              patch('agentic.prepare_patch', side_effect=[
                  ('base-sha', 'digest', summary), ('base-sha', 'digest', summary),
              ]),
              patch('agentic.review', return_value=verdict)):
            passed, *_ = agentic.verify_and_review(run, issue, 'main', config, folder)

        self.assertTrue(passed)
        self.assertNotIn('\033[', console.getvalue())


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


    def test_develop_reports_attempts_and_stops_after_five_retries(self):
        run = agentic.create_worktree(self.repo, 'main', 38)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(38, 'Retry limit', 'Implement it')
        config = {
            'max_retries': 5,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        with (patch('agentic.run_agent', return_value=(True, 'implemented')) as run_agent,
              patch('agentic.verify_and_review', return_value=(
                  False, 'AssertionError: expected a changed result', {}, '', '',
              )),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertFalse(agentic.develop(run, issue, 'main', config, folder))

        self.assertEqual(run_agent.call_count, 6)
        rendered = output.getvalue()
        for attempt in range(1, 7):
            self.assertIn(f'attempt {attempt}/6', rendered)
        self.assertIn('[RESULT] RETRY_EXHAUSTED', rendered)
        self.assertIn('Attempts: 6/6 (configured retries: 5)', rendered)
        self.assertIn('Reason:   Tests failed: AssertionError: expected a changed result', rendered)
        self.assertIn(f'Logs:     {folder}', rendered)
        self.assertIn('then python agentic.py --issue 38', rendered)
        result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'RETRY_EXHAUSTED')
        self.assertEqual(result['attempts'], 6)
        self.assertEqual(result['max_retries'], 5)


    def test_develop_runs_only_initial_attempt_when_retries_are_zero(self):
        run = agentic.create_worktree(self.repo, 'main', 39)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(39, 'No retries', 'Implement it')
        config = {
            'max_retries': 0,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        with (patch('agentic.run_agent', return_value=(True, 'implemented')) as run_agent,
              patch('agentic.verify_and_review', return_value=(
                  False, 'AssertionError: expected a changed result', {}, '', '',
              )),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertFalse(agentic.develop(run, issue, 'main', config, folder))

        run_agent.assert_called_once()
        self.assertIn('attempt 1/1', output.getvalue())
        self.assertIn('[RESULT] RETRY_EXHAUSTED', output.getvalue())
        self.assertIn('Attempts: 1/1 (configured retries: 0)', output.getvalue())


    def test_develop_local_retry_exhaustion_reports_reusable_task_file_command(self):
        run = agentic.create_worktree(self.repo, 'main', 43)
        folder = agentic.logs_path(self.repo, run)
        task_file = self.repo / 'local-task.md'
        task_file.write_text('Implement the local task.\n')
        issue = agentic.Issue.from_local_text(task_file.read_text(), task_file)
        config = {
            'max_retries': 0,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        with (patch('agentic.run_agent', return_value=(True, 'implemented')),
              patch('agentic.verify_and_review', return_value=(
                  False, 'AssertionError: expected a changed result', {}, '', '',
              )),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertFalse(agentic.develop(run, issue, 'main', config, folder))

        command = shlex.join([
            'python', 'agentic.py', '--task-file',
            str((folder / 'resume-task.md').resolve()),
        ])
        self.assertIn(f'then {command}', output.getvalue())
        result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
        self.assertIn(command, result['next_action'])

    def test_develop_inline_retry_resume_does_not_persist_task_body(self):
        run = agentic.create_worktree(self.repo, 'main', 44)
        folder = agentic.logs_path(self.repo, run)
        body = 'Implement it\n\x1b]0;injected title\x07\n' + ('x' * 5_000)
        issue = agentic.Issue.from_local_text(body)
        config = {
            'max_retries': 0,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        with (patch('agentic.run_agent', return_value=(True, 'implemented')),
              patch('agentic.verify_and_review', return_value=(
                  False, 'AssertionError: expected a changed result', {}, '', '',
              )),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertFalse(agentic.develop(run, issue, 'main', config, folder))

        result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
        action = result['next_action']
        self.assertLess(len(action), 1_000)
        self.assertNotIn(body, action)
        self.assertNotIn('\x1b', action)
        self.assertNotIn('\x07', action)
        self.assertIn('--task-file', action)
        self.assertEqual((folder / 'resume-task.md').read_text(encoding='utf-8'), body)
        self.assertNotIn(body, output.getvalue())
        self.assertNotIn('\x1b', output.getvalue())
        self.assertNotIn('\x07', output.getvalue())


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
            'max_retries': 5,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        developer_report = 'Blocked: the required upstream service is unavailable.'

        def fake_developer(_worktree, _prompt, *, output, **_kwargs):
            output.write_text(developer_report, encoding='utf-8')
            return True, developer_report

        with (patch('agentic.run_agent', side_effect=fake_developer) as run_agent,
              patch('agentic.run_tests', return_value=(True, 'All configured checks passed')),
              patch('agentic.review') as review,
              patch('sys.stdout', new_callable=StringIO) as output):
            result = agentic.develop(
                run, issue, 'main', config, folder,
                test_commands=[['python', '-c', 'print("ok")']],
            )

        self.assertTrue(result)
        self.assertIn('[RESULT] NO_CHANGES', output.getvalue())
        self.assertIn('Attempts: 1/6 (configured retries: 5)', output.getvalue())
        self.assertIn('Checks passed, but the developer produced no code changes', output.getvalue())
        self.assertIn(developer_report, output.getvalue())
        self.assertIn(str(folder), output.getvalue())
        run_agent.assert_called_once()
        review.assert_not_called()
        blocked = json.loads((folder / 'blocked.json').read_text(encoding='utf-8'))
        self.assertEqual(blocked['kind'], 'no-code-changes')
        self.assertEqual(blocked['result_status'], 'NO_CHANGES')
        self.assertEqual(blocked['issue_digest'], agentic.issue_fingerprint(issue))
        self.assertEqual(blocked['developer_summary'], developer_report)
        self.assertEqual(blocked['developer_log'], 'developer-001.txt')
        self.assertEqual(blocked['logs_path'], str(folder))
        result_record = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result_record['status'], 'NO_CHANGES')

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
              patch('agentic.verify_and_review', return_value=review),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        self.assertEqual(run_agent.call_count, 2)
        blocked = json.loads((folder / 'blocked.json').read_text())
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['result_status'], 'BLOCKED')
        self.assertEqual(blocked['patch_digest'], 'same-digest')
        self.assertEqual(blocked['issue_digest'], agentic.issue_fingerprint(issue))
        self.assertIn('Production adapter is unavailable', blocked['summary'])
        rendered = output.getvalue()
        self.assertIn('[RESULT] BLOCKED', rendered)
        self.assertIn('Attempts: 2/4 (configured retries: 3)', rendered)
        self.assertIn('Production adapter is unavailable', rendered)
        self.assertIn(f'Logs:     {folder}', rendered)

        with (patch('agentic.prepare_patch', return_value=('base', 'same-digest', 'summary')),
              patch('agentic.run_agent') as resumed_agent):
            self.assertTrue(agentic.develop(run, issue, 'main', config, folder))
        resumed_agent.assert_not_called()


    def test_develop_resumes_review_blocker_when_issue_changes(self):
        run = agentic.create_worktree(self.repo, 'main', 45)
        folder = agentic.logs_path(self.repo, run)
        blocked_issue = agentic.Issue(45, 'Missing contract', 'Define the API contract.')
        changed_issue = agentic.Issue(45, 'Missing contract', 'Define the API contract and error cases.')
        (folder / 'blocked.json').write_text(json.dumps({
            'status': 'blocked',
            'result_status': 'BLOCKED',
            'patch_digest': 'same-digest',
            'issue_digest': agentic.issue_fingerprint(blocked_issue),
            'summary': 'The contract is still missing',
            'attempts': 2,
        }))
        (folder / 'result.json').write_text(json.dumps({'status': 'BLOCKED'}))
        config = {
            'max_retries': 3,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        approved = (True, 'tests passed', {
            'approved': True, 'summary': 'Contract is now defined', 'findings': [],
        }, 'same-digest', 'base')
        with (patch('agentic.prepare_patch', return_value=('base', 'same-digest', 'summary')),
              patch('agentic.run_agent', return_value=(True, 'implemented')) as run_agent,
              patch('agentic.verify_and_review', return_value=approved),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertTrue(agentic.develop(run, changed_issue, 'main', config, folder))

        run_agent.assert_called_once()
        self.assertFalse((folder / 'blocked.json').exists())
        self.assertFalse((folder / 'result.json').exists())
        self.assertIn('[RESUME] Issue changed since the previous blocked run; resumed development.',
                      output.getvalue())
        self.assertIn('Reason:   Issue changed since the previous blocked run; resumed development.',
                      output.getvalue())


    def test_develop_resumes_no_code_blocker_when_issue_changes(self):
        run = agentic.create_worktree(self.repo, 'main', 46)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(46, 'Blocked task', 'Implement the task.')
        config = {
            'max_retries': 1,
            'max_files': None,
            'developer_agent': {'provider': 'codex'},
        }
        developer_report = 'Blocked: the required upstream service is unavailable.'

        def fake_developer(_worktree, _prompt, *, output, **_kwargs):
            output.write_text(developer_report, encoding='utf-8')
            return True, developer_report

        with (patch('agentic.run_agent', side_effect=fake_developer),
              patch('agentic.run_tests', return_value=(True, 'All configured checks passed')),
              patch('agentic.review'),
              patch('sys.stdout', new_callable=StringIO)):
            self.assertTrue(agentic.develop(
                run, issue, 'main', config, folder,
                test_commands=[['python', '-c', 'print("ok")']],
            ))

        blocked = json.loads((folder / 'blocked.json').read_text(encoding='utf-8'))
        changed_issue = agentic.Issue(46, 'Blocked task', 'Implement the task and document retries.')
        self.assertEqual(blocked['issue_digest'], agentic.issue_fingerprint(issue))

        with (patch('agentic.prepare_patch', return_value=('base', '', 'no changes')),
              patch('agentic.run_agent', return_value=(True, 'implemented')) as resumed_agent,
              patch('agentic.verify_and_review', return_value=(
                  True, 'tests passed', {'approved': True, 'summary': 'approved', 'findings': []},
                  '', 'base',
              )),
              patch('sys.stdout', new_callable=StringIO) as output):
            self.assertTrue(agentic.develop(run, changed_issue, 'main', config, folder))

        resumed_agent.assert_called_once()
        self.assertFalse((folder / 'blocked.json').exists())
        self.assertFalse((folder / 'result.json').exists())
        self.assertIn('[RESUME] Issue changed since the previous blocked run; resumed development.',
                      output.getvalue())


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


    def test_execute_local_retry_failure_reports_saved_resume_command(self):
        config_path = self.repo / 'local-agent-config.json'
        config_path.write_text(json.dumps({
            'task_source': 'local',
            'test_commands': [[sys.executable, '-c', 'print("ok")']],
        }))
        task_file = self.repo / 'task.md'
        task_file.write_text('Implement the local task.\n')
        state_dir = Path(self.tmp.name) / 'agentic-state'
        args = agentic.build_parser().parse_args([
            '--repo', str(self.repo), '--config', str(config_path),
            '--state-dir', str(state_dir), '--task-file', str(task_file),
        ])
        fake_run = agentic.Run(None, 'agent/task-local-aabbccddeeff', Path('/tmp/fake-worktree'))
        run_folder = agentic.logs_path(self.repo, fake_run, state_dir)
        command = shlex.join([
            'python', 'agentic.py', '--task-file', str(task_file.resolve()),
        ])

        def incomplete(*_args, **_kwargs):
            (run_folder / 'result.json').write_text(json.dumps({
                'status': 'RETRY_EXHAUSTED', 'next_action': (
                    f'inspect the run logs at {run_folder}, then {command}'
                ),
            }))
            return False

        with (patch('agentic.preflight_test_commands'),
              patch('agentic.preflight_agent_settings'),
              patch('agentic.create_local_worktree', return_value=fake_run),
              patch('agentic._run_investigation', return_value={
                  'summary': 'Valid local task', 'findings': [],
              }),
              patch('agentic.run_tests', return_value=(True, 'ok')),
              patch('agentic.develop', side_effect=incomplete)):
            with self.assertRaisesRegex(RuntimeError, r'RETRY_EXHAUSTED: .*--task-file') as raised:
                agentic.execute_locked(args, self.repo)

        self.assertNotIn('--issue N', str(raised.exception))
        self.assertIn(command, str(raised.exception))


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
