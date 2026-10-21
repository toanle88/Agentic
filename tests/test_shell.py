import os
import subprocess
import sys
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic
import agentic_shell
import agentic_shell_input
from agentic import state_paths


class ShellTests(unittest.TestCase):
    @unittest.skipUnless(os.name != 'nt', 'POSIX process-group behavior only')
    def test_foreground_ctrl_c_from_pty_reaches_child_without_interrupting_shell(self):
        import io
        import pty
        import termios
        import time

        master, slave = pty.openpty()
        raw_slave = os.fdopen(slave, 'rb', buffering=0)
        terminal_input = io.TextIOWrapper(raw_slave, encoding='utf-8')
        process = subprocess.Popen(
            [
                sys.executable,
                '-c',
                'import time; print("ready", flush=True); time.sleep(30)',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

        class Foreground:
            def set_input(self, _text, _cursor):
                pass

        forwarder = agentic_shell._ForegroundInputForwarder(process, Foreground())
        try:
            self.assertIsNotNone(process.stdout)
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with patch.object(agentic_shell_input.sys, 'stdin', terminal_input):
                forwarder.start()
                deadline = time.monotonic() + 3
                while forwarder.reader._posix_fd is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                fd = forwarder.reader._posix_fd
                if fd is None:
                    self.fail('foreground reader did not open the PTY')
                while (
                    termios.tcgetattr(fd)[3] & termios.ISIG
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertFalse(termios.tcgetattr(fd)[3] & termios.ISIG)
                os.write(master, b'\003')
                deadline = time.monotonic() + 3
                while forwarder.cancel_reason is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                forwarder.stop()
            self.assertEqual(forwarder.cancel_reason, 'Ctrl-C')
            process.wait(timeout=3)
        finally:
            forwarder.stop()
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), agentic_shell.signal.SIGKILL)
                process.wait(timeout=3)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            terminal_input.close()
            os.close(master)

    @unittest.skipUnless(os.name != 'nt', 'POSIX process-group behavior only')
    def test_foreground_cancel_signals_descendants_in_child_process_group(self):
        import time

        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'terminated'
            ready = Path(temp) / 'ready'
            descendant_code = (
                'import signal, time\n'
                'def stop(_signum, _frame):\n'
                f'    open({str(marker)!r}, "w").write("terminated")\n'
                '    raise SystemExit\n'
                'signal.signal(signal.SIGTERM, stop)\n'
                f'open({str(ready)!r}, "w").write("ready")\n'
                'time.sleep(30)\n'
            )
            parent_code = (
                'import os, subprocess, sys, time\n'
                'descendant = subprocess.Popen([sys.executable, "-c", sys.argv[1]])\n'
                'while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n'
                'print(descendant.pid, flush=True)\n'
                'time.sleep(30)\n'
            )
            process = subprocess.Popen(
                [sys.executable, '-c', parent_code, descendant_code, str(ready)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )

            class Foreground:
                def set_input(self, _text, _cursor):
                    pass

            forwarder = agentic_shell._ForegroundInputForwarder(process, Foreground())
            try:
                descendant_pid = int(process.stdout.readline())
                forwarder._cancel('/stop', agentic_shell.signal.SIGTERM)
                process.wait(timeout=3)
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                self.assertNotEqual(descendant_pid, process.pid)
            finally:
                forwarder.stop()
                if process.poll() is None:
                    os.killpg(os.getpgid(process.pid), agentic_shell.signal.SIGKILL)
                    process.wait(timeout=3)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stdin is not None:
                    process.stdin.close()

    def test_foreground_input_forwarder_sends_publish_line_to_child(self):
        class Reader:
            _posix_fd = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if not hasattr(self, 'keys'):
                    self.keys = iter(
                        [('CHAR', char) for char in 'publish']
                        + [('ENTER', '\n'), ('STOP', '')]
                    )
                return next(self.keys)

        class Foreground:
            def __init__(self):
                self.inputs = []

            def commit_prompt(self):
                pass

            def set_input(self, text, cursor):
                self.inputs.append((text, cursor))

        stdin = StringIO()
        process = type('Process', (), {'stdin': stdin})()
        forwarder = agentic_shell._ForegroundInputForwarder(process, Foreground())
        forwarder.reader = Reader()
        with patch.object(forwarder, '_set_noecho'):
            forwarder._run()

        self.assertEqual(stdin.getvalue(), 'publish\n')

    def test_foreground_input_forwarder_ignores_stale_empty_enter_before_prompt(self):
        class Reader:
            _posix_fd = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if not hasattr(self, 'keys'):
                    self.keys = iter([('ENTER', '\n'), ('STOP', '')])
                return next(self.keys)

        class Foreground:
            prompt = ''

            def commit_prompt(self):
                pass

            def set_input(self, text, cursor):
                pass

        stdin = StringIO()
        process = type('Process', (), {'stdin': stdin})()
        forwarder = agentic_shell._ForegroundInputForwarder(process, Foreground())
        forwarder.reader = Reader()
        with patch.object(forwarder, '_set_noecho'):
            forwarder._run()

        self.assertEqual(stdin.getvalue(), '')

    def test_foreground_input_forwarder_consumes_all_arrow_directions(self):
        class Reader:
            _posix_fd = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if not hasattr(self, 'keys'):
                    self.keys = iter(
                        [('CHAR', char) for char in 'abcd']
                        + [
                            ('LEFT', ''), ('UP', ''), ('RIGHT', ''), ('DOWN', ''),
                            ('HOME', ''), ('RIGHT', ''), ('DELETE', ''), ('END', ''),
                            ('ENTER', '\n'), ('STOP', ''),
                        ]
                    )
                return next(self.keys)

        class Foreground:
            def __init__(self):
                self.inputs = []

            def commit_prompt(self):
                pass

            def set_input(self, text, cursor):
                self.inputs.append((text, cursor))

        stdin = StringIO()
        foreground = Foreground()
        process = type('Process', (), {'stdin': stdin})()
        forwarder = agentic_shell._ForegroundInputForwarder(process, foreground)
        forwarder.reader = Reader()
        with patch.object(forwarder, '_set_noecho'):
            forwarder._run()

        self.assertEqual(stdin.getvalue(), 'acd\n')
        self.assertIn(('acd', 3), foreground.inputs)
        self.assertIsNone(forwarder.cancel_reason)

    def test_foreground_input_forwarder_stop_cancels_without_forwarding(self):
        class Reader:
            _posix_fd = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if not hasattr(self, 'keys'):
                    self.keys = iter(
                        [('CHAR', char) for char in '/stop'] + [('ENTER', '\n')]
                    )
                return next(self.keys)

        class Foreground:
            def commit_prompt(self):
                pass

            def set_input(self, text, cursor):
                pass

        class Process:
            stdin = StringIO()

            def __init__(self):
                self.signals = []

            def send_signal(self, signum):
                self.signals.append(signum)

        process = Process()
        forwarder = agentic_shell._ForegroundInputForwarder(process, Foreground())
        forwarder.reader = Reader()
        with patch.object(forwarder, '_set_noecho'):
            forwarder._run()

        self.assertEqual(forwarder.cancel_reason, '/stop')
        self.assertEqual(process.signals, [agentic_shell.signal.SIGTERM])
        self.assertEqual(process.stdin.getvalue(), '')

    def test_foreground_input_forwarder_ctrl_c_cancels_child(self):
        class Reader:
            _posix_fd = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                return 'CTRL_C', '\003'

        class Foreground:
            def set_input(self, text, cursor):
                pass

        class Process:
            stdin = StringIO()

            def __init__(self):
                self.signals = []

            def send_signal(self, signum):
                self.signals.append(signum)

        process = Process()
        forwarder = agentic_shell._ForegroundInputForwarder(process, Foreground())
        forwarder.reader = Reader()
        with patch.object(forwarder, '_set_noecho'):
            forwarder._run()

        self.assertEqual(forwarder.cancel_reason, 'Ctrl-C')
        self.assertEqual(process.signals, [agentic_shell.signal.SIGINT])

    def test_foreground_input_forwarder_scrolls_on_page_keys(self):
        class Reader:
            _posix_fd = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if not hasattr(self, 'keys'):
                    self.keys = iter([
                        ('PAGE_UP', ''), ('PAGE_DOWN', ''), ('CTRL_D', ''),
                    ])
                return next(self.keys)

        class Foreground:
            def __init__(self):
                self.scrolls = []

            def scroll(self, delta):
                self.scrolls.append(delta)

            def set_input(self, text, cursor):
                pass

            def refresh(self):
                pass

        process = type('Process', (), {'stdin': StringIO()})()
        foreground = Foreground()
        forwarder = agentic_shell._ForegroundInputForwarder(process, foreground)
        forwarder.reader = Reader()
        with patch.object(forwarder, '_set_noecho'):
            forwarder._run()

        self.assertEqual(foreground.scrolls, [1, -1])

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
        self.assertIn(f'v{agentic_shell.__version__}', output.getvalue())
        self.assertIn('Interactive issue-driven coding workflow', output.getvalue())
        self.assertIn('/run ISSUE', output.getvalue())
        self.assertIn('/ps', output.getvalue())
        self.assertIn('/logs JOB', output.getvalue())
        self.assertIn('/list', output.getvalue())
        self.assertIn('/runs', output.getvalue())
        self.assertIn('/doctor', output.getvalue())
        self.assertIn('/cleanup', output.getvalue())
        self.assertIn('/status', output.getvalue())

    def test_run_retains_startup_help_in_interactive_output_viewport(self):
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as temp:
            state = agentic_shell_input._InteractiveOutputState()
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            output = StringIO()

            def quit_shell(_prompt, _status_line):
                self.assertIs(shell._interactive_output_state, state)
                return '/quit'

            with patch('agentic_shell._is_interactive_terminal', return_value=True), \
                 patch('agentic_shell._InteractiveOutputState', return_value=state), \
                 patch.object(shell, '_input', side_effect=quit_shell), \
                 redirect_stdout(output):
                self.assertEqual(shell.run(), 0)

        self.assertIn('A G E N T I C', ''.join(state.history))
        self.assertIn('Commands:', ''.join(state.history))
        self.assertIn('/help', ''.join(state.history))


    def test_welcome_banner_does_not_emit_color_when_output_is_not_a_terminal(self):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            agentic_shell._welcome_banner(Path('/tmp/example'))
        self.assertNotIn('\033[', output.getvalue())


    def test_help_does_not_emit_color_when_output_is_not_a_terminal(self):
        from contextlib import redirect_stdout

        output = StringIO()
        with redirect_stdout(output):
            agentic_shell._print_help()
        self.assertIn('Run', output.getvalue())
        self.assertIn('/dashboard [OUTPUT]', output.getvalue())
        self.assertIn('/list', output.getvalue())
        self.assertIn('/runs [--json]', output.getvalue())
        self.assertIn('/stop JOB|--all', output.getvalue())
        self.assertIn('/init [OPTIONS]', output.getvalue())
        self.assertNotIn('\033[', output.getvalue())


    def test_help_uses_color_on_a_terminal(self):
        from contextlib import redirect_stdout

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        output = TTYBuffer()
        with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), redirect_stdout(output):
            agentic_shell._print_help()
        self.assertIn('\033[1;36mCommands:', output.getvalue())
        self.assertIn('\033[1;37m/run', output.getvalue())


    def test_log_replay_colors_statuses_only_on_a_terminal(self):
        from contextlib import redirect_stdout

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            job = shell._launch([sys.executable, '-c', 'print("[TESTS] PASSED")'])
            job.process.wait(timeout=5)
            shell.refresh()

            colored = TTYBuffer()
            with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), redirect_stdout(colored):
                shell.show_logs(['/logs', str(job.number), '5'])
            self.assertIn('\033[32m[TESTS]\033[0m PASSED', colored.getvalue())
            self.assertNotIn('\033[32mPASSED\033[0m', colored.getvalue())

            plain = StringIO()
            with redirect_stdout(plain):
                shell.show_logs(['/logs', str(job.number), '5'])
            self.assertNotIn('\033[', plain.getvalue())
            self.assertIn('[TESTS] PASSED', plain.getvalue())


    def test_log_replay_colors_patch_diff_stat_only_on_a_terminal(self):
        from contextlib import redirect_stdout

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        summary = (
            '[PATCH] Proposed changes:\n'
            ' src/App.py | 4 ++--\n'
            ' 1 file changed, 2 insertions(+), 2 deletions(-)\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            job = shell._launch([
                sys.executable,
                '-c',
                f'import sys; sys.stdout.write({summary!r})',
            ])
            job.process.wait(timeout=5)
            shell.refresh()

            colored = TTYBuffer()
            with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), redirect_stdout(colored):
                shell.show_logs(['/logs', str(job.number), '5'])
            rendered = colored.getvalue()
            self.assertIn('\033[95m[PATCH]\033[0m Proposed changes:', rendered)
            self.assertIn('\033[32m++\033[0m\033[31m--\033[0m', rendered)
            self.assertIn('\033[32m2 insertions(+)\033[0m', rendered)
            self.assertIn('\033[31m2 deletions(-)\033[0m', rendered)

            plain = StringIO()
            with redirect_stdout(plain):
                shell.show_logs(['/logs', str(job.number), '5'])
            self.assertNotIn('\033[', plain.getvalue())
            self.assertIn(summary, plain.getvalue())


    def test_runner_command_forwarding_uses_terminal_styling(self):
        from contextlib import redirect_stdout
        from subprocess import CompletedProcess

        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            output = TTYBuffer()
            with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
                 patch('agentic_shell.subprocess.run', return_value=CompletedProcess(
                     ['agentic'], 0, stdout='[TESTS] PASSED\n', stderr=''
                 )), redirect_stdout(output):
                shell._run_runner_command(['agentic', 'doctor'])
            self.assertIn('\033[32m[TESTS]\033[0m \033[32mPASSED\033[0m', output.getvalue())


    def test_help_command_is_available(self):
        self.assertEqual(agentic_shell.build_parser().parse_args(['help']).command, 'help')

    def test_status_line_contains_model_repository_and_active_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text('{"developer":{"provider":"openrouter","model":"openai/test-model"}}')
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), config=config, session_dir=root / 'logs'
            )
            shell.active_task = 'issue #69'
            status = shell._status_line()
            self.assertIn('openrouter · openai/test-model', status)
            self.assertIn(str(root.resolve()), status)
            self.assertIn('task: issue #69', status)

    def test_status_line_filters_terminal_controls_from_dynamic_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text(
                '{"developer":{"provider":"openrouter\\u001b[31m\\u0007",'
                '"model":"model\\r\\nname"}}'
            )
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), config=config, session_dir=root / 'logs'
            )
            shell.active_task = 'task\x1b]0;spoof\x07\nnext'
            status = shell._status_line()
            self.assertNotRegex(status, r'[\x00-\x1f\x7f-\x9f]')
            self.assertNotIn('\x1b', status)
            self.assertIn('openrouter', status)
            self.assertIn('model', status)
            self.assertIn('name', status)
            self.assertIn('task next', status)

    def test_non_tty_input_does_not_emit_an_interactive_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch('agentic_shell._is_interactive_terminal', return_value=False), \
                 patch('builtins.input', return_value='/help') as read_input:
                self.assertEqual(shell._input('› ', 'model · path · task: idle'), '/help')
            read_input.assert_called_once_with()
            self.assertEqual(shell.terminal_mode, agentic_shell.TerminalMode.OUTPUT)

    def test_input_mode_is_command_ready_only_while_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            modes = []

            def read_input():
                modes.append(shell.terminal_mode)
                return '/quit'

            with patch('agentic_shell._is_interactive_terminal', return_value=False), \
                 patch('builtins.input', side_effect=read_input):
                shell._input('› ')

            self.assertEqual(modes, [agentic_shell.TerminalMode.COMMAND_READY])
            self.assertEqual(shell.terminal_mode, agentic_shell.TerminalMode.OUTPUT)

    def test_help_and_errors_restore_output_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch('agentic_shell._print_help'):
                self.assertTrue(shell.execute('/help'))
            self.assertEqual(shell.terminal_mode, agentic_shell.TerminalMode.OUTPUT)

            self.assertTrue(shell.execute('/unknown'))
            self.assertEqual(shell.terminal_mode, agentic_shell.TerminalMode.OUTPUT)

    def test_ctrl_c_cancels_input_without_leaving_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch.object(shell, '_input', side_effect=[KeyboardInterrupt, '/quit']), \
                 patch.object(agentic_shell, '_welcome_banner'), \
                 patch.object(agentic_shell, '_print_help'), \
                 patch('agentic_shell._shell_print') as shell_print:
                self.assertEqual(shell.run(), 0)
            shell_print.assert_any_call('Input cancelled')

    def test_execute_records_submitted_history_for_arrow_navigation(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch.object(shell, 'show_status'):
                shell.execute('/status')
                shell.execute('/status')
            self.assertEqual(shell.history, ['/status'])

    def test_removed_chat_command_is_not_dispatched(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch('agentic_shell._shell_print') as shell_print:
                self.assertTrue(shell.execute('/chat 69'))
            shell_print.assert_called_once_with("Unknown command: /chat. Use /help.")

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
            agentic_shell._parse_job_command(['/run', '119', '--verbose']),
            ('119', ['--verbose']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command(['/run', '119', '--profile', 'slow']),
            ('119', ['--profile', 'slow']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command([
                '/run', '119', '--instruction', 'Bump the version when complete',
            ]),
            ('119', ['--instruction', 'Bump the version when complete']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command([
                '/run', '119', '--instruction', '--preserve',
            ]),
            ('119', ['--instruction', '--preserve']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command([
                '/run', '119', '--instruction', '', '--review',
            ]),
            ('119', ['--instruction', '', '--review']),
        )
        for option in (
            '--profile', '--instruction', '--review', '--publish',
            '--mark-done', '--reason', '--new', '--task-file',
        ):
            actual_flag = '--publish' if option == '--review' else '--review'
            parsed = agentic_shell._parse_job_command([
                '/run', '119', '--instruction', option, actual_flag,
            ])
            self.assertEqual(
                parsed, ('119', ['--instruction', option, actual_flag]),
            )
            self.assertFalse(agentic_shell._has_job_flag(parsed[1], option))
        self.assertEqual(
            agentic_shell._parse_job_command(['/run', '--task-file', 'task.md']),
            (None, ['--task-file', 'task.md']),
        )
        self.assertEqual(
            agentic_shell._parse_job_command([
                '/run', '119', '--mark-done', '--reason', 'human reviewed',
            ]),
            ('119', ['--mark-done', '--reason', 'human reviewed']),
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


    def test_runner_subcommands_use_argv_and_shell_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'state'
            shell = agentic_shell.AgenticShell(
                root, Path(__file__).resolve().parents[1] / 'agentic.py',
                state_dir=state, session_dir=root / 'logs',
            )
            result = type('Result', (), {'returncode': 0, 'stdout': 'ok\n'})()
            with patch('agentic_shell.subprocess.run', return_value=result) as run:
                shell.show_runs(['/runs', '--json'])
                runs_argv = run.call_args.args[0]
                self.assertIn('runs', runs_argv)
                self.assertIn('--json', runs_argv)
                self.assertIn(str(state.resolve()), runs_argv)

                shell.run_init(['/init', '--provider', 'codex', '--force'])
                self.assertIn('init', run.call_args.args[0])
                self.assertIn('--provider', run.call_args.args[0])

                shell.run_doctor(['/doctor', '--json'])
                self.assertIn('doctor', run.call_args.args[0])
                self.assertIn('--json', run.call_args.args[0])

                shell.run_doctor([
                    '/doctor', '--commands', '--profile', 'slow',
                    '--worktree', str(root),
                ])
                doctor_argv = run.call_args.args[0]
                self.assertIn('--commands', doctor_argv)
                self.assertIn('--profile', doctor_argv)
                self.assertIn('slow', doctor_argv)
                self.assertIn('--worktree', doctor_argv)
                self.assertIn(str(root), doctor_argv)

                shell.run_cleanup(['/cleanup', '--issue', '12', '--yes'])
                cleanup_argv = run.call_args.args[0]
                self.assertIn('cleanup', cleanup_argv)
                self.assertIn('--issue', cleanup_argv)
                self.assertIn('12', cleanup_argv)

                shell.show_version(['/version'])
                self.assertEqual(run.call_args.args[0][-1], '--version')


    def test_run_accepts_existing_local_task_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'agent_config.json'
            config.write_text('{"task_source":"local"}')
            (root / 'nested').mkdir()
            task = root / 'nested' / '..' / 'task.md'
            task.write_text('Implement export\n')
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), config=config, session_dir=root / 'logs'
            )
            with (patch.object(shell, '_capture_local_task') as capture,
                  patch.object(shell, '_launch') as launch):
                shell.run_issue(['/run', '--task-file', str(task)])
            capture.assert_not_called()
            argv = launch.call_args.args[0]
            self.assertIn('--task-file', argv)
            self.assertIn(str(task.resolve()), argv)


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


    def test_show_processes_places_running_jobs_first(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            finished = shell._launch([sys.executable, '-c', 'print("finished")'])
            finished.process.wait(timeout=5)
            shell.refresh()
            running = shell._launch([sys.executable, '-c', 'import time; time.sleep(5)'])
            try:
                output = _capture_ps(shell)
                rows = [line for line in output.splitlines() if line.startswith(('1 ', '2 '))]
                self.assertEqual(len(rows), 2)
                self.assertTrue(rows[0].startswith(f'{running.number:<3}'))
                self.assertIn('RUNNING', rows[0])
                self.assertTrue(rows[1].startswith(f'{finished.number:<3}'))
                self.assertIn('EXIT 0', rows[1])
            finally:
                running.process.terminate()
                running.process.wait(timeout=5)
                shell.refresh()


    def test_logs_accepts_pid_and_follows_running_job(self):
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            job = shell._launch([
                sys.executable, '-c',
                'import time; print("first", flush=True); time.sleep(.15); print("second", flush=True)',
            ])
            output = StringIO()
            with redirect_stdout(output):
                shell.show_logs(['/logs', str(job.process.pid), '1'])

            job.process.wait(timeout=5)
            rendered = output.getvalue()
            self.assertIn('following live output', rendered)
            self.assertIn('first', rendered)
            self.assertIn('second', rendered)
            self.assertIn('full log:', rendered)
            self.assertEqual(shell.terminal_mode, agentic_shell.TerminalMode.OUTPUT)

    def test_logs_follow_uses_live_output_mode_until_follow_finishes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            job = shell._launch([
                sys.executable, '-c', 'import time; print("output", flush=True); time.sleep(.1)',
            ])
            transitions = []
            original_set_mode = shell._set_terminal_mode

            def record_mode(mode):
                transitions.append(mode)
                original_set_mode(mode)

            with patch.object(shell, '_set_terminal_mode', side_effect=record_mode):
                shell.show_logs(['/logs', str(job.number), '1'])

            job.process.wait(timeout=5)
            self.assertIn(agentic_shell.TerminalMode.LIVE_OUTPUT, transitions)
            self.assertEqual(transitions[-1], agentic_shell.TerminalMode.OUTPUT)


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


    def test_list_runs_runner_and_prints_issues(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(__file__).resolve().parents[1] / 'agentic.py',
                state_dir=root / 'state', session_dir=root / 'logs',
            )
            result = type('Result', (), {'returncode': 0, 'stdout': '#1 Fix it\n'})()
            with (patch('agentic_shell.subprocess.run', return_value=result) as run,
                  patch('sys.stdout', new_callable=StringIO) as output):
                shell.show_issues(['/list'])
            argv = run.call_args.args[0]
            self.assertIn('--list', argv)
            self.assertEqual(Path(run.call_args.kwargs['cwd']).resolve(), root.resolve())
            self.assertEqual(output.getvalue(), '#1 Fix it\n')


    def test_list_rejects_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with self.assertRaises(ValueError):
                shell.show_issues(['/list', 'extra'])


    def test_list_command_dispatches_to_issue_listing(self):
        with tempfile.TemporaryDirectory() as temp:
            shell = agentic_shell.AgenticShell(
                Path(temp), Path(sys.executable), session_dir=Path(temp) / 'logs'
            )
            with patch.object(shell, 'show_issues') as show_issues:
                self.assertTrue(shell.execute('/list'))
            show_issues.assert_called_once_with(['/list'])


    def test_job_records_are_visible_from_a_later_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_dir = root / 'logs'
            first = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=session_dir)
            job = first._launch([
                sys.executable, '-u', '-c',
                'import time; print("persisted"); time.sleep(.3)',
            ])
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


    def test_shell_forwards_verbose_to_runner_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            with patch.object(shell, '_launch') as launch:
                shell.run_issue(['/run', '119', '--verbose'])
            self.assertIn('--verbose', launch.call_args.args[0])


    def test_shell_runs_publish_in_foreground_for_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            process = type('Process', (), {
                'stdout': StringIO('publication output\n'),
                'wait': lambda self: 0,
            })()
            with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                  patch('agentic_shell.subprocess.Popen', return_value=process) as run,
                  patch.object(shell, '_launch') as launch,
                  patch('sys.stdout', new_callable=StringIO) as output):
                shell.run_issue(['/run', '119', '--publish'])
            launch.assert_not_called()
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertIn('--publish', argv)
            self.assertNotIn('stdin', run.call_args.kwargs)
            self.assertEqual(run.call_args.kwargs['stdout'], agentic_shell.subprocess.PIPE)
            self.assertEqual(run.call_args.kwargs['stderr'], agentic_shell.subprocess.STDOUT)
            self.assertIn('foreground', output.getvalue())

    def test_foreground_publish_output_is_retained_by_interactive_viewport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            state = agentic_shell_input._InteractiveOutputState()
            previous = agentic_shell_input._activate_interactive_output(state)
            process = type('Process', (), {
                'stdout': StringIO('publication output\n'),
                'wait': lambda self: 0,
            })()
            try:
                with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                      patch('agentic_shell.subprocess.Popen', return_value=process),
                      patch('sys.stdout', new_callable=StringIO)):
                    shell.run_issue(['/run', '119', '--publish'])
            finally:
                agentic_shell_input._restore_interactive_output(previous)

        self.assertIn('publication output', ''.join(state.history))

    def test_foreground_publish_command_output_never_becomes_editable_prompt(self):
        command_output = "agent> gh issue edit 119 --body $'first\\nsecond'\n" \
            "gh issue create --title 'quoted title' --body $'third\\nfourth' agent> "
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                shell = agentic_shell.AgenticShell(
                    root, Path(sys.executable), session_dir=root / 'logs'
                )
                state = agentic_shell_input._InteractiveOutputState()
                previous = agentic_shell_input._activate_interactive_output(state)
                output = StringIO()

                class Process:
                    stdout = StringIO(command_output)
                    pid = 123

                    def poll(self):
                        return None

                    def wait(self):
                        owner = getattr(
                            agentic_shell_input._ACTIVE_INTERACTIVE_OUTPUT,
                            '__self__',
                            None,
                        )
                        self.input_at_wait = getattr(owner, 'input_text', None)
                        self.prompt_at_wait = getattr(owner, 'prompt', None)
                        return returncode

                process = Process()
                try:
                    with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                          patch('agentic_shell.subprocess.Popen', return_value=process),
                          patch('agentic_shell.shutil.get_terminal_size',
                                return_value=os.terminal_size((80, 8))),
                          patch('agentic_shell_input.shutil.get_terminal_size',
                                return_value=os.terminal_size((80, 8))),
                          patch('sys.stdout', output)):
                        if returncode:
                            with self.assertRaisesRegex(RuntimeError, 'exit 1'):
                                shell._run_foreground(['--publish'])
                        else:
                            shell._run_foreground(['--publish'])
                finally:
                    active_after_run = agentic_shell_input._ACTIVE_INTERACTIVE_OUTPUT
                    agentic_shell_input._restore_interactive_output(previous)

                self.assertIs(
                    getattr(active_after_run, '__self__', None),
                    state,
                )
                self.assertEqual(process.input_at_wait, '')
                self.assertEqual(process.prompt_at_wait, '')
                rendered_history = ''.join(state.history)
                self.assertNotIn(command_output, rendered_history)
                self.assertIn("agent> gh issue edit 119", rendered_history)
                self.assertIn('first\nsecond', rendered_history)
                self.assertIn('third\nfourth agent> ', rendered_history)
                self.assertNotIn('first\\nsecond', rendered_history)
                self.assertNotIn("$'", rendered_history)

    def test_foreground_output_normalization_preserves_windows_paths(self):
        windows_command = r"gh issue edit 119 --body C:\new\file --title C:\temp\folder"

        self.assertEqual(
            agentic_shell._normalize_foreground_output(windows_command),
            windows_command,
        )

    def test_foreground_child_confirmation_prompt_stays_in_reserved_bottom_row(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            state = agentic_shell_input._InteractiveOutputState()
            previous = agentic_shell_input._activate_interactive_output(state)
            output = StringIO()

            class Process:
                stdout = StringIO(
                    'publication output\n'
                    "[PUBLISH] Publish this patch? [y/N]: "
                )

                def wait(self):
                    self.mode_at_wait = shell.terminal_mode
                    self.prompt_was_visible = (
                        "[PUBLISH] Publish this patch? [y/N]: "
                        in output.getvalue()
                    )
                    return 0

            process = Process()
            try:
                with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                      patch('agentic_shell.subprocess.Popen', return_value=process),
                      patch('agentic_shell.shutil.get_terminal_size',
                            return_value=os.terminal_size((80, 8))),
                      patch('agentic_shell_input.shutil.get_terminal_size',
                            return_value=os.terminal_size((80, 8))),
                      patch('sys.stdout', output)):
                    shell._run_foreground(['publish'])
            finally:
                agentic_shell_input._restore_interactive_output(previous)

        rendered = output.getvalue()
        self.assertTrue(process.prompt_was_visible)
        self.assertEqual(process.mode_at_wait, agentic_shell.TerminalMode.CONFIRMATION)
        self.assertLess(
            rendered.rfind('publication output'),
            rendered.rfind("[PUBLISH] Publish this patch? [y/N]: "),
        )
        self.assertIn(
            "[PUBLISH] Publish this patch? [y/N]: ",
            ''.join(state.history),
        )

    def test_foreground_publish_waiting_prompts_are_editable(self):
        prompts = (
            "[PUBLISH] Waiting for worktree selection. Enter a number or 'q' to cancel: ",
            "[PUBLISH] Publish this patch? [y/N]: ",
            "[PUBLISH] Waiting for publication confirmation. Type 'publish' to continue or 'q' to cancel: ",
            "[PUBLISH] Publish this patch? Type 'publish' to approve: ",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(agentic_shell._is_foreground_prompt(prompt))
                self.assertEqual(agentic_shell._foreground_prompt_prefix(prompt), 'PUBLISH')

    def test_foreground_child_arbitrary_prompt_is_shown_while_it_waits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            output = StringIO()

            class PromptStream:
                def __init__(self):
                    self.text = iter('Continue?')
                    self.waiting = threading.Event()

                def read(self, _size):
                    try:
                        return next(self.text)
                    except StopIteration:
                        self.waiting.wait(0.2)
                        return ''

            class Process:
                stdout = PromptStream()

                def wait(self):
                    self.mode_at_wait = shell.terminal_mode
                    self.prompt_was_visible = 'Continue?' in output.getvalue()
                    owner = getattr(
                        agentic_shell_input._ACTIVE_INTERACTIVE_OUTPUT,
                        '__self__',
                        None,
                    )
                    self.editable_prompt = getattr(owner, 'prompt', None)
                    return 0

            process = Process()
            state = agentic_shell_input._InteractiveOutputState()
            previous = agentic_shell_input._activate_interactive_output(state)
            try:
                with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                      patch('agentic_shell.subprocess.Popen', return_value=process),
                      patch('agentic_shell.shutil.get_terminal_size',
                            return_value=os.terminal_size((80, 8))),
                      patch('agentic_shell_input.shutil.get_terminal_size',
                            return_value=os.terminal_size((80, 8))),
                      patch('sys.stdout', output)):
                    shell._run_foreground(['publish'])
            finally:
                agentic_shell_input._restore_interactive_output(previous)

        self.assertTrue(process.prompt_was_visible)
        self.assertEqual(process.mode_at_wait, agentic_shell.TerminalMode.CONFIRMATION)
        self.assertEqual(process.editable_prompt, '')

    def test_foreground_child_output_styles_prefix_without_recoloring_body(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            process = type('Process', (), {
                'stdout': StringIO('[DEVELOPER] Agent: provider says PASSED\n'),
                'wait': lambda self: 0,
            })()
            with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                  patch('agentic_shell.subprocess.Popen', return_value=process),
                  patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}),
                  patch('sys.stdout', new=TTYBuffer()) as output):
                shell._run_foreground(['chat'])

        rendered = output.getvalue()
        self.assertIn(
            '\033[36m[DEVELOPER]\033[0m Agent: provider says PASSED',
            rendered,
        )
        self.assertNotIn('\033[32mPASSED\033[0m', rendered)


    def test_shell_preserves_instruction_as_one_runner_argv_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            instruction = 'Bump version; keep this text as data'
            with patch.object(shell, '_launch') as launch:
                shell.run_issue(['/run', '119', '--instruction', instruction])
            argv = launch.call_args.args[0]
            self.assertIn('--instruction=' + instruction, argv)


    def test_shell_preserves_option_like_and_empty_instruction_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            with patch.object(shell, '_launch') as launch:
                shell.execute('/run 119 --instruction "--preserve"')
                argv = launch.call_args.args[0]
                self.assertIn('--instruction=--preserve', argv)
                parsed = agentic.build_parser().parse_args(argv[3:])
                self.assertEqual(parsed.instruction, '--preserve')

                shell.execute('/run 119 --instruction "--publish"')
                argv = launch.call_args.args[0]
                self.assertIn('--instruction=--publish', argv)
                self.assertNotIn('--publish', argv)

                shell.execute('/run 119 --instruction ""')
                argv = launch.call_args.args[0]
                self.assertIn('--instruction=', argv)
                parsed = agentic.build_parser().parse_args(argv[3:])
                self.assertEqual(parsed.instruction, '')


    def test_shell_does_not_treat_instruction_option_names_as_run_options(self):
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
                shell.run_issue(['/run', '--instruction', '--task-file'])
            argv = launch.call_args.args[0]
            self.assertIn('--instruction=--task-file', argv)
            self.assertIn('--task-file', argv)
            self.assertIn(str(task.resolve()), argv)
            self.assertNotIn('--publish', argv)


    def test_shell_reports_foreground_publish_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            process = type('Process', (), {
                'stdout': StringIO('publication failed\n'),
                'wait': lambda self: 1,
            })()
            with (patch('agentic_shell._is_interactive_terminal', return_value=True),
                  patch('agentic_shell.subprocess.Popen', return_value=process)):
                with self.assertRaisesRegex(RuntimeError, 'exit 1'):
                    shell.run_issue(['/run', '119', '--publish'])

    def test_foreground_publish_preserves_separate_streams_when_non_interactive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            with (patch('agentic_shell._is_interactive_terminal', return_value=False),
                  patch('agentic_shell.subprocess.run', return_value=type(
                      'Result', (), {'returncode': 0}
                  )()) as run,
                  patch('agentic_shell.subprocess.Popen') as popen):
                shell._run_foreground(['publish'])

            run.assert_called_once_with(['publish'], cwd=root, check=False)
            popen.assert_not_called()


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


    def test_shell_stop_all_requests_termination_for_running_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(root, Path(sys.executable), session_dir=root / 'logs')
            first = shell._launch([sys.executable, '-c', 'import time; time.sleep(5)'])
            second = shell._launch([sys.executable, '-c', 'import time; time.sleep(5)'])
            with patch('sys.stdout'):
                shell.stop(['/stop', '--all'])
            first.process.wait(timeout=5)
            second.process.wait(timeout=5)
            shell.refresh()
            self.assertIsNotNone(first.exit_code)
            self.assertIsNotNone(second.exit_code)


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
