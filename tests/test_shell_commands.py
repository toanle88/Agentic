"""Focused tests for shell argv validation independent of process lifecycle."""
import unittest

from agentic_shell_commands import parse_job_command, parse_options


class ShellCommandTests(unittest.TestCase):
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
