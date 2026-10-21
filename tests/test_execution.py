"""Streaming and command-runner tests."""
import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import agentic
import agentic_execution
from agentic_output import is_transient_progress, style_patch_summary, style_system_text, style_text


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
            'CHAT': '96', 'COST': '90', 'NO_CHANGES': '33', 'CANCEL': '33',
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

    def test_style_system_text_styles_exact_unbracketed_prompts_only(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_system_text(
                'chat> ',
                system_prefix='CHAT',
                system_prefix_text='chat>',
                style_status_tokens=False,
            )
            user_text = style_system_text(
                'user text containing chat> ',
                system_prefix='CHAT',
                system_prefix_text='chat>',
                style_status_tokens=False,
            )
        self.assertEqual(styled, '\033[96mchat>\033[0m ')
        self.assertEqual(user_text, 'user text containing chat> ')

    def test_style_system_text_preserves_wrapping_and_plain_output(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        message = '[CHAT] Agent response:\n[REVIEW] body\nnext line\n'
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_system_text(message, system_prefix='CHAT', style_status_tokens=False)
        self.assertEqual(
            styled,
            '\033[96m[CHAT]\033[0m Agent response:\n[REVIEW] body\nnext line\n',
        )

        with patch.dict('os.environ', {'NO_COLOR': '1'}):
            self.assertEqual(
                style_system_text(message, system_prefix='CHAT'),
                message,
            )

    def test_chat_output_styles_category_without_recoloring_response(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        output = TTYBuffer()
        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=output):
            agentic._chat_output('[CHAT] Agent response: FAILED\n[REVIEW] provider text')
        self.assertEqual(
            output.getvalue(),
            '\033[96m[CHAT]\033[0m Agent response: FAILED\n[REVIEW] provider text\n',
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
                      'print(json.dumps({"type":"item.started","item":'
                      '{"type":"command_execution","command":"ls tests"}}),flush=True); '
                      'print(json.dumps({"type":"item.completed","item":'
                      '{"type":"command_execution","exit_code":0}}),flush=True)')
            with patch('sys.stdout', new_callable=StringIO) as out:
                result = agentic.streamed_command([sys.executable, '-c', script], root,
                                                  label='DEVELOPER', log_file=log,
                                                  timeout=10, codex_json=True,
                                                  metrics_folder=root)
            self.assertEqual(result.returncode, 0)
            self.assertIn('Running command: ls tests', out.getvalue())
            self.assertIn('Command finished (exit 0)', log.read_text())
            self.assertIn('item.started', log.with_suffix('.jsonl').read_text())
            metrics = json.loads((root / 'run.json').read_text())['metrics']
            self.assertEqual(metrics['operations'][0]['role'], 'developer')
            self.assertEqual(metrics['operations'][0]['status'], 'completed')
            self.assertGreaterEqual(metrics['operations'][0]['duration_seconds'], 0)

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
            self.assertIn('Still running', (root / 'tests.log').read_text(encoding='utf-8'))

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

