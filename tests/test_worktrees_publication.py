"""Focused publication tests."""
import json
import re
import sys
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner.telemetry import record_test_result
from test_worktrees import _GitRepositoryTestCase, sh


class PublicationTests(_GitRepositoryTestCase):
    @staticmethod
    def _record_passed_validation(folder):
        record_test_result(folder, passed=True)

    def test_validation_summary_preserves_unavailable_command_status(self):
        summary = agentic._validation_summary(
            'Validation check 1 — passed\n'
            'Validation check 2 — unavailable (required command unavailable)',
            [['python', '-m', 'ok'], ['missing-tool']],
        )
        self.assertIn('Local validation did not pass; publication is blocked.', summary)
        self.assertIn('&lt;configured test command: 3 argv values&gt;</code> — passed', summary)
        self.assertIn('&lt;configured test command: 1 argv values&gt;</code> — unavailable', summary)

    def test_automated_publication_rejects_recorded_local_validation_failure(self):
        run = agentic.create_worktree(self.repo, 'main', 192)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(192, 'Failed local gate', 'Keep checks hard')
        config = {'test_commands': [[sys.executable, '-c', 'pass']], 'max_files': 10}
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Reviewed', 'findings': [], 'skipped': []},
            {'reviewer_agent': {'provider': 'codex'}}, folder,
        )
        (folder / 'run.json').write_text(
            json.dumps({'test_result': {'status': 'failed'}}), encoding='utf-8',
        )

        with self.assertRaisesRegex(RuntimeError, 'Local validation failed'):
            agentic.validate_publication_approval(
                run, issue, 'main', config,
                {**agentic.load_review_approval(folder), 'kind': 'automated'},
                folder,
            )

    def test_publication_rejects_stale_approval_after_terminal_failure(self):
        run = agentic.create_worktree(self.repo, 'main', 195)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(195, 'Stale repair approval', 'Do not publish a failed repair')
        config = {'test_commands': [[sys.executable, '-c', 'pass']], 'max_files': 10}
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        approval = agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Reviewed before repair failure', 'findings': [], 'skipped': []},
            {'reviewer_agent': {'provider': 'codex'}}, folder,
        )
        approval['kind'] = 'automated'
        self._record_passed_validation(folder)

        for marker, state in (
            ('blocked.json', {'status': 'blocked', 'result_status': 'BLOCKED'}),
            ('result.json', {'status': 'RETRY_EXHAUSTED'}),
        ):
            with self.subTest(marker=marker):
                (folder / marker).write_text(json.dumps(state), encoding='utf-8')
                with self.assertRaisesRegex(RuntimeError, 'publication|blocked'):
                    agentic.validate_publication_approval(
                        run, issue, 'main', config, approval, folder,
                    )
                (folder / marker).unlink()

    def test_publication_requires_recorded_successful_validation_for_all_approvals(self):
        for kind, recorded_status in (
            ('automated', None), ('automated', 'unknown'), ('manual', None),
        ):
            with self.subTest(kind=kind, recorded_status=recorded_status):
                run = agentic.create_worktree(self.repo, 'main', 193)
                (run.worktree / 'fix.py').write_text('fixed=True\n')
                folder = agentic.logs_path(self.repo, run)
                issue = agentic.Issue(193, 'Missing local gate', '')
                config = {'max_files': 10}
                base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
                if kind == 'manual':
                    approval = agentic.record_manual_approval(
                        run, issue, 'main', config, folder, 'Human reviewed the patch',
                    )
                else:
                    approval = agentic.record_review_approval(
                        run, issue, 'main', base_sha, digest,
                        {'approved': True, 'summary': 'Reviewed', 'findings': [], 'skipped': []},
                        {'reviewer_agent': {'provider': 'codex'}}, folder,
                    )
                approval['kind'] = kind
                if recorded_status is not None:
                    (folder / 'run.json').write_text(
                        json.dumps({'test_result': {'status': recorded_status}}),
                        encoding='utf-8',
                    )

                with self.assertRaisesRegex(RuntimeError, 'unavailable or not recorded'):
                    agentic.validate_publication_approval(
                        run, issue, 'main', config, approval, folder,
                    )

                self._record_passed_validation(folder)
                with patch('agentic.prepare_patch', return_value=(base_sha, digest, 'summary')):
                    self.assertEqual(
                        agentic.validate_publication_approval(
                            run, issue, 'main', config, approval, folder,
                        )[:2],
                        (base_sha, digest),
                    )

    def test_auto_publish_reuses_existing_pr_and_records_url_without_prompt(self):
        run = agentic.create_worktree(self.repo, 'main', 190)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(190, 'Existing automatic PR', '')
        config = {'test_commands': [[sys.executable, '-c', 'pass']], 'max_files': 10}
        agentic.record_manual_approval(
            run, issue, 'main', config, folder, 'Existing PR is already approved',
        )
        self._record_passed_validation(folder)
        with (
            patch('agentic.check_existing_pr', return_value=[{
                'number': 9, 'url': 'https://github.test/pull/9', 'state': 'OPEN',
            }]),
            patch('agentic_shell_input._interactive_line', side_effect=AssertionError('prompted')),
        ):
            url = agentic.publish(
                run, issue, 'owner/repo', 'main', config, folder,
                auto=True,
            )
        self.assertEqual(url, 'https://github.test/pull/9')
        self.assertEqual(
            json.loads((folder / 'publication.json').read_text())['url'],
            'https://github.test/pull/9',
        )

    def test_automatic_candidate_accepts_standalone_automated_review(self):
        run = agentic.create_worktree(self.repo, 'main', 191)
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(191, 'Standalone review', '')
        agentic.record_review_approval(
            run, issue, 'main', 'base-sha', 'patch-digest',
            {'approved': True, 'summary': 'Approved', 'findings': [], 'skipped': []},
            {'reviewer_agent': {'provider': 'codex'}}, folder,
        )
        self._record_passed_validation(folder)
        self.assertTrue(agentic._automatic_candidate_eligible(issue, folder))

    def test_automatic_candidate_requires_recorded_local_validation(self):
        run = agentic.create_worktree(self.repo, 'main', 194)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(194, 'Missing local validation', '')
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Approved', 'findings': [], 'skipped': []},
            {'reviewer_agent': {'provider': 'codex'}}, folder,
        )
        self.assertFalse(agentic._automatic_candidate_eligible(issue, folder))

    def test_changed_issue_invalidates_both_approval_kinds_with_durable_evidence(self):
        folder = Path(self.tmp.name) / "approval-invalidation"
        folder.mkdir()
        for name, kind in (("manual-approval.json", "manual"), ("review-approval.json", "automated")):
            (folder / name).write_text(json.dumps({
                "status": "approved", "issue_digest": "old-issue",
                "patch_digest": f"{kind}-patch", "branch": "agent/issue-247-test",
            }), encoding="utf-8")

        invalidated = agentic.invalidate_publication_approvals(
            folder, previous_digest="old-issue", current_digest="new-issue",
        )

        self.assertEqual(invalidated, ["manual", "automated"])
        self.assertFalse((folder / "manual-approval.json").exists())
        self.assertFalse((folder / "review-approval.json").exists())
        events = json.loads((folder / "approval-events.json").read_text(encoding="utf-8"))
        self.assertEqual(events[0]["event"], "publication_approvals_invalidated")
        self.assertEqual(
            {item["kind"] for item in events[0]["approvals"]},
            {"manual", "automated"},
        )

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
        self._record_passed_validation(folder)
        (run.worktree / 'fix.py').write_text('after\n')
        with self.assertRaisesRegex(RuntimeError, 'Patch changed since approval'):
            agentic.validate_publication_approval(
                run, issue, 'main', config, approval, folder,
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
        self._record_passed_validation(agentic.logs_path(self.repo, run))
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
                agentic.logs_path(self.repo, run),
            )

        self.assertEqual(result, ('base-sha', 'digest', summary))
        rendered_without_timestamps = re.sub(
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ",
            "",
            console.getvalue(),
            flags=re.MULTILINE,
        )
        self.assertEqual(rendered_without_timestamps, '[PATCH] Proposed changes:\n' + summary + '\n')
        self.assertNotIn('\033[', console.getvalue())


    def test_change_summary_sanitizes_handoff_and_uses_approved_diff_stat(self):
        summary = (
            ' src/app.py | 4 +++-\n'
            ' docs/guide.md | 2 ++\n'
            ' 2 files changed, 5 insertions(+), 1 deletion(-)\n'
        )
        handoff = (
            '\x1b[32mImplemented:\x1b[0m\n'
            '- Added the user-visible summary. <script>alert(1)</script>\n'
            '- Preserved validation and review evidence.\n'
            'Validation: tests passed\n'
            'api_key=supersecretvalue\n'
        )
        body = agentic._change_summary(summary, handoff)

        self.assertIn('## What changed', body)
        self.assertIn('Added the user-visible summary.', body)
        self.assertIn('src/app.py', body)
        self.assertIn('2 files changed', body)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', body)
        self.assertNotIn('<script>', body)
        self.assertNotIn('supersecretvalue', body)
        self.assertNotIn('\x1b', body)
        self.assertLessEqual(len(body), 2_000)


    def test_change_summary_continues_after_inline_implementation_status(self):
        handoff = (
            'Implemented Issue #187.\n\n'
            '- Added a concise change summary before validation.\n'
            '- Preserved the review and log sections.\n\n'
            'Validation: all checks passed\n'
        )

        body = agentic._change_summary(' 1 file changed, 2 insertions(+)', handoff)

        self.assertIn('Added a concise change summary before validation.', body)
        self.assertIn('Preserved the review and log sections.', body)
        self.assertNotIn('Issue #187.', body)


    def test_blocked_change_from_previous_commit_cannot_publish(self):
        run = agentic.create_worktree(self.repo, 'main', 13)
        (run.worktree / 'AGENTS.md').write_text('unexpected instructions\n')
        sh('git', 'add', '-A', cwd=run.worktree)
        sh('git', 'commit', '-m', 'bad', cwd=run.worktree)
        with self.assertRaisesRegex(RuntimeError, 'Forbidden'):
            agentic.prepare_patch(run.worktree, 'main', 10)


    def test_publication_rejects_untracked_symlink_regardless_of_target(self):
        run = agentic.create_worktree(self.repo, 'main', 131)
        outside = self.repo.parent / 'publication-secret.txt'
        outside.write_text('do not publish\n', encoding='utf-8')
        try:
            (run.worktree / 'safe-name.txt').symlink_to(outside)
            (run.worktree / 'other-name.txt').symlink_to(run.worktree / 'AGENTS.md')
        except OSError as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with self.assertRaisesRegex(RuntimeError, 'Non-regular files'):
            agentic.prepare_patch(run.worktree, 'main', 10)


    def test_publication_rejects_tracked_symlink_mode(self):
        run = agentic.create_worktree(self.repo, 'main', 132)
        target = run.worktree / 'target.txt'
        target.write_text('target\n', encoding='utf-8')
        link = run.worktree / 'tracked-name.txt'
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        sh('git', 'add', 'target.txt', 'tracked-name.txt', cwd=run.worktree)
        sh('git', 'commit', '-m', 'add symlink', cwd=run.worktree)
        with self.assertRaisesRegex(RuntimeError, 'Non-regular files'):
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
        self._record_passed_validation(folder)
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
            self.assertFalse(any('push' in c.args[0] for c in mock_command.call_args_list))
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
        self._record_passed_validation(folder)
        with (patch('agentic.check_existing_pr', return_value=[]),
              patch('agentic_shell_input._interactive_line', side_effect=['later', 'q']) as confirm,
              patch('agentic.command', wraps=agentic.command) as mock_command,
              patch('agentic._status_print') as status):
            agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)

        self.assertEqual(confirm.call_count, 2)
        self.assertTrue(any('Invalid confirmation' in call.args[0] for call in status.call_args_list))
        self.assertFalse(any('push' in c.args[0] for c in mock_command.call_args_list))


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
                self._record_passed_validation(folder)
                with (patch('agentic.check_existing_pr', return_value=[]),
                      patch('agentic_shell_input._interactive_line', return_value=response),
                      patch('agentic.command', wraps=agentic.command) as mock_command):
                    agentic.publish(run, issue, 'fake/repo', 'main', cfg, folder)
                self.assertFalse(any(
                    'push' in call_item.args[0]
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
        self._record_passed_validation(folder)
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
        self._record_passed_validation(folder)

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
        self._record_passed_validation(folder)
        agentic.next_log(folder, 'tests').write_text(
            'Validation check 1 — passed\n'
            '\x1b[32mTest Files 2 passed (2)\x1b[0m\n'
            '\x1b[32mTests 4 passed <img src=x> `[details]`\x1b[0m',
            encoding='utf-8',
        )
        agentic.next_log(folder, 'developer').write_text(
            'Implemented:\n- Added a concise change summary.\n'
            'Validation: all checks passed\n',
            encoding='utf-8',
        )
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if 'diff' in argv and '--cached' in argv and '--quiet' in argv:
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
        self.assertIn(call(
            run.worktree, '-c', 'core.hooksPath=/dev/null', 'commit',
            '-m', 'fix: resolve issue #30',
        ), git_mock.call_args_list)
        self.assertIn(call(
            run.worktree, '-c', 'core.hooksPath=/dev/null',
            'push', '-u', 'origin', run.branch,
        ), git_mock.call_args_list)
        pr_command = next(argv for argv in commands if argv[:3] == ['gh', 'pr', 'create'])
        self.assertIn('--draft', pr_command)
        self.assertIn('--base', pr_command)
        self.assertIn('main', pr_command)
        body = pr_command[-1]
        self.assertLess(body.index('## What changed'), body.index('## Independent validation'))
        self.assertIn('Added a concise change summary.', body)
        self.assertIn('## Notes', body)
        self.assertIn('Independent Gemini review', pr_command[-1])
        self.assertIn(
            '- <code>&lt;configured test command: 3 argv values&gt;</code> — passed',
            pr_command[-1],
        )
        self.assertIn('Tests 4 passed &lt;img src=x&gt; &#96;[details]&#96;', pr_command[-1])
        self.assertNotIn('\x1b', pr_command[-1])
        self.assertNotIn('```text', pr_command[-1])


    def test_publish_disables_mutating_hooks_and_revalidates_before_push(self):
        run = agentic.create_worktree(self.repo, 'main', 38)
        (run.worktree / 'fix.py').write_text('approved\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(38, 'Hook safety', 'Keep the approved patch')
        config = {'test_commands': [[sys.executable, '-c', 'pass']], 'max_files': 10}
        base_sha, digest, _ = agentic.prepare_patch(run.worktree, 'main', 10)
        agentic.record_review_approval(
            run, issue, 'main', base_sha, digest,
            {'approved': True, 'summary': 'Good', 'findings': [], 'skipped': []},
            config, folder,
        )
        self._record_passed_validation(folder)
        marker = self.repo / 'hook-ran'
        hook = self.repo / '.git' / 'hooks' / 'pre-commit'
        hook.write_text(
            '#!/bin/sh\n'
            f'printf hook-ran > {marker}\n'
            f'printf mutated > {run.worktree / "fix.py"}\n',
            encoding='utf-8',
        )
        hook.chmod(0o755)

        real_command = agentic.command
        real_git = agentic.git

        def command_without_github(argv, cwd, **kwargs):
            if argv[:3] == ['gh', 'pr', 'create']:
                return CompletedProcess(argv, 0, stdout='https://github.test/pr/38')
            return real_command(argv, cwd, **kwargs)

        def git_without_push(repo, *args, **kwargs):
            if args[2:4] == ('push', '-u'):
                return ''
            return real_git(repo, *args, **kwargs)

        with (
            patch('agentic.check_existing_pr', return_value=[]),
            patch('agentic_shell_input._interactive_line', return_value='y'),
            patch('agentic.command', side_effect=command_without_github),
            patch('agentic.git', side_effect=git_without_push),
        ):
            agentic.publish(run, issue, 'owner/repo', 'main', config, folder)

        self.assertFalse(marker.exists())
        self.assertEqual((run.worktree / 'fix.py').read_text(encoding='utf-8'), 'approved\n')


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
        self._record_passed_validation(folder)

        def fail_on_push(_repo, *args, **_kwargs):
            if args[2:4] == ('push', '-u'):
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
        self._record_passed_validation(folder)
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if 'diff' in argv and '--cached' in argv and '--quiet' in argv:
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
        self.assertIn('## What changed', pr_command[-1])
        self.assertIn('## Notes', pr_command[-1])
        self.assertNotIn('All configured checks passed', pr_command[-1])


    def test_publish_skips_existing_pr_and_rejects_closed_issue(self):
        run = agentic.create_worktree(self.repo, 'main', 35)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        config = {'test_commands': [['go', 'test', './...']], 'max_files': None}
        agentic.record_manual_approval(
            run, agentic.Issue(35, 'Existing', ''), 'main', config, folder,
            'Existing PR is already approved',
        )
        self._record_passed_validation(folder)
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

    def test_existing_pr_shortcut_requires_local_validation(self):
        run = agentic.create_worktree(self.repo, 'main', 350)
        (run.worktree / 'fix.py').write_text('fixed=True\n')
        folder = agentic.logs_path(self.repo, run)
        issue = agentic.Issue(350, 'Existing PR without validation', '')
        config = {'test_commands': [[sys.executable, '-c', 'pass']], 'max_files': 10}
        approval = agentic.record_manual_approval(
            run, issue, 'main', config, folder, 'Approval without local validation',
        )

        with patch('agentic.check_existing_pr') as check:
            with self.assertRaisesRegex(RuntimeError, 'unavailable or not recorded'):
                agentic.publish(
                    run, issue, 'owner/repo', 'main', config, folder,
                    manual_approval=approval, auto=True,
                )
        check.assert_not_called()

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
        self._record_passed_validation(folder)
        pull_request = agentic.PullRequest(
            123, 'Repair', 'Fix CI', 'OPEN', 'main', 'base-sha', run.branch,
            'head-sha', 'owner/repo', 'https://github.test/pull/123',
        )
        commands = []

        def fake_command(argv, cwd, **kwargs):
            commands.append(argv)
            if 'diff' in argv and '--cached' in argv and '--quiet' in argv:
                return CompletedProcess(argv, 1, stdout='')
            return CompletedProcess(argv, 0, stdout='')

        with (patch('agentic.prepare_patch', return_value=('base-sha', 'patch-digest', 'summary')),
              patch('agentic.git') as git_mock,
              patch('agentic.command', side_effect=fake_command),
              patch('agentic._status_print')):
            git_mock.side_effect = ['', 'pushed-sha', '']
            pushed_head = agentic.push_existing_pr(
                run, issue, 'owner/repo', 'main', config, folder, pull_request,
                fetch_pr=lambda: pull_request,
            )
        self.assertEqual(pushed_head, 'pushed-sha')
        self.assertIn(call(
            run.worktree, '-c', 'core.hooksPath=/dev/null',
            'push', '-u', 'origin', run.branch,
        ), git_mock.call_args_list)
        self.assertFalse(any(argv[:3] == ['gh', 'pr', 'create'] for argv in commands))
