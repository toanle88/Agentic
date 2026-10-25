"""Focused tests for shell argv validation independent of process lifecycle."""
import unittest

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner.output import ConciseLogStream
from agentic_runner.shell_commands import (
    parse_job_command, parse_log_command, parse_options, parse_repair_pr_command,
)


class ShellCommandTests(unittest.TestCase):
    def test_repair_pr_requires_positive_numeric_pull_request(self):
        self.assertEqual(parse_repair_pr_command(['/repair-pr', '123']), ('123', False))
        self.assertEqual(
            parse_repair_pr_command(['/repair-pr', '123', '--clean']),
            ('123', True),
        )
        for parts in (['/repair-pr'], ['/repair-pr', '0'], ['/repair-pr', '-1'],
                      ['/repair-pr', '123', '--verbose'],
                      ['/repair-pr', '123', '--clean', '--clean']):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                parse_repair_pr_command(parts)

    def test_instruction_keeps_option_like_and_empty_values(self):
        self.assertEqual(
            parse_job_command(["/run", "47", "--instruction", "--preserve"]),
            ("47", ["--instruction", "--preserve"]),
        )
        self.assertEqual(
            parse_job_command(["/run", "47", "--instruction", ""]),
            ("47", ["--instruction", ""]),
        )

    def test_offline_resume_is_a_valid_run_flag(self):
        self.assertEqual(
            parse_job_command(["/run", "47", "--offline"]),
            ("47", ["--offline"]),
        )

    def test_verbose_is_a_valid_run_flag(self):
        self.assertEqual(
            parse_job_command(["/run", "47", "--verbose"]),
            ("47", ["--verbose"]),
        )

    def test_verbose_is_a_valid_logs_flag(self):
        self.assertEqual(
            parse_log_command(["/logs", "47", "80", "--verbose"]),
            (47, 80, True),
        )
        self.assertEqual(
            parse_log_command(["/logs", "47", "--verbose"]),
            (47, 40, True),
        )

    def test_logs_rejects_duplicate_or_invalid_arguments(self):
        for parts in (
            ["/logs", "47", "--verbose", "--verbose"],
            ["/logs", "47", "0"],
            ["/logs", "47", "80", "81"],
            ["/logs", "47", "--unknown"],
        ):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                parse_log_command(parts)

    def test_concise_log_stream_handles_arbitrary_chunks_and_summarizes_changes(self):
        stream = ConciseLogStream()
        self.assertEqual(
            stream.feed('[DEVELOPER] Command finished (exit '),
            '',
        )
        self.assertEqual(
            stream.feed('0)\n[DEVELOPER] Modified files: a.py\n[DEVELOPER] Modified '),
            '',
        )
        output = stream.feed('files: b.py\n[DEVELOPER] ERROR: failed\n') + stream.flush()
        self.assertIn('[DEVELOPER] ERROR: failed\n', output)
        self.assertIn('Modified files: 2 files changed\n', output)
        self.assertNotIn('Command finished (exit 0)', output)

    def test_conflicting_run_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_job_command(["/run", "47", "--review", "--publish"])

    def test_subcommand_parser_returns_original_argv(self):
        flags = parse_options(
            ["/runs", "--json"], usage="/runs [--json]",
            value_options=set(), boolean_options={"--json"},
        )
        self.assertEqual(flags, ["--json"])


if __name__ == "__main__":
    unittest.main()
