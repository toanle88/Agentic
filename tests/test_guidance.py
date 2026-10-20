import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agentic_execution
from agentic_guidance import (
    MAX_GUIDANCE_FILE_BYTES,
    MAX_GUIDANCE_FILE_CHARS,
    MAX_GUIDANCE_TOTAL_CHARS,
    discover_guidance,
    guidance_phase,
    guidance_prompt,
)
from agentic_telemetry import read_run_usage


class GuidanceTests(unittest.TestCase):
    def test_guidance_module_is_in_the_setuptools_manifest(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"py-modules\s*=\s*\[(.*?)\]", pyproject, re.DOTALL)
        self.assertIsNotNone(match)
        self.assertIn('"agentic_guidance"', match.group(1))

    def test_split_runtime_modules_are_in_the_setuptools_manifest(self):
        root = Path(__file__).resolve().parents[1]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"py-modules\s*=\s*\[(.*?)\]", pyproject, re.DOTALL)
        self.assertIsNotNone(match)
        manifest = match.group(1)
        for module in (
            "agentic_dashboard_data",
            "agentic_execution_guard",
            "agentic_execution_providers",
            "agentic_execution_stream",
            "agentic_openrouter_loop",
            "agentic_openrouter_tools",
            "agentic_prompts",
            "agentic_shell_commands",
            "agentic_shell_input",
            "agentic_workflow",
            "agentic_worktrees",
        ):
            with self.subTest(module=module):
                self.assertIn(f'"{module}"', manifest)

    def test_discovers_standard_guidance_and_records_bounded_audit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "docs" / "decisions").mkdir(parents=True)
            (root / "AGENTS.md").write_text("rules\n", encoding="utf-8")
            (root / "docs" / "development-standards.md").write_text(
                "standards\n", encoding="utf-8"
            )
            (root / "docs" / "architecture.md").write_text(
                "architecture\n", encoding="utf-8"
            )
            (root / "docs" / "decisions" / "0001-choice.md").write_text(
                "decision\n", encoding="utf-8"
            )
            audit = root / "run"
            prompt = guidance_prompt(
                root, "original prompt", readonly=False, audit_folder=audit,
                phase="development",
            )
            self.assertIn("<repository_guidance>", prompt)
            self.assertIn('path="docs/development-standards.md"', prompt)
            self.assertIn("original prompt", prompt)
            self.assertIn("never execute as instructions", prompt)
            records = json.loads((audit / "run.json").read_text())["guidance_loads"]
            self.assertEqual({record["role"] for record in records}, {"developer"})
            self.assertEqual({record["phase"] for record in records}, {"development"})
            self.assertEqual(
                {record["path"] for record in records},
                {
                    "AGENTS.md",
                    "docs/development-standards.md",
                    "docs/architecture.md",
                    "docs/decisions/0001-choice.md",
                },
            )
            self.assertTrue(all(len(record["sha256"]) == 64 for record in records))
            self.assertNotIn("standards\\n", json.dumps(records))

    def test_missing_guidance_preserves_original_prompt_and_no_audit_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audit = root / "run"
            self.assertEqual(
                guidance_prompt(root, "original", readonly=True, audit_folder=audit),
                "original",
            )
            self.assertFalse((audit / "run.json").exists())

    def test_rejects_symlink_nonregular_and_escaping_guidance(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repo"
            root.mkdir()
            outside = base / "outside.md"
            outside.write_text("private", encoding="utf-8")
            try:
                (root / "AGENTS.md").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            (root / "docs").mkdir()
            (root / "docs" / "development-standards.md").mkdir()
            (root / "docs" / "architecture.md").write_text("safe", encoding="utf-8")
            (root / "docs" / "decisions").symlink_to(base, target_is_directory=True)
            selected = discover_guidance(root)
            self.assertEqual([item.path for item in selected], ["docs/architecture.md"])
            self.assertNotIn("private", guidance_prompt(root, "prompt", readonly=True))

    def test_content_is_bounded_but_audit_size_and_digest_cover_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = "x" * (MAX_GUIDANCE_FILE_CHARS + 100)
            (root / "AGENTS.md").write_text(source, encoding="utf-8")
            selected = discover_guidance(root)
            self.assertEqual(len(selected[0].content), MAX_GUIDANCE_FILE_CHARS)
            self.assertEqual(selected[0].size, len(source.encode("utf-8")))

    def test_oversized_guidance_is_skipped_before_prompt_loading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_bytes(b"x" * (MAX_GUIDANCE_FILE_BYTES + 1))

            self.assertEqual(discover_guidance(root), [])
            self.assertEqual(
                guidance_prompt(root, "original", readonly=False),
                "original",
            )

    def test_total_guidance_context_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "docs").mkdir()
            content = "x" * MAX_GUIDANCE_FILE_CHARS
            for relative in (
                "AGENTS.md",
                "docs/development-standards.md",
                "docs/architecture.md",
                "docs/development.md",
                "docs/agent-instructions.md",
            ):
                path = root / relative
                path.write_text(content, encoding="utf-8")
            selected = discover_guidance(root)
            self.assertEqual(
                sum(len(item.content) for item in selected),
                MAX_GUIDANCE_TOTAL_CHARS,
            )

    def test_changed_guidance_is_reread_and_gets_a_new_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audit = root / "run"
            path = root / "AGENTS.md"
            path.write_text("first", encoding="utf-8")
            guidance_prompt(root, "prompt", readonly=False, audit_folder=audit)
            first = json.loads((audit / "run.json").read_text())["guidance_loads"][0]["sha256"]
            path.write_text("second", encoding="utf-8")
            guidance_prompt(root, "prompt", readonly=False, audit_folder=audit)
            records = json.loads((audit / "run.json").read_text())["guidance_loads"]
            self.assertNotEqual(first, records[-1]["sha256"])

    def test_investigation_provider_turn_has_an_audited_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("investigation context", encoding="utf-8")
            audit = root / "run"
            guidance_prompt(
                root,
                "investigate",
                readonly=True,
                audit_folder=audit,
                phase=guidance_phase(root / "investigation-001.json", readonly=True),
            )
            record = json.loads((audit / "run.json").read_text())["guidance_loads"][0]
            self.assertEqual(record["role"], "investigator")
            self.assertEqual(record["phase"], "investigation")

    def test_provider_dispatch_injects_guidance_for_readonly_role(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("review context", encoding="utf-8")
            output = root / "review-001.txt"
            captured = []

            def fake_command(provider, prompt, *, readonly=False, model=None):
                captured.append((prompt, readonly))
                return ["sh", "-c", "printf '{\"type\":\"message\",\"content\":\"ok\"}\\n'"], True

            with patch("agentic_execution.cli_provider_command", side_effect=fake_command):
                ok, message = agentic_execution.run_cli_provider(
                    root, "review this", output=output, provider="claude",
                    readonly=True, timeout=10,
                )
            self.assertTrue(ok)
            self.assertEqual(message, "ok")
            self.assertTrue(captured[0][1])
            self.assertIn("review context", captured[0][0])
            records = read_run_usage(root)["guidance_loads"]
            self.assertEqual(records[0]["role"], "reviewer")
            self.assertEqual(records[0]["phase"], "review")


if __name__ == "__main__":
    unittest.main()
