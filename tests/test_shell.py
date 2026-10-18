import os
import sys
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic_shell
from agentic import state_paths


class ShellTests(unittest.TestCase):
    def test_startup_prints_available_commands(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as temp:
            output = StringIO()
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch('builtins.input', return_value='/quit'), redirect_stdout(output):
                self.assertEqual(shell.run(), 0)
        self.assertIn('A G E N T I C', output.getvalue())
        self.assertIn('Interactive issue-driven coding workflow', output.getvalue())
        self.assertIn('/run ISSUE', output.getvalue())
        self.assertIn('/ps', output.getvalue())
        self.assertIn('/logs JOB', output.getvalue())
        self.assertIn('/status', output.getvalue())

    def test_welcome_banner_does_not_emit_color_when_output_is_not_a_terminal(self):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            agentic_shell._welcome_banner(Path('/tmp/example'))
        self.assertNotIn('\033[', output.getvalue())

    def test_help_command_is_available(self):
        self.assertEqual(agentic_shell.build_parser().parse_args(['help']).command, 'help')

    def test_parse_run_command(self):
        self.assertEqual(agentic_shell._parse_job_command(['/run', '119']), ('119', []))
        self.assertEqual(agentic_shell._parse_job_command(['/run']), (None, []))
        self.assertEqual(
            agentic_shell._parse_job_command(['/run', '--publish']),
            (None, ['--publish']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command(['/run', '119', '--review']),
            ('119', ['--review']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command(['/run', '119', '--profile', 'slow']),
            ('119', ['--profile', 'slow']),
        )
        with self.assertRaises(ValueError):
            agentic_shell._parse_job_command(['/run', '119', '--review', '--publish'])
        with self.assertRaises(ValueError):
            agentic_shell._parse_job_command(['/run', '119', 'slow'])
        with self.assertRaises(ValueError):
            agentic_shell._parse_job_command(['/run', '119', '--profile', 'fast', '--profile', 'slow'])
        with self.assertRaises(ValueError):
            agentic_shell._parse_job_command(['/run', '119', '--profile', 'fast', 'slow'])
        with self.assertRaises(ValueError):
            agentic_shell._parse_job_command(['/run', '119', '--unknown'])

    def test_completion_suggests_commands_options_and_job_ids(self):
        self.assertEqual(
            agentic_shell._completion_candidates('/r'),
            ['/run'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/run --p'),
            ['--profile', '--publish'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/run --profile s'),
            ['slow'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/logs ', job_numbers=(2, 1)),
            ['1', '2'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/status '),
            ['--json'],
        )
        self.assertEqual(agentic_shell._completion_candidates('run '), [])

    def test_completion_replaces_only_the_current_token(self):
        self.assertEqual(
            agentic_shell._replace_current_token('/run --prof', '--profile'),
            '/run --profile',
        )
        self.assertEqual(
            agentic_shell._replace_current_token('/run ', '--review'),
            '/run --review',
        )

    def test_completion_items_include_descriptions(self):
        items = dict(agentic_shell._completion_items('/'))
        self.assertIn('/run', items)
        self.assertEqual(items['/run'], 'start a background job or local task')
        self.assertIn('/ps', items)
        self.assertEqual(items['/ps'], 'show running or persisted background jobs')
        self.assertIn('/dashboard', items)
        self.assertEqual(items['/dashboard'], 'generate and open the HTML dashboard')

        run_options = dict(agentic_shell._completion_items('/run --'))
        self.assertIn('--profile', run_options)
        self.assertIn('--review', run_options)
        self.assertIn('--publish', run_options)
        self.assertEqual(run_options['--profile'], 'execution profile (fast or slow)')

        profiles = dict(agentic_shell._completion_items('/run --profile '))
        self.assertEqual(profiles.get('fast'), 'run lightweight checks and fast model')
        self.assertEqual(profiles.get('slow'), 'run comprehensive checks and full reasoning')

    def test_interactive_input_tab_completes_candidate(self):
        keys = [('CHAR', '/'), ('CHAR', 'r'), ('TAB', '\t'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/run ')

    def test_interactive_input_enter_completes_partial_candidate(self):
        keys = [('CHAR', '/'), ('CHAR', 'r'), ('ENTER', '\n'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/run ')

    def test_interactive_input_arrow_navigation_selects_candidate(self):
        keys = [('CHAR', '/'), ('DOWN', ''), ('ENTER', '\r'), ('ENTER', '\r')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/ps')

    def test_interactive_input_escape_dismisses_dropdown(self):
        keys = [('CHAR', '/'), ('ESCAPE', '\x1b'), ('CHAR', 'q'), ('CHAR', 'u'),
                ('CHAR', 'i'), ('CHAR', 't'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/quit')

    def test_interactive_input_history_navigation(self):
        keys = [('UP', ''), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                history=['/ps', '/status'],
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/status')

    def test_interactive_input_line_editing_and_backspace(self):
        # Backspace: type /px, backspace, s -> /ps
        keys = [('CHAR', '/'), ('CHAR', 'p'), ('CHAR', 'x'), ('BACKSPACE', '\b'),
                ('CHAR', 's'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/ps')

        # Cursor movement: type /un, left, left, r, right, right -> /run
        keys = [('CHAR', '/'), ('CHAR', 'u'), ('CHAR', 'n'), ('LEFT', ''),
                ('LEFT', ''), ('CHAR', 'r'), ('RIGHT', ''), ('RIGHT', ''), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/run')

    def test_interactive_input_completion_preserves_suffix_and_cursor(self):
        keys = [
            ('CHAR', '/'), ('CHAR', 'r'), ('CHAR', ' '),
            ('CHAR', 'f'), ('CHAR', 'o'), ('CHAR', 'o'),
            ('LEFT', ''), ('LEFT', ''), ('LEFT', ''), ('LEFT', ''),
            ('DOWN', ''), ('ENTER', '\n'), ('CHAR', 'X'), ('ENTER', '\n'),
        ]
        reader = _create_mock_reader(keys)
        with patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, '/runX foo')

    def test_interactive_input_ctrl_c_and_ctrl_d(self):
        reader_c = _create_mock_reader([('CTRL_C', '\003')])
        with patch('sys.stdout', new_callable=StringIO):
            with self.assertRaises(KeyboardInterrupt):
                agentic_shell._interactive_input(
                    'agentic> ',
                    agentic_shell._completion_items,
                    key_reader_factory=reader_c,
                )

        reader_d = _create_mock_reader([('CTRL_D', '\004')])
        with patch('sys.stdout', new_callable=StringIO):
            with self.assertRaises(EOFError):
                agentic_shell._interactive_input(
                    'agentic> ',
                    agentic_shell._completion_items,
                    key_reader_factory=reader_d,
                )

    def test_interactive_terminal_detection(self):
        with patch.dict('os.environ', {'AGENTIC_PLAIN': '1'}):
            self.assertFalse(agentic_shell._is_interactive_terminal())

    def test_parse_ansi_sequence_handles_arrows_and_special_keys(self):
        cases = [
            (['[', 'A'], ('UP', '')),
            (['[', 'B'], ('DOWN', '')),
            (['[', 'C'], ('RIGHT', '')),
            (['[', 'D'], ('LEFT', '')),
            (['[', 'H'], ('HOME', '')),
            (['[', 'F'], ('END', '')),
            (['[', 'Z'], ('UP', '')),
            (['[', '3', '~'], ('DELETE', '')),
            (['O', 'A'], ('UP', '')),
            (['O', 'B'], ('DOWN', '')),
            ([None], ('ESCAPE', '\x1b')),
        ]
        for seq, expected in cases:
            iterator = iter(seq)
            result = agentic_shell._parse_ansi_sequence(lambda: next(iterator, None))
            self.assertEqual(result, expected)

    @unittest.skipUnless(os.name != 'nt', 'POSIX terminal behavior only')
    def test_posix_key_reader_does_not_leak_buffered_ansi_sequences(self):
        import io
        import pty

        master, slave = pty.openpty()
        raw = os.fdopen(slave, 'rb', buffering=0)
        stream = io.TextIOWrapper(raw, encoding='utf-8')
        try:
            with patch.object(agentic_shell.sys, 'stdin', stream):
                with agentic_shell._KeyReader() as reader:
                    os.write(master, b'\x1b[B')
                    self.assertEqual(reader.read_key(), ('DOWN', ''))
        finally:
            stream.close()
            os.close(master)



    def test_local_task_capture_writes_a_reusable_task_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text('{"task_source":"local"}')
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), config=config, session_dir=root / 'logs'
            )
            with patch('builtins.input', side_effect=['Add export support', 'Add tests', '/done']):
                task_file = shell._capture_local_task()
            self.assertEqual(task_file.read_text(), 'Add export support\nAdd tests\n')

    def test_shell_launches_and_collects_a_background_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            job = shell._launch([sys.executable, '-c', 'print("hello")'])
            job.process.wait(timeout=5)
            shell.refresh()
            self.assertFalse(job.running)
            self.assertEqual(job.exit_code, 0)
            self.assertIn('hello', job.log_path.read_text())
            self.assertIn('EXIT 0', _capture_ps(shell))

    def test_background_job_completion_callback_runs_without_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            completed = []
            ready = threading.Event()

            def callback(code):
                completed.append(code)
                ready.set()

            job = shell._launch(
                [sys.executable, '-c', 'print("hello")'],
                on_complete=callback,
            )
            self.assertTrue(ready.wait(timeout=5))
            job.process.wait(timeout=5)
            shell.refresh()
            self.assertEqual(completed, [0])

    def test_dashboard_opens_generated_output_after_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / 'state'
            shell = agentic_shell.AgenticShell(
                root, Path(__file__).resolve().parents[1] / 'agentic.py',
                state_dir=state_dir, session_dir=root / 'logs',
            )
            with (patch.object(shell, '_launch') as launch,
                  patch('agentic_shell._open_dashboard') as open_dashboard):
                shell.run_dashboard(['/dashboard'])
                argv = launch.call_args.args[0]
                output = Path(argv[argv.index('--output') + 1])
                self.assertEqual(output, state_paths(root, state_dir).dashboards / 'dashboard.html')
                self.assertEqual(argv[argv.index('--state-dir') + 1], str(state_dir.resolve()))
                callback = launch.call_args.kwargs['on_complete']
                callback(0)
                open_dashboard.assert_called_once_with(output)

                open_dashboard.reset_mock()
                callback(1)
                open_dashboard.assert_not_called()

    def test_job_records_are_visible_from_a_later_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_dir = root / 'logs'
            first = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=session_dir)
            job = first._launch([sys.executable, '-c', 'import time; print("persisted"); time.sleep(.3)'])
            second = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=session_dir)
            output = _capture_ps(second, ['/ps', '--all'])
            self.assertIn(f'{job.number}', output)
            self.assertIn('RUNNING', output)
            job.process.wait(timeout=5)
            first.refresh()
            self.assertIn('persisted', job.log_path.read_text())

    def test_shell_preserves_local_task_file_in_runner_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text('{"task_source":"local"}')
            task = root / 'logs' / 'task.md'
            task.parent.mkdir()
            task.write_text('Implement export\n')
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), config=config, session_dir=root / 'logs'
            )
            with (patch.object(shell, '_capture_local_task', return_value=task),
                  patch.object(shell, '_launch') as launch):
                shell.run_issue(['/run', '--profile', 'slow'])
            argv = launch.call_args.args[0]
            self.assertIn('--task-file', argv)
            self.assertIn(str(task), argv)
            self.assertIn('--profile', argv)
            self.assertIn('slow', argv)

    def test_shell_stop_and_log_tail_handle_running_and_missing_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            job = shell._launch([sys.executable, '-c', 'import time; time.sleep(5)'])
            with patch('sys.stdout'):
                shell.stop(['/stop', str(job.number)])
            job.process.wait(timeout=5)
            shell.refresh()
            self.assertIsNotNone(job.exit_code)
            self.assertIn('Could not read', agentic_shell._tail(root / 'missing.log', 5))
            with self.assertRaisesRegex(ValueError, 'unknown job'):
                shell.show_logs(['/logs', '999'])

    def test_shell_ignores_malformed_persisted_job_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session = root / 'logs'
            session.mkdir()
            (session / 'job-001.json').write_text('{not-json')
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=session)
            self.assertEqual(shell.jobs, {})


def _capture_ps(shell, parts=None):
    from contextlib import redirect_stdout
    from io import StringIO

    output = StringIO()
    with redirect_stdout(output):
        shell.show_processes(parts)
    return output.getvalue()


class _MockKeyReader:
    def __init__(self, keys):
        self.keys = list(keys)
        self.idx = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read_key(self):
        if self.idx < len(self.keys):
            key = self.keys[self.idx]
            self.idx += 1
            return key
        return ('ENTER', '\n')


def _create_mock_reader(keys):
    return lambda: _MockKeyReader(keys)


if __name__ == '__main__':
    unittest.main()
