import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic_shell


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


def _capture_ps(shell, parts=None):
    from contextlib import redirect_stdout
    from io import StringIO

    output = StringIO()
    with redirect_stdout(output):
        shell.show_processes(parts)
    return output.getvalue()


if __name__ == '__main__':
    unittest.main()
