import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner.telemetry import (
    combine_usage,
    extract_jsonl_usage,
    MAX_GUIDANCE_RECORDS,
    MAX_GUIDANCE_STATS,
    MAX_INVOCATION_RECORDS,
    MAX_ISSUE_HISTORY_BYTES,
    MAX_OPERATION_RECORDS,
    MAX_POLICY_HISTORY_RECORDS,
    MAX_RUN_RECORD_BYTES,
    read_run_usage,
    describe_issue_change,
    issue_snapshot,
    read_issue_history,
    read_run_instruction,
    record_operation,
    record_issue_metadata,
    read_issue_snapshot,
    record_run_instruction,
    record_security_policy,
    record_usage,
    normalize_usage,
    record_workflow_outcome,
)


class TelemetryTests(unittest.TestCase):
    def test_run_history_retains_only_newest_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            raw = {
                'schema_version': 1,
                'metrics': {
                    'operations': [{'name': f'op-{index}'} for index in range(150)],
                },
                'usage_by_invocation': [{'name': f'inv-{index}'} for index in range(150)],
                'security_policy_history': [{'policy': {'mode': str(index)}} for index in range(75)],
                'guidance_loads': [{'path': f'guide-{index}'} for index in range(150)],
                'guidance_stats': [{'phase': str(index)} for index in range(150)],
            }
            (folder / 'run.json').write_text(json.dumps(raw), encoding='utf-8')

            record_workflow_outcome(
                folder, approved=False, attempts=1, first_pass=False,
            )
            stored = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(len(stored['metrics']['operations']), MAX_OPERATION_RECORDS)
            self.assertEqual(stored['metrics']['operations'][0]['name'], 'op-50')
            self.assertEqual(len(stored['usage_by_invocation']), MAX_INVOCATION_RECORDS)
            self.assertEqual(stored['usage_by_invocation'][0]['name'], 'inv-50')
            self.assertEqual(len(stored['security_policy_history']), MAX_POLICY_HISTORY_RECORDS)
            self.assertEqual(len(stored['guidance_loads']), MAX_GUIDANCE_RECORDS)
            self.assertEqual(len(stored['guidance_stats']), MAX_GUIDANCE_STATS)

    def test_oversized_legacy_run_record_is_not_read_or_rewritten_unbounded(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'run.json').write_text(
                json.dumps({'schema_version': 1, 'junk': 'x' * MAX_RUN_RECORD_BYTES}),
                encoding='utf-8',
            )

            usage = read_run_usage(folder)
            self.assertIsNone(usage['total_tokens'])
            record_operation(folder, 'tests', 'test-command-001')
            self.assertLessEqual((folder / 'run.json').stat().st_size, MAX_RUN_RECORD_BYTES)
            self.assertEqual(
                len(json.loads((folder / 'run.json').read_text())['metrics']['operations']), 1,
            )

    def test_run_record_loader_uses_bounded_descriptor_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "run.json").write_text(
                json.dumps({"schema_version": 1, "usage": {"total_tokens": 7}}),
                encoding="utf-8",
            )
            with patch.object(Path, "stat", side_effect=AssertionError("stat race")), \
                 patch.object(Path, "read_text", side_effect=AssertionError("unbounded read")):
                usage = read_run_usage(folder)
        self.assertEqual(usage["total_tokens"], 7)

    def test_small_legacy_extension_fields_survive_telemetry_write(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            extension = {"source": "legacy-provider", "enabled": True}
            (folder / "run.json").write_text(
                json.dumps({"schema_version": 1, "provider_extension": extension}),
                encoding="utf-8",
            )

            record_workflow_outcome(
                folder, approved=False, attempts=1, first_pass=False,
            )

            stored = json.loads((folder / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["provider_extension"], extension)

    def test_issue_metadata_is_bounded_and_available_to_dashboard_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_issue_metadata(
                folder, number=71, title="Dashboard issue", state="open",
            )
            stored = json.loads((folder / "run.json").read_text())
            self.assertEqual(stored["issue_metadata"]["number"], 71)
            self.assertEqual(stored["issue_metadata"]["state"], "OPEN")
            self.assertEqual(read_run_usage(folder)["issue_metadata"]["title"], "Dashboard issue")

    def test_issue_metadata_redacts_secret_bearing_titles_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_issue_metadata(
                folder,
                number=71,
                title="Investigate api_key=title-secret-value in dashboard metadata",
                state="open",
            )

            raw = json.loads((folder / "run.json").read_text())
            title = raw["issue_metadata"]["title"]
            self.assertNotIn("title-secret-value", json.dumps(raw))
            self.assertEqual(
                title,
                "Investigate api_key=[REDACTED] in dashboard metadata",
            )

    def test_issue_snapshot_is_bounded_redacted_and_readable_for_offline_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_issue_metadata(
                folder, number=72, title="Offline resume", state="open",
                body="api_key=issue-secret-value\n" + ("x" * 20_000),
            )
            snapshot = read_issue_snapshot(folder)
            self.assertEqual(snapshot["number"], 72)
            self.assertNotIn("issue-secret-value", json.dumps(snapshot))
            self.assertLessEqual(len(snapshot["body"]), 12_000)
            self.assertNotIn("issue-snapshot.json", json.dumps(read_run_usage(folder)))

    def test_issue_change_history_preserves_previous_snapshot_and_bounded_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            old = issue_snapshot(
                number=73, title="Original requirement", body="Keep the old behavior.",
                state="OPEN", issue_digest="old-digest",
            )
            new = issue_snapshot(
                number=73, title="Updated requirement", body="Add the new behavior.",
                state="OPEN", issue_digest="new-digest",
            )
            change = describe_issue_change(
                old, new, current_digest="new-digest",
            )
            self.assertIsNotNone(change)
            self.assertEqual(change["changed_fields"], ["title", "body"])
            self.assertIn("title and body", change["summary"])
            record_issue_metadata(
                folder, number=73, title=new["title"], body=new["body"],
                state="OPEN", issue_digest="new-digest", issue_change=change,
            )
            history = read_issue_history(folder)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["previous_snapshot"]["title"], "Original requirement")
            self.assertEqual(history[0]["current_snapshot"]["body"], "Add the new behavior.")
            self.assertEqual(read_issue_snapshot(folder)["title"], "Updated requirement")

    def test_legacy_issue_snapshot_without_digest_still_records_field_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_issue_metadata(
                folder, number=74, title="Before", body="Original", state="OPEN",
            )
            record_issue_metadata(
                folder, number=74, title="After", body="Original", state="OPEN",
            )
            history = read_issue_history(folder)
            self.assertEqual(history[-1]["changed_fields"], ["title"])

    def test_issue_change_history_is_redacted_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_issue_metadata(
                folder, number=75, title="api_key=old-secret", body="old body", state="OPEN",
                issue_digest="old",
            )
            record_issue_metadata(
                folder, number=75, title="api_key=new-secret", body="new body", state="OPEN",
                issue_digest="new",
            )
            raw = (folder / "issue-history.json").read_text(encoding="utf-8")
            self.assertNotIn("old-secret", raw)
            self.assertNotIn("new-secret", raw)
            self.assertLessEqual(len(raw.encode("utf-8")), MAX_ISSUE_HISTORY_BYTES)

    def test_run_instruction_is_optional_bounded_and_replaceable(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.assertIsNone(read_run_instruction(folder))
            record_run_instruction(folder, 'first instruction')
            self.assertEqual(read_run_instruction(folder), 'first instruction')
            record_run_instruction(folder, 'second instruction')
            self.assertEqual(read_run_instruction(folder), 'second instruction')
            record_run_instruction(folder, '')
            self.assertIsNone(read_run_instruction(folder))
            self.assertNotIn('instruction', json.loads((folder / 'run.json').read_text()))

    def test_run_instruction_is_bounded_in_durable_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_run_instruction(folder, 'head-' + ('x' * 3_000) + '-tail')
            stored = read_run_instruction(folder)
            self.assertEqual(len(stored), 2_000)
            self.assertIn('head-', stored)
            self.assertIn('-tail', stored)

    def test_extracts_provider_usage_without_guessing_cost(self):
        usage = extract_jsonl_usage(
            json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 4}}),
            provider="openrouter",
        )
        self.assertEqual(usage["total_tokens"], 14)
        self.assertIsNone(usage["estimated_cost_usd"])

    def test_preserves_cache_and_reasoning_usage_details(self):
        usage = normalize_usage({
            "usage": {
                "prompt_tokens": 10, "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
                "cache_write_tokens": 1,
            },
        })
        self.assertEqual(usage["cache_read_tokens"], 3)
        self.assertEqual(usage["cache_write_tokens"], 1)
        self.assertEqual(usage["reasoning_tokens"], 2)

    def test_cumulative_jsonl_usage_uses_final_snapshot(self):
        text = "\n".join([
            json.dumps({"usage_scope": "cumulative", "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}),
            json.dumps({"usage_scope": "cumulative", "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}),
        ])
        usage = extract_jsonl_usage(text, provider="codex")
        self.assertEqual(usage["total_tokens"], 7)

    def test_merges_partial_outer_and_nested_provider_usage(self):
        usage = extract_jsonl_usage(
            json.dumps({
                "usage": {"total_cost": 0.01},
                "result": {"usage": {"input_tokens": 3, "output_tokens": 4}},
            }),
            provider="gemini",
        )
        self.assertEqual(usage["prompt_tokens"], 3)
        self.assertEqual(usage["completion_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 7)
        self.assertEqual(usage["estimated_cost_usd"], 0.01)

    def test_preserves_unknown_token_breakdowns(self):
        usage = combine_usage([{"provider": "provider", "total_tokens": 14}])
        self.assertIsNone(usage["prompt_tokens"])
        self.assertIsNone(usage["completion_tokens"])
        self.assertEqual(usage["total_tokens"], 14)

    def test_record_usage_is_additive_and_backward_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_usage(folder, "developer", combine_usage([{
                "provider": "codex", "prompt_tokens": 10, "completion_tokens": 5,
            }]))
            record_usage(folder, "reviewer", combine_usage([{
                "provider": "openrouter", "prompt_tokens": 3, "completion_tokens": 2,
                "estimated_cost_usd": 0.01,
            }]))
            usage = read_run_usage(folder)
            self.assertEqual(usage["total_tokens"], 20)
            self.assertEqual(usage["estimated_cost_usd"], 0.01)
            self.assertEqual(json.loads((folder / "run.json").read_text())["schema_version"], 1)

    def test_record_usage_keeps_invocation_metadata_without_prompt_text(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_usage(
                folder,
                "developer",
                {"provider": "openrouter", "prompt_tokens": 12, "completion_tokens": 3},
                invocation={"name": "developer-001", "prompt_chars": 8000, "turns": 4},
            )
            raw = json.loads((folder / "run.json").read_text())
            self.assertEqual(raw["usage_by_invocation"][0]["prompt_chars"], 8000)
            self.assertEqual(raw["usage_by_invocation"][0]["turns"], 4)
            self.assertRegex(raw["usage_by_invocation"][0]["recorded_at"], r"^202[0-9]-")
            self.assertNotIn("full prompt", json.dumps(raw["usage_by_invocation"][0]))
            self.assertEqual(read_run_usage(folder)["by_invocation"][0]["name"], "developer-001")

    def test_usage_is_exposed_by_stage_and_outcome(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_usage(
                folder, "investigator",
                {"provider": "codex", "prompt_tokens": 4, "completion_tokens": 1},
                invocation={"name": "investigation-001", "stage": "investigator"},
            )
            record_workflow_outcome(folder, approved=True, attempts=2, first_pass=False, review_findings=1)
            usage = read_run_usage(folder)
            self.assertEqual(usage["by_stage"]["investigator"]["total_tokens"], 5)
            self.assertEqual(usage["workflow_metrics"]["retries"], 1)
            self.assertFalse(usage["workflow_metrics"]["first_pass_approval"])

    def test_workflow_outcome_can_record_review_value_across_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_workflow_outcome(
                folder, approved=True, attempts=2, first_pass=False,
                review_findings=3,
            )
            metrics = read_run_usage(folder)["workflow_metrics"]
            self.assertEqual(metrics["review_findings"], 3)
            self.assertEqual(metrics["retries"], 1)

    def test_record_operation_persists_duration_and_status_without_command_text(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_operation(
                folder, "tests", "test-command-001",
                started_at="2026-01-01T00:00:00.000+00:00",
                finished_at="2026-01-01T00:00:02.500+00:00",
                duration_seconds=2.5, status="completed", exit_code=0,
            )
            record_operation(
                folder, "developer", "developer-001",
                started_at="2026-01-01T00:00:03.000+00:00",
                finished_at="2026-01-01T00:00:05.000+00:00",
                duration_seconds=2, status="failed", exit_code=1,
            )
            metrics = read_run_usage(folder)["metrics"]
            self.assertEqual(metrics["duration_seconds"], 5.0)
            self.assertEqual(metrics["operation_count"], 2)
            self.assertEqual(metrics["operations"][1]["status"], "failed")
            self.assertEqual(metrics["operations"][1]["exit_code"], 1)
            self.assertNotIn("python -c", json.dumps(metrics))

    def test_record_security_policy_is_sanitized_and_backward_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_security_policy(folder, {
                "security_mode": "isolated",
                "sandbox": {
                    "enabled": True,
                    "engine": "docker",
                    "image": "agentic-runner:latest",
                    "allow_network": False,
                    "pass_env": ["OPENAI_API_KEY"],
                },
                "developer_agent": {"provider": "codex"},
                "reviewer_agent": {"provider": "claude"},
            })
            raw = json.loads((folder / "run.json").read_text())
            policy = raw["security_policy"]
            self.assertEqual(policy["mode"], "isolated")
            self.assertEqual(policy["network"], "none")
            self.assertEqual(policy["git_metadata_mount"], "ro")
            self.assertEqual(policy["passed_environment_names"], ["OPENAI_API_KEY"])
            self.assertEqual(read_run_usage(folder)["security_policy"], policy)
            self.assertNotIn("secret-value", json.dumps(raw))

    def test_record_security_policy_records_permission_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_security_policy(folder, {
                "security_mode": "isolated",
                "sandbox": {"enabled": True, "engine": "docker", "image": "runner"},
                "developer_agent": {
                    "provider": "claude",
                    "permission_policy": {"allow_dangerous": True},
                },
                "reviewer_agent": {
                    "provider": "codex",
                    "permission_policy": {"allow_dangerous": False},
                },
            })
            policy = json.loads((folder / "run.json").read_text())["security_policy"]
        self.assertTrue(policy["permission_policy"]["developer"]["allow_dangerous"])
        self.assertFalse(policy["permission_policy"]["reviewer"]["allow_dangerous"])

    def test_record_security_policy_preserves_resume_history(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            record_security_policy(folder, {
                "security_mode": "trusted",
                "sandbox": {"enabled": False},
            })
            record_security_policy(folder, {
                "security_mode": "isolated",
                "sandbox": {
                    "enabled": True,
                    "engine": "docker",
                    "image": "agentic-runner:latest",
                    "allow_network": False,
                    "pass_env": [],
                },
            })
            raw = json.loads((folder / "run.json").read_text())
            self.assertEqual(raw["security_policy"]["mode"], "trusted")
            self.assertEqual(
                [item["policy"]["mode"] for item in raw["security_policy_history"]],
                ["trusted", "isolated"],
            )
            self.assertEqual(
                len(read_run_usage(folder)["security_policy_history"]), 2,
            )


if __name__ == "__main__":
    unittest.main()
