"""Streaming and command-runner tests."""
import json
import os
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import execution as agentic_execution
from agentic_runner import workflow as agentic_workflow
from agentic_runner.output import (
    PatchSummaryStream,
    is_transient_progress,
    strip_ansi,
    style_patch_summary,
    style_system_text,
    style_text,
)
from agentic_runner.execution_stream import MAX_RAW_LOG_BYTES
import agentic_runner.execution_stream as execution_stream


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class StreamingTests(unittest.TestCase):
    def test_style_patch_summary_colors_git_additions_and_removals(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        summary = (
            '[PATCH] Proposed changes:\n'
            ' src/App.css      | 51 +++++++++++++-\n'
            ' src/App.test.tsx | 19 +++++\n'
            ' 2 files changed, 70 insertions(+), 1 deletion(-)\n'
        )
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_patch_summary(summary)

        self.assertIn('\033[95m[PATCH]\033[0m Proposed changes:', styled)
        self.assertIn(' src/App.css      | 51 \033[32m+++++++++++++\033[0m\033[31m-\033[0m', styled)
        self.assertIn(' src/App.test.tsx | 19 \033[32m+++++\033[0m', styled)
        self.assertIn(
            '\033[32m70 insertions(+)\033[0m, \033[31m1 deletion(-)\033[0m',
            styled,
        )
        self.assertEqual(style_patch_summary(styled), summary)

    def test_style_patch_summary_preserves_unsafe_or_non_stat_lines(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        summary = (
            '[PATCH] Proposed changes:\n'
            ' weird+file-.txt | 2 +-\n'
            ' image.bin        | Bin 0 -> 4 bytes\n'
            ' old-name.txt => new-name.txt | 0\n'
            ' 2 files changed\n'
        )
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_patch_summary(summary)

        self.assertIn(' weird+file-.txt | 2 \033[32m+\033[0m\033[31m-\033[0m', styled)
        self.assertIn(' image.bin        | Bin 0 -> 4 bytes', styled)
        self.assertIn(' old-name.txt => new-name.txt | 0', styled)
        self.assertIn(' 2 files changed', styled)
        stat_line = styled.split(' weird+file-.txt', 1)[1].splitlines()[0]
        self.assertNotIn('\033[', stat_line.split('|', 1)[0])

    def test_style_patch_summary_aligns_unindented_first_stat_row(self):
        summary = (
            '[PATCH] Proposed changes:\n'
            'SECURITY.md                                        | 35 ++++\n'
            ' agent_config.example.json                          | 11 +\n'
            ' 2 files changed, 46 insertions(+)\n'
        )

        aligned = style_patch_summary(summary)
        rows = aligned.splitlines()[1:3]
        self.assertTrue(rows[0].startswith(' '))
        self.assertEqual(rows[0].index('|'), rows[1].index('|'))

    def test_style_patch_summary_colors_diff_totals_without_english_labels(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        summary = (
            '[PATCH] Proposed changes:\n'
            ' 2 Dateien geändert, 4 Einfügungen(+), 1 Löschungen(-)\n'
        )
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_patch_summary(summary)

        self.assertIn('\033[32m4 Einfügungen(+)\033[0m', styled)
        self.assertIn('\033[31m1 Löschungen(-)\033[0m', styled)
        self.assertEqual(style_patch_summary(styled), summary)

    def test_style_patch_summary_is_plain_for_redirected_output_and_no_color(self):
        summary = '[PATCH] Proposed changes:\n file.py | 2 ++\n 1 file changed, 2 insertions(+)\n'
        self.assertEqual(style_patch_summary(summary), summary)

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '1', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            self.assertEqual(style_patch_summary(summary), summary)

    def test_patch_summary_stream_styles_split_live_log_chunks(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        output = TTYBuffer()
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=output):
            renderer = PatchSummaryStream()
            rendered = renderer.feed('[PATCH] Proposed changes:\n src/App.py | 4 ')
            rendered += renderer.feed('++--\n 1 file changed, 2 insertions(+), ')
            rendered += renderer.feed('2 deletions(-)\n[REVIEW] next\n')
            rendered += renderer.flush()

            timestamped = PatchSummaryStream(output)
            rendered_timestamped = timestamped.feed('[13:42:27')
            rendered_timestamped += timestamped.feed(
                '] [PATCH] Proposed changes:\n src/App.py | 4 ++--\n'
            )
            rendered_timestamped += timestamped.flush()

        self.assertIn('\033[95m[PATCH]\033[0m Proposed changes:', rendered)
        self.assertIn(' src/App.py | 4 \033[32m++\033[0m\033[31m--\033[0m', rendered)
        self.assertIn('\033[32m2 insertions(+)\033[0m', rendered)
        self.assertIn('\033[31m2 deletions(-)\033[0m', rendered)
        self.assertIn('\033[35m[REVIEW]\033[0m next', rendered)

        self.assertIn('\033[95m[PATCH]\033[0m Proposed changes:', rendered_timestamped)
        self.assertIn(
            ' src/App.py | 4 \033[32m++\033[0m\033[31m--\033[0m',
            rendered_timestamped,
        )

        unindented = PatchSummaryStream()
        rendered_unindented = unindented.feed(
            '[PATCH] Proposed changes:\nSECURITY.md                                        | 35 ++++\n'
            ' agent_config.example.json                          | 11 +\n'
        )
        rows = rendered_unindented.splitlines()[1:3]
        plain_rows = [strip_ansi(row) for row in rows]
        self.assertEqual(plain_rows[0].index('|'), plain_rows[1].index('|'))

    def test_status_styling_is_tty_only_and_honors_no_color(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        plain = '[00:30:10] [TESTS] PASSED /tmp/result.txt'
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), patch('sys.stdout', new=TTYBuffer()):
            self.assertIn('\033[32m', style_text('[TESTS] PASSED'))
        with patch.dict('os.environ', {'NO_COLOR': '1'}), patch('sys.stdout', new=TTYBuffer()):
            self.assertEqual(style_text(plain), plain)
        self.assertEqual(style_text(plain), plain)

    def test_style_text_colors_prefixes_and_status_tokens_only(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_text(
                '[00:30:10] [TESTS] Finished in 0s (exit 1) /tmp/traceback.py\n'
                '[REVIEW] APPROVED summary text'
            )
        self.assertIn('\033[90m[00:30:10]\033[0m', styled)
        self.assertIn('\033[31m[TESTS]\033[0m', styled)
        self.assertIn('\033[32mFinished\033[0m', styled)
        self.assertIn('\033[31mexit 1\033[0m', styled)
        self.assertIn('/tmp/traceback.py', styled)
        self.assertIn('\033[35m[REVIEW]\033[0m \033[32mAPPROVED\033[0m summary text', styled)
        self.assertNotIn('\033[34m[TESTS] Finished', styled)

    def test_style_text_does_not_color_message_body_or_file_paths(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_text(
                '[TESTS] AssertionError in /tmp/FAILED.txt '
                '/tmp/[REVIEW]/result.txt [REVIEW] details'
            )
        self.assertEqual(
            styled,
            '\033[34m[TESTS]\033[0m AssertionError in /tmp/FAILED.txt '
            '/tmp/[REVIEW]/result.txt [REVIEW] details',
        )

    def test_style_text_does_not_color_trailing_message_labels_or_paths(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            self.assertEqual(style_text('message [REVIEW]'), 'message [REVIEW]')
            self.assertEqual(style_text('message FAILED /tmp/result.txt'), 'message FAILED /tmp/result.txt')
            self.assertEqual(style_text('/tmp/[REVIEW]'), '/tmp/[REVIEW]')
            self.assertEqual(style_text('[REVIEW]/result.txt'), '[REVIEW]/result.txt')
            self.assertEqual(style_text('[REVIEW]\\result.txt'), '[REVIEW]\\result.txt')
            self.assertEqual(
                style_text('[TESTS] report C:\\tmp\\FAILED.txt /tmp/[REVIEW]'),
                '\033[34m[TESTS]\033[0m report C:\\tmp\\FAILED.txt /tmp/[REVIEW]',
            )
            self.assertEqual(
                style_text('[TESTS] report /tmp/[PASSED] C:\\tmp\\[RETRY]'),
                '\033[34m[TESTS]\033[0m report /tmp/[PASSED] C:\\tmp\\[RETRY]',
            )
            self.assertEqual(
                style_text('[TESTS] report /tmp/[FAIL ] C:\\tmp\\[WARN ]'),
                '\033[34m[TESTS]\033[0m report /tmp/[FAIL ] C:\\tmp\\[WARN ]',
            )
            self.assertEqual(
                style_text('[TESTS] report /tmp/folder with space/[FAIL].txt'),
                '\033[34m[TESTS]\033[0m report /tmp/folder with space/[FAIL].txt',
            )
            self.assertEqual(
                style_text('[TESTS] report /tmp/folder with space/[RETRY]'),
                '\033[34m[TESTS]\033[0m report /tmp/folder with space/[RETRY]',
            )
            self.assertEqual(
                style_text('[TESTS] report /tmp/folder with space [RETRY]'),
                '\033[34m[TESTS]\033[0m report /tmp/folder with space [RETRY]',
            )

    def test_style_text_colors_each_structured_prefix(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        expected = {
            'DEVELOPER': '36', 'REVIEW': '35', 'TESTS': '34', 'RETRY': '33',
            'GIT': '33', 'PATCH': '95', 'ERROR': '31', 'RESULT': '31',
            'COST': '90', 'NO_CHANGES': '33', 'CANCEL': '33',
            'DOCTOR': '36', 'TIMEOUT': '33', 'BLOCKED': '33', 'STOP': '33',
        }
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            for label, code in expected.items():
                with self.subTest(label=label):
                    self.assertEqual(
                        style_text(f'[{label}] message'),
                        f'\033[{code}m[{label}]\033[0m message',
                    )
            self.assertEqual(
                style_text('[RETRY] WARNING: retrying'),
                '\033[33m[RETRY]\033[0m \033[33mWARNING\033[0m: retrying',
            )
            self.assertEqual(
                style_text('[TESTS] Finished in 0s (exit 124) [TIMEOUT]'),
                '\033[31m[TESTS]\033[0m \033[32mFinished\033[0m in 0s '
                '(\033[31mexit 124\033[0m) \033[33m[TIMEOUT]\033[0m',
            )
            self.assertEqual(
                style_text('[TESTS] Finished in 0s (exit -9)'),
                '\033[31m[TESTS]\033[0m \033[32mFinished\033[0m in 0s '
                '(\033[31mexit -9\033[0m)',
            )
            self.assertEqual(
                style_text('[STOP] no running jobs', style_status_tokens=False),
                '\033[33m[STOP]\033[0m no running jobs',
            )

    def test_style_system_text_supports_future_prefixes_without_coloring_body(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_system_text(
                '[FUTURE_STAGE] [REVIEW] provider text\n[REVIEW] user text',
                system_prefix='FUTURE_STAGE', style_status_tokens=False,
            )
            generic = style_text('[FUTURE_STAGE] provider text')
            inferred = style_system_text('[FUTURE_STAGE] provider text')
            plain = style_system_text('[FUTURE_STAGE] provider text', system_prefix='FUTURE_STAGE')
        self.assertEqual(
            styled,
            '\033[90m[FUTURE_STAGE]\033[0m [REVIEW] provider text\n[REVIEW] user text',
        )
        self.assertEqual(generic, '[FUTURE_STAGE] provider text')
        self.assertEqual(inferred, '\033[90m[FUTURE_STAGE]\033[0m provider text')
        self.assertEqual(plain, '\033[90m[FUTURE_STAGE]\033[0m provider text')

    def test_style_system_text_preserves_wrapping_and_plain_output(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        message = '[DEVELOPER] Agent response:\n[REVIEW] body\nnext line\n'
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_system_text(message, system_prefix='DEVELOPER', style_status_tokens=False)
        self.assertEqual(
            styled,
            '\033[36m[DEVELOPER]\033[0m Agent response:\n[REVIEW] body\nnext line\n',
        )

        with patch.dict('os.environ', {'NO_COLOR': '1'}):
            self.assertEqual(
                style_system_text(message, system_prefix='DEVELOPER'),
                message,
            )

    def test_stage_prefix_colors_follow_outcomes_without_painting_body(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            self.assertEqual(
                style_text('[TESTS] passed /tmp/FAILED.txt', style_status_tokens=False),
                '\033[32m[TESTS]\033[0m passed /tmp/FAILED.txt',
            )
            self.assertEqual(
                style_text('[TESTS] failed /tmp/PASSED.txt', style_status_tokens=False),
                '\033[31m[TESTS]\033[0m failed /tmp/PASSED.txt',
            )
            self.assertEqual(
                style_text('[GIT] operation failed', style_status_tokens=False),
                '\033[31m[GIT]\033[0m operation failed',
            )
            self.assertEqual(
                style_text('[RESULT] APPROVED', style_status_tokens=False),
                '\033[32m[RESULT]\033[0m APPROVED',
            )
            self.assertEqual(
                style_text('[RESULT] BLOCKED', style_status_tokens=False),
                '\033[31m[RESULT]\033[0m BLOCKED',
            )

    def test_style_text_colors_padded_bracketed_warning_and_failure_statuses(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_text('[WARN ] dependency needs attention\n[FAIL ] command output')
        self.assertEqual(
            styled,
            '\033[33m[WARN ]\033[0m dependency needs attention\n'
            '\033[31m[FAIL ]\033[0m command output',
        )

    def test_style_text_can_skip_status_tokens_for_streamed_child_output(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_text(
                '[00:30:10] [TESTS] ERROR PASSED /tmp/traceback.py',
                style_status_tokens=False,
            )
        self.assertEqual(
            styled,
            '\033[90m[00:30:10]\033[0m '\
            '\033[31m[TESTS]\033[0m ERROR PASSED /tmp/traceback.py',
        )

    def test_successful_provider_completions_are_transient_but_failures_are_not(self):
        for line in (
            '[DEVELOPER] Agent finished its turn (12 input/output tokens)',
            '[DEVELOPER] Command finished (exit 0)',
            '[REVIEW] Gemini agent finished',
        ):
            with self.subTest(line=line):
                self.assertTrue(is_transient_progress(line))

        for line in (
            '[DEVELOPER] Command finished (exit 1)',
            '[DEVELOPER] Agent: provider says PASSED',
        ):
            with self.subTest(line=line):
                self.assertFalse(is_transient_progress(line))

    def test_style_text_preserves_bounded_status_and_incoming_ansi(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_text('[TESTS] \033[32mPASSED\033[0m /tmp/PASSED.txt')
        self.assertEqual(
            styled,
            '\033[32m[TESTS]\033[0m \033[32mPASSED\033[0m /tmp/PASSED.txt',
        )

    def test_native_provider_processes_streams_and_records_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'fake_provider.py'
            script.write_text(
                'import json, sys\n'
                'provider = sys.argv[1]\n'
                'if provider == "copilot":\n'
                '    print("copilot final", flush=True)\n'
                'else:\n'
                '    print(json.dumps({"type": "message", "content": "intermediate"}), flush=True)\n'
                '    print(json.dumps({"type": "result", "usage": {}, "result": {\n'
                '        "text": provider + " final",\n'
                '        "usage": {"input_tokens": 3, "output_tokens": 4}\n'
                '    }}), flush=True)\n',
                encoding='utf-8',
            )
            calls = []

            def fake_command(provider, prompt, *, readonly=False, model=None):
                calls.append((provider, prompt, readonly, model))
                return [sys.executable, str(script), provider], provider != 'copilot'

            with patch('agentic_execution.cli_provider_command', side_effect=fake_command):
                for provider in ('gemini', 'claude', 'copilot'):
                    with self.subTest(provider=provider):
                        output = root / f'{provider}.txt'
                        ok, message = agentic_execution.run_cli_provider(
                            root, 'review this patch', output=output,
                            provider=provider, readonly=True, model='test-model', timeout=10,
                            permission_policy={'read_files': True},
                        )
                        self.assertTrue(ok)
                        self.assertEqual(message, f'{provider} final')
                        self.assertEqual(output.read_text(encoding='utf-8'), message)

                        raw_path = output.with_suffix('.jsonl')
                        if provider == 'copilot':
                            self.assertFalse(raw_path.exists())
                        else:
                            self.assertIn('input_tokens', raw_path.read_text(encoding='utf-8'))

                        run_record = json.loads((root / 'run.json').read_text(encoding='utf-8'))
                        invocation = run_record['usage_by_invocation'][-1]
                        self.assertEqual(invocation['role'], 'reviewer')
                        self.assertEqual(invocation['model'], 'test-model')
                        self.assertEqual(invocation['prompt_chars'], len('review this patch'))
                        if provider == 'copilot':
                            self.assertIsNone(invocation['total_tokens'])
                        else:
                            self.assertEqual(invocation['total_tokens'], 7)

            self.assertEqual(
                calls,
                [
                    ('gemini', 'review this patch', True, 'test-model'),
                    ('claude', 'review this patch', True, 'test-model'),
                    ('copilot', 'review this patch', True, 'test-model'),
                ],
            )

    def test_codex_events_become_readable_progress(self):
        event = json.dumps({'type': 'item.started', 'item': {
            'type': 'command_execution', 'command': 'python -m pytest -q'}})
        self.assertIn('Running command: python -m pytest -q', agentic_execution._codex_progress(event))
        event = json.dumps({'type': 'item.completed', 'item': {
            'type': 'file_change', 'changes': [{'path': 'src/invoice.py'}]}})
        self.assertIn('src/invoice.py', agentic_execution._codex_progress(event))
        self.assertIsNone(agentic_execution._codex_progress(json.dumps({'type': 'item.updated',
                                                             'item': {'type': 'reasoning'}})))

    def test_streamed_codex_json_saves_logs_and_shows_progress(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            log = root / 'developer-001.log'
            script = ('import json,sys; '
                      'print(json.dumps({"type":"thread.started"}),flush=True); '
                      'print(json.dumps({"type":"turn.completed"}),flush=True); '
                      'print(json.dumps({"type":"item.started","item":'
                      '{"type":"command_execution","command":"ls tests"}}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"file_change","changes":[{"path":"a.py"}]}}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"file_change","changes":[{"path":"b.py"}]}}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"command_execution","exit_code":0}}),flush=True)')
            with patch('sys.stdout', new_callable=StringIO) as out:
                result = agentic.streamed_command([sys.executable, '-c', script], root,
                                                  label='DEVELOPER', log_file=log,
                                                  timeout=10, codex_json=True,
                                                  metrics_folder=root)
            self.assertEqual(result.returncode, 0)
            self.assertIn('Running command: ls tests', out.getvalue())
            human = log.read_text(encoding='utf-8')
            self.assertNotIn('Codex session started', human)
            self.assertNotIn('Agent finished its turn', human)
            self.assertNotIn('Command finished (exit 0)', human)
            self.assertEqual(human.count('Modified files:'), 1)
            raw = log.with_suffix('.jsonl').read_text(encoding='utf-8')
            self.assertIn('item.started', raw)
            self.assertIn('thread.started', raw)
            metrics = json.loads((root / 'run.json').read_text())['metrics']
            self.assertEqual(metrics['operations'][0]['role'], 'developer')
            self.assertEqual(metrics['operations'][0]['status'], 'completed')
            self.assertGreaterEqual(metrics['operations'][0]['duration_seconds'], 0)

    def test_streamed_codex_json_bounds_raw_log_and_marks_dropped_output(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            log = root / 'developer-oversized.log'
            script = (
                'import json; '
                '[(print(json.dumps({"type":"item.completed","item":{"type":"command_execution",'
                '"command":"' + ('x' * 120) + '"}}), flush=True)) for _ in range(20000)]'
            )
            with patch('sys.stdout', new_callable=StringIO):
                result = agentic.streamed_command(
                    [sys.executable, '-c', script], root, label='DEVELOPER',
                    log_file=log, timeout=20, codex_json=True,
                )
            self.assertEqual(result.returncode, 0)
            raw = log.with_suffix('.jsonl').read_text(encoding='utf-8')
            self.assertLessEqual(
                len(raw.encode('utf-8')),
                MAX_RAW_LOG_BYTES,
            )
            self.assertIn('agentic_stream_truncated', raw)
            self.assertIn('Output truncated', log.read_text(encoding='utf-8'))

    def test_streamed_human_log_is_bounded_and_marks_truncation(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname, patch.object(
            execution_stream, 'MAX_HUMAN_LOG_BYTES', 1024
        ):
            root = Path(dirname)
            log = root / 'developer-human-oversized.log'
            script = 'for _ in range(20): print("x" * 500, flush=True)'
            with patch('sys.stdout', new_callable=StringIO):
                result = agentic.streamed_command(
                    [sys.executable, '-c', script], root, label='DEVELOPER',
                    log_file=log, timeout=10,
                )
            self.assertEqual(result.returncode, 0)
            human = log.read_text(encoding='utf-8')
            self.assertLessEqual(log.stat().st_size, 1024)
            self.assertIn('agentic_stream_truncated', human)

    def test_test_runner_does_not_persist_literal_argv_tokens(self):
        from io import StringIO
        token = 'literal-credential-that-is-not-patterned-123456'
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            sh('git', 'init', cwd=root)
            command = [sys.executable, '-c', 'pass', token]
            with patch('sys.stdout', new_callable=StringIO) as output:
                passed, report = agentic.run_tests(root, [command], folder=root)
            self.assertTrue(passed)
            self.assertNotIn(token, output.getvalue())
            self.assertNotIn(token, report)
            for path in root.glob('test-command-*'):
                self.assertNotIn(token, path.read_text(encoding='utf-8'))

    def test_test_runner_removes_configured_credentials_from_child_environment(self):
        from io import StringIO
        value = 'custom-test-credential-123456'
        git_executable = shutil.which('git') or 'git'
        with tempfile.TemporaryDirectory() as dirname, patch.dict(
            'os.environ', {'SERVICE_VALUE': value}, clear=True
        ):
            root = Path(dirname)
            sh(git_executable, 'init', cwd=root)
            script = 'import os; print(os.environ.get("SERVICE_VALUE", "missing"))'
            with patch('sys.stdout', new_callable=StringIO) as output:
                passed, report = agentic.run_tests(
                    root,
                    [[sys.executable, '-c', script]],
                    folder=root,
                    credential_names=['SERVICE_VALUE'],
                )
            self.assertTrue(passed)
            self.assertIn('missing', output.getvalue())
            self.assertIn('missing', report)
            self.assertNotIn(value, output.getvalue())
            self.assertNotIn(value, report)

    def test_verbose_streamed_codex_log_keeps_routine_progress(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            log = root / 'developer.log'
            script = ('import json; '
                      'print(json.dumps({"type":"thread.started"}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"command_execution","exit_code":0}}),flush=True)')
            with patch('sys.stdout', new_callable=StringIO):
                result = agentic.streamed_command(
                    [sys.executable, '-c', script], root,
                    label='DEVELOPER', log_file=log, timeout=10,
                    codex_json=True, verbose=True,
                )
            self.assertEqual(result.returncode, 0)
            human = log.read_text(encoding='utf-8')
            self.assertIn('Codex session started', human)
            self.assertIn('Command finished (exit 0)', human)

    def test_streamed_progress_keeps_failure_and_timeout_diagnostics(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            script = ('import json; '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"command_execution","exit_code":3}}),flush=True); '
                      'print(json.dumps({"type":"error","message":"provider failed"}),flush=True)')
            result = agentic.streamed_command(
                [sys.executable, '-c', script], root,
                label='DEVELOPER', log_file=root / 'developer.log',
                timeout=10, codex_json=True,
            )
            self.assertEqual(result.returncode, 0)
            human = (root / 'developer.log').read_text(encoding='utf-8')
            self.assertIn('Command finished (exit 3)', human)
            self.assertIn('ERROR: provider failed', human)

    def test_tty_codex_progress_colors_generated_status_tokens(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            script = ('import json,sys; '
                      'print(json.dumps({"type":"error","message":'
                      '"provider detail PASSED APPROVED FAILED /tmp/result.txt"}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"agent_message","text":'
                      '"provider says PASSED APPROVED FAILED"}}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"command_execution","exit_code":1}}),flush=True)')
            with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
                 patch('sys.stdout', new=TTYBuffer()) as out:
                result = agentic.streamed_command(
                    [sys.executable, '-c', script], root,
                    label='DEVELOPER', log_file=root / 'developer.log',
                    timeout=10, codex_json=True,
                )

            self.assertEqual(result.returncode, 0)
            rendered = out.getvalue()
            self.assertIn(
                '\033[31mERROR\033[0m: provider detail PASSED APPROVED FAILED /tmp/result.txt',
                rendered,
            )
            self.assertIn(
                '\033[36m[DEVELOPER]\033[0m Agent: provider says PASSED APPROVED FAILED',
                rendered,
            )
            self.assertNotIn('\033[32mPASSED\033[0m', rendered)
            self.assertNotIn('\033[32mAPPROVED\033[0m', rendered)
            self.assertNotIn('\033[31mFAILED\033[0m', rendered)
            self.assertIn(
                'Command \033[32mfinished\033[0m (\033[31mexit 1\033[0m)',
                rendered,
            )
            self.assertNotIn('\033[36m[DEVELOPER] ERROR', rendered)

    def test_tty_streaming_colors_terminal_but_keeps_human_log_plain(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), patch('sys.stdout', new=TTYBuffer()) as out:
                result = agentic.streamed_command(
                    [sys.executable, '-c', 'print("\\033[31m[TESTS] FAILED\\033[0m")'],
                    root, label='TESTS', log_file=root / 'tests.log', timeout=10,
                )
            self.assertEqual(result.returncode, 0)
            self.assertIn('\033[', out.getvalue())
            self.assertNotIn('\033[', (root / 'tests.log').read_text(encoding='utf-8'))
            self.assertIn('[TESTS] FAILED', (root / 'tests.log').read_text(encoding='utf-8'))

    def test_streamed_child_status_words_keep_terminal_default_color(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
                 patch('sys.stdout', new=TTYBuffer()) as out:
                result = agentic.streamed_command(
                    [sys.executable, '-c', 'print("ERROR PASSED /tmp/traceback.py")'],
                    root, label='TESTS', log_file=root / 'tests.log', timeout=10,
                )
            self.assertEqual(result.returncode, 0)
            rendered = out.getvalue()
            self.assertIn('\033[31m[TESTS]\033[0m ERROR PASSED /tmp/traceback.py', rendered)
            self.assertNotIn('\033[31mERROR\033[0m', rendered)
            self.assertNotIn('\033[32mPASSED\033[0m', rendered)

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

    def test_tty_streaming_replaces_transient_progress_but_keeps_it_in_log(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            script = 'import time; time.sleep(1.2); print("ok", flush=True)'
            with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
                 patch('sys.stdout', new=TTYBuffer()) as out:
                result = agentic.streamed_command(
                    [sys.executable, '-c', script], root, label='TESTS',
                    log_file=root / 'tests.log', timeout=10, heartbeat=0,
                )
            self.assertEqual(result.returncode, 0)
            self.assertIn('\r\033[2K', out.getvalue())
            self.assertEqual(
                (root / 'tests.log').read_text(encoding='utf-8').count('Still running'),
                1,
            )

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

    def test_test_runner_checks_untracked_files_for_whitespace(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            sh('git', 'init', cwd=root)
            (root / 'new.py').write_text('value = 1\n\n')

            passed, report = agentic.run_tests(
                root, [[sys.executable, '-c', 'print("ok")']]
            )

            self.assertTrue(passed)
            self.assertIn('ok', report)
            self.assertEqual((root / 'new.py').read_bytes(), b'value = 1\n')

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'no no-follow open primitive')
    def test_eof_repair_rejects_final_symlink_replacement(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname) / 'worktree'
            root.mkdir()
            target = root / 'new.py'
            outside = Path(dirname) / 'outside.py'
            target.write_bytes(b'value = 1\n\n')
            outside.write_bytes(b'outside\n')
            original_open = agentic_workflow.os.open

            def replace_before_final_open(path, flags, mode=0o777, *, dir_fd=None):
                if dir_fd is not None and path == 'new.py':
                    target.unlink()
                    target.symlink_to(outside)
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with patch.object(agentic_workflow.os, 'open', side_effect=replace_before_final_open):
                repaired = agentic_workflow._repair_eof_blank_lines(
                    root, 'new.py:2: new blank line at EOF.'
                )

            self.assertEqual(repaired, [])
            self.assertEqual(outside.read_bytes(), b'outside\n')
            self.assertTrue(target.is_symlink())

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'no no-follow open primitive')
    def test_eof_repair_preserves_single_crlf_terminator(self):
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            target = root / 'new.txt'
            target.write_bytes(b'value\r\n\r\n')

            repaired = agentic_workflow._repair_eof_blank_lines(
                root, 'new.txt:2: new blank line at EOF.'
            )

            self.assertEqual(repaired, ['new.txt'])
            self.assertEqual(target.read_bytes(), b'value\r\n')

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

    def test_cli_provider_uses_provider_defaults_and_explicit_policy_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (patch.object(agentic_execution, 'cli_provider_command', return_value=(['provider'], False)) as build,
                  patch.object(
                      agentic_execution, 'streamed_command',
                      return_value=CompletedProcess(['provider'], 0, stdout='done'),
                  )):
                agentic_execution.run_cli_provider(
                    root, 'prompt', output=root / 'default.txt', provider='gemini',
                )
                self.assertNotIn('permission_policy', build.call_args.kwargs)
                override = {'run_checks': False}
                agentic_execution.run_cli_provider(
                    root, 'prompt', output=root / 'override.txt', provider='gemini',
                    permission_policy=override,
                )
                self.assertIs(build.call_args.kwargs['permission_policy'], override)

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

    def test_stream_timeout_terminates_descendants(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as dirname:
            root = Path(dirname)
            marker = root / 'descendant-alive'
            child = (
                'import pathlib,time; time.sleep(0.5); '
                f'pathlib.Path({str(marker)!r}).write_text("alive")'
            )
            parent = (
                'import subprocess,sys,time; '
                f'subprocess.Popen([sys.executable, "-c", {child!r}]); time.sleep(10)'
            )
            with patch('sys.stdout', new_callable=StringIO):
                result = agentic.streamed_command(
                    [sys.executable, '-c', parent], root, label='TESTS',
                    log_file=root / 'tests.log', timeout=1, heartbeat=0,
                )
            time.sleep(0.8)
        self.assertEqual(result.returncode, 124)
        self.assertFalse(marker.exists())

