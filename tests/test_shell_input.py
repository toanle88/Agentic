"""Focused interactive input and completion tests."""
import os
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agentic_shell
import agentic_shell_input
from agentic_output import strip_ansi


from test_shell import _create_mock_reader


class TTYBuffer(StringIO):
    def isatty(self):
        return True


class InteractiveInputTests(unittest.TestCase):
    def test_output_cursor_is_recomputed_after_terminal_width_changes(self):
        state = agentic_shell_input._InteractiveOutputState()
        state.append('12345678', ensure_newline=False, columns=8)
        self.assertEqual((state.cursor_column, state.cursor_wrap_pending), (8, True))

        state.recompute_cursor(20)
        self.assertEqual((state.cursor_column, state.cursor_wrap_pending), (9, False))
        state.append('X', ensure_newline=False, columns=20)
        self.assertEqual((state.cursor_column, state.cursor_wrap_pending), (10, False))

    def test_prompt_boundary_separates_partial_output_from_next_prompt_output(self):
        state = agentic_shell_input._InteractiveOutputState()
        state.append('partial', ensure_newline=False, columns=80)
        state.mark_prompt_boundary(80)
        state.append('next', ensure_newline=False, columns=80)

        self.assertEqual(''.join(state.history), 'partial\nnext')
        self.assertEqual((state.cursor_column, state.cursor_wrap_pending), (5, False))

    def test_output_history_wraps_and_retains_rows(self):
        self.assertEqual(
            agentic_shell_input._output_history_lines(
                'first\nsecond\nthird\n', 20
            ),
            ['first', 'second', 'third'],
        )
        self.assertEqual(
            agentic_shell_input._output_history_lines('123456789', 4),
            ['1234', '5678', '9'],
        )

    def test_interactive_output_page_up_reviews_retained_history(self):
        class OutputReader:
            def __init__(self):
                self.index = 0
                self.keys = [
                    ('PAGE_UP', ''), ('ENTER', '\n'),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index < 5:
                    agentic_shell._shell_print(f'output-{self.index + 1}')
                    self.index += 1
                    return 'CHAR', 'x'
                key = self.keys[self.index - 5]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 4))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        self.assertEqual(result, 'xxxxx')
        last_frame = output.getvalue().rsplit('\033[1;1r', 1)[-1]
        visible = strip_ansi(last_frame)
        self.assertIn('output-4', visible)
        self.assertNotIn('output-5', visible)

    def test_command_output_between_prompts_stays_in_the_next_viewport(self):
        state = agentic_shell_input._InteractiveOutputState()
        previous = agentic_shell_input._activate_interactive_output(state)
        output = StringIO()
        try:
            with patch('agentic_shell.shutil.get_terminal_size',
                       return_value=os.terminal_size((80, 4))), \
                 patch('sys.stdout', output):
                first_reader = _create_mock_reader([('ENTER', '\n')])
                self.assertEqual(
                    agentic_shell._interactive_input(
                        '› ', agentic_shell._completion_items,
                        key_reader_factory=first_reader,
                        output_state=state,
                    ),
                    '',
                )
                for index in range(1, 6):
                    agentic_shell._shell_print(f'command-output-{index}')

                second_reader = _create_mock_reader([
                    ('PAGE_UP', ''), ('ENTER', '\n'),
                ])
                agentic_shell._interactive_input(
                    '› ', agentic_shell._completion_items,
                    key_reader_factory=second_reader,
                    output_state=state,
                )
        finally:
            agentic_shell_input._restore_interactive_output(previous)

        self.assertEqual(len(state.history), 5)
        last_frame = output.getvalue().rsplit('\033[1;1r', 1)[-1]
        visible = strip_ansi(last_frame)
        self.assertIn('command-output-4', visible)
        self.assertNotIn('command-output-5', visible)

    def test_live_log_chunks_preserve_boundaries_but_status_adds_newline(self):
        captured = []

        def capture(text, ensure_newline):
            captured.append((text, ensure_newline))

        with patch.object(agentic_shell_input, '_ACTIVE_INTERACTIVE_OUTPUT', capture):
            agentic_shell._shell_write_log('partial')
            agentic_shell._shell_write_log(' log')
            agentic_shell._shell_print('[JOB 1] finished')

        self.assertEqual(
            captured,
            [
                ('partial', False),
                (' log', False),
                ('[JOB 1] finished\n', True),
            ],
        )

    def test_transient_status_is_not_retained_in_shell_scrollback(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((40, 8))):
            state.write_transient('[12:00:00] [DEVELOPER] Still running (3s elapsed)...')
            state.write_without_editor('[DEVELOPER] Finished in 3s (exit 0)\n')

        self.assertFalse(state.live_status)
        self.assertNotIn('Still running', ''.join(state.history))
        self.assertIn('[DEVELOPER] Finished in 3s (exit 0)', ''.join(state.history))
        self.assertIn('\r\033[2K', output.getvalue())

    def test_foreground_prompt_reserves_bottom_rows_and_redraws_after_resize(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   side_effect=[os.terminal_size((20, 6)), os.terminal_size((30, 8))]):
            foreground.set_prompt('first line\nsecond: ')
            foreground.refresh()

        rendered = output.getvalue()
        self.assertIn('\033[5;1H\033[2Kfirst line', rendered)
        self.assertIn('\033[7;1H\033[2Kfirst line', rendered)
        self.assertIn('second: ', rendered)

    def test_foreground_prompt_redraw_preserves_typed_child_input(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((30, 6))):
            foreground.set_prompt('Continue? ')
            foreground.set_input('publ', 4)
            foreground.refresh()

        self.assertEqual(foreground.input_text, 'publ')
        self.assertEqual(foreground.input_cursor, 4)
        self.assertIn('Continue? publ', output.getvalue())

    def test_foreground_prompt_keeps_input_typed_before_prompt_arrives(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((30, 6))):
            foreground.set_input('publ', 4)
            foreground.set_prompt('[PUBLISH] Publish this patch? ')

        self.assertEqual(foreground.input_text, 'publ')
        self.assertEqual(foreground.input_cursor, 4)
        self.assertNotIn('publ\n', ''.join(state.history))
        self.assertIn(
            '[PUBLISH] Publish this patch? publ',
            strip_ansi(output.getvalue()),
        )

    def test_foreground_output_keeps_typed_child_input_active(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((30, 6))):
            foreground.set_prompt('Continue? ')
            foreground.set_input('publ', 4)
            foreground.write('status update\n')

        self.assertEqual(foreground.input_text, 'publ')
        self.assertEqual(foreground.input_cursor, 4)
        self.assertIn('status update', ''.join(state.history))
        self.assertIn('Continue? publ', output.getvalue())

    def test_foreground_page_scroll_keeps_prompt_at_bottom(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 4))):
            foreground.set_prompt('Continue? ')
            for index in range(1, 9):
                foreground.write(f'output-{index}\n')
            foreground.scroll(100)

        visible = strip_ansi(output.getvalue().rsplit('\033[1;3r', 1)[-1])
        self.assertIn('output-1', visible)
        self.assertNotIn('output-8', visible)
        self.assertIn('Continue? ', visible)

        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 4))):
            foreground.scroll(-100)
        visible = strip_ansi(output.getvalue().rsplit('\033[1;3r', 1)[-1])
        self.assertNotIn('output-1', visible)
        self.assertIn('output-8', visible)
        self.assertIn('Continue? ', visible)

    def test_foreground_long_output_and_resize_keep_publish_prompt_active(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        sizes = iter([os.terminal_size((80, 8)), os.terminal_size((32, 5))])
        last_size = os.terminal_size((32, 5))

        def terminal_size(*_args, **_kwargs):
            nonlocal last_size
            try:
                last_size = next(sizes)
            except StopIteration:
                pass
            return last_size

        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   side_effect=terminal_size):
            foreground.set_prompt('[PUBLISH] Publish this patch? ')
            foreground.set_input('publish', 7)
            for index in range(200):
                foreground.write(f'patch line {index:03d} ' + ('x' * 60) + '\n')
            foreground.refresh()

        self.assertGreater(len(''.join(state.history)), 10_000)
        self.assertEqual(foreground.input_text, 'publish')
        self.assertIn(
            '[PUBLISH] Publish this patch? publish',
            strip_ansi(output.getvalue()),
        )

    def test_foreground_prompt_clips_multiline_text_to_active_bottom_rows(self):
        state = agentic_shell_input._InteractiveOutputState()
        output = TTYBuffer()
        foreground = agentic_shell_input._ForegroundOutputState(state)
        with patch('sys.stdout', output), \
             patch('agentic_shell_input.shutil.get_terminal_size',
                   return_value=os.terminal_size((30, 3))):
            foreground.set_prompt('one\ntwo\nthree\nfour\nfive: ')

        rendered = output.getvalue()
        self.assertNotIn('one', rendered)
        self.assertIn('three', rendered)
        self.assertIn('five: ', rendered)

    def test_help_output_is_retained_by_the_interactive_viewport(self):
        state = agentic_shell_input._InteractiveOutputState()
        previous = agentic_shell_input._activate_interactive_output(state)
        try:
            with patch('sys.stdout', StringIO()):
                agentic_shell._print_help()
        finally:
            agentic_shell_input._restore_interactive_output(previous)

        self.assertIn('Commands:', ''.join(state.history))
        self.assertIn('/help', ''.join(state.history))

    def test_interactive_input_keeps_completion_above_bottom_prompt(self):
        keys = [('CHAR', '/'), ('ENTER', '\n'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        self.assertEqual(result, '/run ')
        rendered = output.getvalue()
        self.assertIn('\033[6;1H', rendered)
        completion_frame = rendered.split('\033[1;1H', 1)[1].split('\033[6;1H', 1)[0]
        plain_frame = strip_ansi(completion_frame)
        self.assertLess(plain_frame.find('/run'), plain_frame.rfind('› /'))

    def test_interactive_output_is_redrawn_above_input(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', '/'), ('ENTER', '\n'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_print('[JOB 1] finished')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
                status_line='status',
            )

        self.assertEqual(result, '/run ')
        rendered = output.getvalue()
        self.assertIn('\033[4;1H[JOB 1] finished', rendered)
        plain_output = strip_ansi(rendered)
        self.assertLess(
            plain_output.rfind('[JOB 1] finished'),
            plain_output.rfind('› /run '),
        )

    def test_interactive_input_paints_a_full_width_row_on_a_tty(self):
        reader = _create_mock_reader([('CHAR', 'x'), ('ENTER', '\n')])
        output = TTYBuffer()
        with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((20, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        self.assertEqual(result, 'x')
        rendered = output.getvalue()
        self.assertIn(
            '\033[48;5;236m\033[1;36m› \033[97mx' + ' ' * 17 + '\033[0m',
            rendered,
        )
        self.assertIn('\033[97m', rendered)

    def test_interactive_input_adds_padded_rows_around_the_prompt(self):
        reader = _create_mock_reader([('CHAR', 'x'), ('ENTER', '\n')])
        output = TTYBuffer()
        with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((20, 8))), \
             patch('sys.stdout', output):
            agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        rendered = output.getvalue()
        self.assertIn('\033[6;1H', rendered)
        self.assertGreaterEqual(rendered.count('\033[48;5;236m'), 3)

    def test_interactive_input_keeps_prompt_and_completion_on_a_tiny_terminal(self):
        keys = [('CHAR', '/'), ('ENTER', '\n'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 2))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        self.assertEqual(result, '/run ')
        rendered = output.getvalue()
        self.assertIn('\033[1;1H', rendered)
        self.assertIn('\n› /run', strip_ansi(rendered))

    def test_interactive_input_preserves_output_row_on_a_three_row_terminal(self):
        state = agentic_shell_input._InteractiveOutputState()
        state.history.append('retained output\n')
        reader = _create_mock_reader([
            ('PASTE', 'first\nsecond'), ('ENTER', '\n'),
        ])
        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 3))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
                output_state=state,
            )

        self.assertEqual(result, 'first\nsecond')
        plain_output = strip_ansi(output.getvalue())
        self.assertIn('retained output', plain_output)
        self.assertLess(
            plain_output.find('retained output'),
            plain_output.rfind('› '),
        )

    def test_input_row_uses_display_width_for_wide_characters(self):
        with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('sys.stdout', new=TTYBuffer()):
            rendered = agentic_shell_input._input_row('› ', '界', 20)

        self.assertEqual(
            agentic_shell_input._terminal_text_width(strip_ansi(rendered)),
            20,
        )
        self.assertTrue(strip_ansi(rendered).endswith(' ' * 16))

    def test_clip_input_line_uses_display_width_for_wide_characters(self):
        visible, cursor = agentic_shell_input._clip_input_line(
            'abcdefgh界', len('abcdefgh界'), 2, 10,
        )

        self.assertEqual(visible, 'defgh界')
        self.assertEqual(cursor, agentic_shell_input._terminal_text_width(visible))
        self.assertLessEqual(
            agentic_shell_input._terminal_text_width('› ' + visible),
            9,
        )

    def test_clip_input_line_does_not_start_inside_a_wide_character(self):
        visible, cursor = agentic_shell_input._clip_input_line(
            '界界', len('界界'), 2, 6,
        )

        self.assertEqual(visible, '界')
        self.assertEqual(cursor, 2)

    def test_interactive_input_falls_back_without_a_background_when_color_is_disabled(self):
        reader = _create_mock_reader([('CHAR', 'x'), ('ENTER', '\n')])
        output = TTYBuffer()
        with patch.dict(os.environ, {'NO_COLOR': '1', 'AGENTIC_PLAIN': ''}), \
             patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((12, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        self.assertEqual(result, 'x')
        rendered = output.getvalue()
        self.assertNotIn('\033[48;5;236m', rendered)
        self.assertIn('› x' + ' ' * 9, strip_ansi(rendered))

    def test_interactive_input_resizes_every_multiline_row_background(self):
        reader = _create_mock_reader([
            ('PASTE', 'first\nsecond'), ('ENTER', '\n'),
        ])
        output = TTYBuffer()
        sizes = iter([
            os.terminal_size((20, 8)),
            os.terminal_size((10, 8)),
            os.terminal_size((10, 8)),
        ])
        with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('agentic_shell.shutil.get_terminal_size',
                   side_effect=lambda fallback: next(sizes, os.terminal_size((10, 8)))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        self.assertEqual(result, 'first\nsecond')
        rendered = output.getvalue()
        self.assertIn(
            '\033[48;5;236m\033[1;36m› \033[97mfirst   \033[0m',
            rendered,
        )
        self.assertIn(
            '\033[48;5;236m\033[1;36m  \033[97msecond  \033[0m',
            rendered,
        )

    def test_interactive_output_resets_input_background_before_logs(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'x'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 1:
                    agentic_shell._shell_print('[JOB 1] finished')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = TTYBuffer()
        with patch.dict(os.environ, {'NO_COLOR': '', 'AGENTIC_PLAIN': ''}), \
             patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((20, 8))), \
             patch('sys.stdout', output):
            agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        rendered = output.getvalue()
        log_position = rendered.index('[JOB 1] finished')
        self.assertGreater(rendered.rfind('\033[0m', 0, log_position), -1)

    def test_interactive_split_output_chunks_keep_the_same_terminal_line(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_write_log('partial')
                elif self.index == 1:
                    agentic_shell._shell_write_log(' log')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
                status_line='status',
            )

        self.assertEqual(result, 'ab')
        rendered = output.getvalue()
        self.assertIn('\033[4;1Hpartial', rendered)
        self.assertIn('\033[4;8H log', rendered)

    def test_interactive_split_output_wraps_after_the_right_margin(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_write_log('12345678')
                elif self.index == 1:
                    agentic_shell._shell_write_log('X')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((8, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        self.assertEqual(result, 'ab')
        rendered = output.getvalue()
        self.assertIn('\033[5;1H12345678', rendered)
        self.assertIn('\033[5;1HX', rendered)

    def test_interactive_split_output_tracks_tab_stops(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_write_log('a\t')
                elif self.index == 1:
                    agentic_shell._shell_write_log('X')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((20, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        self.assertEqual(result, 'ab')
        self.assertIn('\033[5;9HX', output.getvalue())

    def test_interactive_split_output_tracks_wide_characters(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_write_log('界')
                elif self.index == 1:
                    agentic_shell._shell_write_log('X')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((20, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        self.assertEqual(result, 'ab')
        self.assertIn('\033[5;3HX', output.getvalue())

    def test_interactive_split_output_wraps_wide_character_at_right_margin(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_write_log('1234567界')
                elif self.index == 1:
                    agentic_shell._shell_write_log('X')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((8, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        self.assertEqual(result, 'ab')
        self.assertIn('\033[5;1H1234567界', output.getvalue())
        self.assertIn('\033[5;3HX', output.getvalue())

    def test_interactive_split_output_preserves_wrap_after_combining_mark(self):
        class OutputReader:
            def __init__(self):
                self.keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
                self.index = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read_key(self):
                if self.index == 0:
                    agentic_shell._shell_write_log('1234567a\u0301')
                elif self.index == 1:
                    agentic_shell._shell_write_log('X')
                key = self.keys[self.index]
                self.index += 1
                return key

        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((8, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=OutputReader,
            )

        self.assertEqual(result, 'ab')
        self.assertIn('\033[5;1H1234567a', output.getvalue())
        self.assertIn('\033[5;1HX', output.getvalue())

    def test_interactive_input_reduces_completion_rows_for_short_terminal(self):
        keys = [('CHAR', '/'), ('ENTER', '\n'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 4))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
                status_line='status',
            )

        self.assertEqual(result, '/run ')
        rendered = output.getvalue()
        completion_frame = next(
            frame for frame in rendered.split('\033[1;1H')
            if '/run' in strip_ansi(frame)
            and '/ps' in strip_ansi(frame)
            and '/chat' not in strip_ansi(frame)
        )
        plain_frame = strip_ansi(completion_frame)
        self.assertIn('/run', plain_frame)
        self.assertIn('/ps', plain_frame)
        self.assertNotIn('/chat', plain_frame)

    def test_interactive_input_clears_completion_rows_when_dismissed(self):
        keys = [
            ('CHAR', '/'), ('ESCAPE', '\x1b'),
            ('CHAR', 'q'), ('CHAR', 'u'), ('CHAR', 'i'), ('CHAR', 't'),
            ('ENTER', '\n'),
        ]
        reader = _create_mock_reader(keys)
        output = StringIO()
        with patch('agentic_shell.shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 8))), \
             patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )

        self.assertEqual(result, '/quit')
        rendered = output.getvalue()
        self.assertGreaterEqual(rendered.count('\033[2K'), 8)

    def test_completion_suggests_commands_options_and_job_ids(self):
        self.assertIn('/chat', agentic_shell._completion_candidates('/ch'))
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

    def test_interactive_input_accepts_bracketed_paste_as_multiline_text(self):
        keys = [('PASTE', 'first line\nsecond line'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        output = StringIO()
        with patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
                status_line='GPT model · /tmp/repo · task: idle',
            )
        self.assertEqual(result, 'first line\nsecond line')
        self.assertIn('GPT model · /tmp/repo · task: idle', output.getvalue())
        self.assertIn('\033[?2004h', output.getvalue())
        self.assertIn('\033[?2004l', output.getvalue())

    def test_interactive_input_sanitizes_status_before_painting(self):
        reader = _create_mock_reader([('ENTER', '\n')])
        output = StringIO()
        with patch('sys.stdout', output):
            result = agentic_shell._interactive_input(
                '› ',
                agentic_shell._completion_items,
                key_reader_factory=reader,
                status_line='model\x1b[31m\r\nrepo\x07',
            )
        self.assertEqual(result, '')
        rendered_status = output.getvalue().split('\033[?2004h', 1)[1].split('\n', 1)[0]
        self.assertIn('model', rendered_status)
        self.assertIn('repo', rendered_status)
        self.assertNotIn('\x1b[31m', rendered_status)
        self.assertNotIn('\r', rendered_status)
        self.assertNotIn('\x07', rendered_status)

    def test_interactive_input_redraws_after_terminal_resize(self):
        keys = [('CHAR', 'a'), ('CHAR', 'b'), ('ENTER', '\n')]
        reader = _create_mock_reader(keys)
        sizes = iter([os.terminal_size((80, 24)), os.terminal_size((20, 8))])
        with patch('agentic_shell.shutil.get_terminal_size',
                   side_effect=lambda fallback: next(sizes, os.terminal_size((20, 8)))), \
             patch('sys.stdout', new_callable=StringIO):
            result = agentic_shell._interactive_input(
                '› ', agentic_shell._completion_items,
                key_reader_factory=reader,
            )
        self.assertEqual(result, 'ab')

    def test_parse_ansi_sequence_supports_paste_and_shift_enter(self):
        cases = [
            (['[', '2', '0', '0', '~'], ('PASTE_START', '')),
            (['[', '2', '0', '1', '~'], ('PASTE_END', '')),
            (['[', '1', '3', ';', '2', 'u'], ('NEWLINE', '\n')),
            (['[', '2', '7', ';', '2', ';', '1', '3', '~'], ('NEWLINE', '\n')),
        ]
        for seq, expected in cases:
            iterator = iter(seq)
            result = agentic_shell._parse_ansi_sequence(lambda: next(iterator, None))
            self.assertEqual(result, expected)


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
            (['[', '5', '~'], ('PAGE_UP', '')),
            (['[', '6', '~'], ('PAGE_DOWN', '')),
        ]
        for seq, expected in cases:
            iterator = iter(seq)
            result = agentic_shell._parse_ansi_sequence(lambda: next(iterator, None))
            self.assertEqual(result, expected)

    @unittest.skipUnless(os.name != 'nt', 'POSIX terminal behavior only')

    def test_interactive_input_redraws_after_idle_sigwinch(self):
        import io
        import pty
        import signal
        import threading
        import time

        master, slave = pty.openpty()
        raw = os.fdopen(slave, 'rb', buffering=0)
        stream = io.TextIOWrapper(raw, encoding='utf-8')

        class ResizeReader(agentic_shell._KeyReader):
            def __enter__(self):
                result = super().__enter__()

                def notify_resize():
                    time.sleep(0.05)
                    os.kill(os.getpid(), signal.SIGWINCH)
                    time.sleep(0.1)
                    os.write(master, b'\n')

                self._resize_thread = threading.Thread(target=notify_resize, daemon=True)
                self._resize_thread.start()
                return result

            def __exit__(self, *args):
                self._resize_thread.join(timeout=1)
                return super().__exit__(*args)

        sizes = iter([os.terminal_size((80, 24)), os.terminal_size((80, 24)),
                      os.terminal_size((20, 8))])
        output = StringIO()
        try:
            with patch.object(agentic_shell.sys, 'stdin', stream), \
                 patch('agentic_shell.shutil.get_terminal_size',
                       side_effect=lambda fallback: next(sizes, os.terminal_size((20, 8)))), \
                 patch('sys.stdout', output):
                result = agentic_shell._interactive_input(
                    '› ', agentic_shell._completion_items,
                    key_reader_factory=ResizeReader,
                )
        finally:
            stream.close()
            os.close(master)

        self.assertEqual(result, '')
        rendered = output.getvalue()
        self.assertIn('\033[22;1H', rendered)
        self.assertIn('\033[6;1H', rendered)

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
