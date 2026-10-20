import os
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
            self.assertIn('\033[34m[TESTS]\033[0m PASSED', colored.getvalue())
            self.assertNotIn('\033[32mPASSED\033[0m', colored.getvalue())

            plain = StringIO()
            with redirect_stdout(plain):
                shell.show_logs(['/logs', str(job.number), '5'])
            self.assertNotIn('\033[', plain.getvalue())
            self.assertIn('[TESTS] PASSED', plain.getvalue())


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
            self.assertIn('\033[34m[TESTS]\033[0m \033[32mPASSED\033[0m', output.getvalue())


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


    def test_shell_runs_publish_in_foreground_for_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = agentic_shell.AgenticShell(
                root, Path(sys.executable), session_dir=root / 'logs'
            )
            result = type('Result', (), {'returncode': 0})()
            with (patch('agentic_shell.subprocess.run', return_value=result) as run,
                  patch.object(shell, '_launch') as launch,
                  patch('sys.stdout', new_callable=StringIO) as output):
                shell.run_issue(['/run', '119', '--publish'])
            launch.assert_not_called()
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertIn('--publish', argv)
            self.assertNotIn('stdin', run.call_args.kwargs)
            self.assertIn('foreground', output.getvalue())


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
            result = type('Result', (), {'returncode': 1})()
            with patch('agentic_shell.subprocess.run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, 'exit 1'):
                    shell.run_issue(['/run', '119', '--publish'])


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
