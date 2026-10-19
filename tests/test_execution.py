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
from agentic_output import style_text


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class StreamingTests(unittest.TestCase):
    def test_status_styling_is_tty_only_and_honors_no_color(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), patch('sys.stdout', new=TTYBuffer()):
            self.assertIn('\033[32m', style_text('[TESTS] PASSED'))
        with patch.dict('os.environ', {'NO_COLOR': '1'}), patch('sys.stdout', new=TTYBuffer()):
            self.assertNotIn('\033[', style_text('[TESTS] PASSED'))
        self.assertNotIn('\033[', style_text('[TESTS] PASSED'))

    def test_style_text_marks_commands_and_file_changes(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with patch.dict('os.environ', {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            styled = style_text('[TESTS] Running: python -m unittest\n+added.py\n-removed.py')
        self.assertIn('\033[36m[TESTS] Running:', styled)
        self.assertIn('\033[32m+added.py', styled)
        self.assertIn('\033[31m-removed.py', styled)

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

