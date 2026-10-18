import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic_shell


class ShellTests(unittest.TestCase):
    def test_parse_run_command(self):
        self.assertEqual(agentic_shell._parse_job_command(['/run', '119']), ('119', []))
        self.assertEqual(
            agentic_shell._parse_job_command(['/run', '119', '--review']),
            ('119', ['--review']),
        )
        with self.assertRaises(ValueError):
            agentic_shell._parse_job_command(['/run', '119', '--review', '--publish'])

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


def _capture_ps(shell):
    from contextlib import redirect_stdout
    from io import StringIO

    output = StringIO()
    with redirect_stdout(output):
        shell.show_processes()
    return output.getvalue()


if __name__ == '__main__':
    unittest.main()
