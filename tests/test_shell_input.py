"""Focused interactive input and completion tests."""
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic_shell


from test_shell import _create_mock_reader

class InteractiveInputTests(unittest.TestCase):
    def test_completion_suggests_commands_options_and_job_ids(self):
        self.assertEqual(
            agentic_shell._completion_candidates('/r'),
            ['/run', '/runs'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/li'),
            ['/list'],
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
            agentic_shell._completion_candidates('/run --profile'),
            ['--profile'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/run --profile '),
            ['fast', 'slow'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/logs ', job_numbers=(2, 1)),
            ['1', '2'],
        )
        jobs = {
            1: type('JobState', (), {'running': False, 'pid': 101, 'exit_code': 0})(),
            2: type('JobState', (), {'running': True, 'pid': 102, 'exit_code': None})(),
        }
        self.assertEqual(
            [candidate for candidate, _ in agentic_shell._completion_items(
                '/logs ', job_numbers=(1, 2), jobs=jobs,
            )],
            ['2', '1'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/status '),
            ['--json'],
        )
        self.assertEqual(
            agentic_shell._completion_candidates('/stop '),
            ['--all'],
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
        self.assertIn('/list', items)
        self.assertEqual(items['/list'], 'list open GitHub issues')

        run_options = dict(agentic_shell._completion_items('/run --'))
        self.assertIn('--profile', run_options)
        self.assertIn('--review', run_options)
        self.assertIn('--publish', run_options)
        self.assertEqual(run_options['--profile'], 'execution profile (fast or slow)')

        profiles = dict(agentic_shell._completion_items('/run --profile '))
        self.assertEqual(profiles.get('fast'), 'run the configured fast checks')
        self.assertEqual(profiles.get('slow'), 'run fast checks plus configured slow checks')


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
            ('CHAR', '/'), ('CHAR', 'l'), ('CHAR', ' '),
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
        self.assertEqual(result, '/listX foo')


    def test_interactive_input_clips_long_lines_instead_of_wrapping(self):
        line = '/doctor --commands --worktree /home/toanl/.local/state/agentic/Tally-ba6fd564a347/worktrees/issue-119-5802a6ff'
        visible, cursor = agentic_shell._clip_input_line(
            line, len(line), len('agentic> '), 40,
        )
        self.assertLessEqual(len('agentic> ') + len(visible), 39)
        self.assertEqual(cursor, len(visible))

        keys = [('CHAR', char) for char in line] + [('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        with patch('agentic_shell.shutil.get_terminal_size', return_value=os.terminal_size((40, 24))), \
             patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                'agentic> ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, line)


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


if __name__ == "__main__":
    unittest.main()
