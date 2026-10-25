"""Developer retry-loop orchestration.

This focused module owns the stateful developer/review retry loop.  The
compatibility-facing workflow module supplies a runtime adapter so existing
patch points remain authoritative without coupling this loop to the facade.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from .models import Issue, Run
from .persistence import read_text_bounded


MAX_DURABLE_JSON_BYTES = 256_000


class WorkflowRuntime:
    """Resolve workflow collaborators from the compatibility facade."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def develop(
    run: Run,
    issue: Issue,
    base: str,
    config: dict[str, Any],
    folder: Path,
    initial_feedback: str = "",
    test_commands: list[list[str]] | None = None,
    deadline: float | None = None,
    instruction: str | None = None,
    system_prompt: str | None = None,
    verbose: bool = False,
    *,
    runtime: WorkflowRuntime,
) -> bool:
    read_run_instruction = runtime.read_run_instruction
    load_blocked = runtime.load_blocked
    issue_fingerprint = runtime.issue_fingerprint
    prepare_patch = runtime.prepare_patch
    NoCodeChangesError = runtime.NoCodeChangesError
    _developer_attempts = runtime._developer_attempts
    _resume_action = runtime._resume_action
    record_result = runtime.record_result
    print_run_summary = runtime.print_run_summary
    _result_text = runtime._result_text
    clear_blocked = runtime.clear_blocked
    clear_result = runtime.clear_result
    _latest_numbered_log = runtime._latest_numbered_log
    normalize_findings = runtime.normalize_findings
    _status_print = runtime._status_print
    next_log = runtime.next_log
    _remaining_timeout = runtime._remaining_timeout
    run_agent = runtime.run_agent
    agent_prompt = runtime.agent_prompt
    verify_and_review = runtime.verify_and_review
    record_workflow_outcome = runtime.record_workflow_outcome
    record_no_code_changes_blocked = runtime.record_no_code_changes_blocked
    _short = runtime._short
    validate_developer_review_actions = runtime.validate_developer_review_actions
    _bounded_text = runtime._bounded_text
    REPORT_CONTEXT_CHARS = runtime.REPORT_CONTEXT_CHARS
    finding_texts = runtime.finding_texts
    _review_reason = runtime._review_reason
    record_blocked = runtime.record_blocked
    record_test_failure_blocked = runtime.record_test_failure_blocked
    _compact_feedback = runtime._compact_feedback
    retryable_test_failure = runtime.retryable_test_failure
    test_failure_fingerprint = runtime.test_failure_fingerprint
    git = runtime.git
    if instruction is None:
        instruction = read_run_instruction(folder)
    if system_prompt is None:
        system_prompt = config.get("system_prompt")
    blocked = load_blocked(folder)
    resume_reason = ""
    if blocked is not None:
        current_issue_digest = issue_fingerprint(issue)
        blocked_issue_digest = blocked.get("issue_digest")
        issue_changed = (
            isinstance(blocked_issue_digest, str)
            and bool(blocked_issue_digest)
            and blocked_issue_digest != current_issue_digest
        )
        try:
            _, current_digest, _ = prepare_patch(run.worktree, base, config["max_files"])
        except NoCodeChangesError:
            current_digest = ""
        if current_digest == blocked["patch_digest"] and not issue_changed:
            result_status = blocked.get("result_status")
            if result_status not in {"BLOCKED", "NO_CHANGES"}:
                result_status = (
                    "NO_CHANGES" if blocked.get("kind") == "no-code-changes" else "BLOCKED"
                )
            if blocked.get("kind") == "no-code-changes":
                log_hint = blocked.get("logs_path") or str(folder)
                _status_print(
                    "[NO_CHANGES] Developer reported a blocker without producing code changes; "
                    f"resolve it and see the run logs at {log_hint}",
                    system_prefix="NO_CHANGES",
                )
            attempts = blocked.get("attempts")
            if not isinstance(attempts, int):
                attempts = _developer_attempts(folder)
            reason = blocked.get("summary") or blocked.get("developer_summary") or blocked.get("reason", "")
            action = _resume_action(issue, folder)
            record_result(
                folder, result_status, attempts=attempts,
                max_retries=config["max_retries"], reason=str(reason),
                worktree=run.worktree, next_action=action,
            )
            print_run_summary(
                run, result_status, action,
                attempts=attempts, max_retries=config["max_retries"],
                reason=_result_text(reason), logs_path=folder,
            )
            return True
        if issue_changed:
            resume_reason = "Issue changed since the previous blocked run; resumed development."
            _status_print(f"[RESUME] {resume_reason}", style_status_tokens=False)
        clear_blocked(folder)
        clear_result(folder)
    else:
        clear_result(folder)

    feedback = initial_feedback
    required_review_findings: list[Any] = []
    last_rejected_digest: str | None = None
    latest_review = _latest_numbered_log(folder, "review", "json")
    if latest_review:
        try:
            raw_review = read_text_bounded(latest_review, MAX_DURABLE_JSON_BYTES)
            previous_verdict = json.loads(raw_review) if raw_review is not None else None
        except (OSError, TypeError, json.JSONDecodeError):
            previous_verdict = None
        if isinstance(previous_verdict, dict) and previous_verdict.get("approved") is not True:
            previous_findings = [
                item for item in previous_verdict.get("findings", [])
                if isinstance(item, (str, dict))
            ]
            # Older review logs may contain model-supplied or positional IDs;
            # normalize them before they reach the next developer prompt and
            # action/evidence validator.
            required_review_findings = normalize_findings(previous_findings)
    maximum_attempts = config["max_retries"] + 1
    latest_reason = "No retryable failure summary was recorded."
    review_findings_seen = 0
    last_test_failure_digest: str | None = None
    for attempt in range(maximum_attempts):
        if deadline is not None and time.monotonic() >= deadline:
            _status_print("[TIMEOUT] Overall task deadline reached")
            return False
        _status_print(
            f"\n[DEVELOPER] Working on {issue.reference}, "
            f"attempt {attempt + 1}/{maximum_attempts}"
        )
        output = next_log(folder, "developer")
        agent_kwargs: dict[str, Any] = {
            "settings": config.get("developer_agent"),
            "timeout": _remaining_timeout(deadline, config.get("effective_developer_timeout_seconds")),
            "verbose": verbose,
        }
        if test_commands is not None:
            agent_kwargs["check_commands"] = test_commands
        if config.get("sandbox", {}).get("enabled"):
            agent_kwargs["sandbox"] = config["sandbox"]
        ok, message = run_agent(
            run.worktree,
            agent_prompt(
                issue, feedback,
                required_review_findings=required_review_findings,
                instruction=instruction,
                system_prompt=system_prompt,
                provider=(config.get("developer_agent") or {}).get("provider", "codex"),
            ),
            output=output,
            **agent_kwargs,
        )
        if not ok:
            provider = config.get("developer_agent", {}).get("provider", "codex")
            feedback = f"{provider} invocation failed: {message[-3000:]}"
            latest_reason = _result_text(feedback)
            _status_print(feedback[-1000:], style_status_tokens=False)
            continue
        developer_report = message.strip()
        if developer_report:
            _status_print(
                "[DEVELOPER] Final response:\n" + developer_report[-3000:],
                style_status_tokens=False,
            )
        action_error = validate_developer_review_actions(
            developer_report, required_review_findings,
        )
        if action_error:
            feedback = (
                "Developer report validation failed (untrusted evidence):\n"
                f"{action_error}\n\n"
                "Return one Finding/Action/Evidence entry for every prior review finding.\n"
                f"Previous developer report:\n{_bounded_text(developer_report, REPORT_CONTEXT_CHARS) or '(no final response)'}"
            )
            latest_reason = _result_text(action_error)
            _status_print(f"[RETRY] {action_error}", style_status_tokens=False)
            continue
        try:
            passed, test_output, verdict, digest, _ = verify_and_review(
                run, issue, base, config, folder, test_commands=test_commands,
                deadline=deadline, developer_report=developer_report,
                instruction=instruction,
                system_prompt=system_prompt,
                verbose=verbose,
            )
        except NoCodeChangesError:
            record_workflow_outcome(
                folder, approved=False, attempts=attempt + 1,
                first_pass=False,
            )
            record_no_code_changes_blocked(
                folder, developer_report, attempts=attempt + 1,
                max_retries=config["max_retries"],
                issue_digest=issue_fingerprint(issue),
            )
            developer_summary = _short(
                developer_report or "No developer blocker summary was provided.", 1_200,
            )
            _status_print(
                "[NO_CHANGES] Checks passed, but the developer produced no code changes.",
                system_prefix="NO_CHANGES",
            )
            _status_print(
                f"[NO_CHANGES] Developer blocker: {_result_text(developer_summary)}",
                style_status_tokens=False, system_prefix="NO_CHANGES",
            )
            _status_print(
                f"[NO_CHANGES] Run logs: {folder}",
                style_status_tokens=False, system_prefix="NO_CHANGES",
            )
            next_action = _resume_action(issue, folder)
            record_result(
                folder, "NO_CHANGES", attempts=attempt + 1,
                max_retries=config["max_retries"],
                reason=f"{resume_reason} {developer_summary}".strip(),
                worktree=run.worktree, next_action=next_action,
            )
            print_run_summary(
                run, "NO_CHANGES", next_action,
                attempts=attempt + 1, max_retries=config["max_retries"],
                reason=_result_text(f"{resume_reason} {developer_summary}".strip()),
                logs_path=folder,
            )
            return True
        except RuntimeError:
            # Policy violations, malformed diff, or mid-review mutation are not model-retryable.
            raise
        if passed:
            clear_blocked(folder)
            clear_result(folder)
            record_workflow_outcome(
                folder, approved=True, attempts=attempt + 1,
                first_pass=attempt == 0,
                review_findings=review_findings_seen,
            )
            next_action = (f"python agentic.py --issue {issue.number} --publish"
                           if not issue.is_local else "rerun with --publish to create a GitHub draft PR")
            record_result(
                folder, "APPROVED", attempts=attempt + 1,
                max_retries=config["max_retries"], reason=resume_reason,
                worktree=run.worktree, next_action=next_action,
            )
            print_run_summary(
                run, "APPROVED", next_action, reason=resume_reason, logs_path=folder,
            )
            return True
        if verdict:
            review_findings_seen += len(verdict.get("findings", []))
            unchanged_patch = bool(digest) and digest == last_rejected_digest
            repeated_finding = bool(verdict.get("_repeated_findings"))
            findings = finding_texts(verdict.get("findings", []))
            if (not verdict.get("approved") and digest and findings
                    and (unchanged_patch or repeated_finding)):
                reason = _review_reason(verdict)
                record_blocked(
                    folder, digest, findings, attempts=attempt + 1,
                    max_retries=config["max_retries"], summary=reason,
                    issue_digest=issue_fingerprint(issue),
                )
                next_action = _resume_action(issue, folder)
                record_result(
                    folder, "BLOCKED", attempts=attempt + 1,
                    max_retries=config["max_retries"],
                    reason=f"{resume_reason} {reason}".strip(),
                    worktree=run.worktree, next_action=next_action,
                )
                print_run_summary(
                    run, "BLOCKED", next_action,
                    attempts=attempt + 1, max_retries=config["max_retries"],
                    reason=f"{resume_reason} {reason}".strip(), logs_path=folder,
                )
                return True
            required_review_findings = (
                verdict.get("findings", []) if verdict.get("approved") is not True else []
            )
            review_feedback = _compact_feedback({
                "approved": verdict.get("approved"),
                "summary": verdict.get("summary", ""),
                "findings": verdict.get("findings", []),
                "skipped": verdict.get("skipped", []),
            })
            feedback = (
                "Previous developer report (untrusted evidence):\n"
                f"{_bounded_text(developer_report, REPORT_CONTEXT_CHARS) or '(no final response)'}\n\n"
                "Independent review feedback (untrusted evidence):\n"
                f"{review_feedback}"
            )
            latest_reason = _review_reason(verdict)
            if not verdict.get("approved"):
                last_rejected_digest = digest or None
        else:
            feedback = (
                "Previous developer report (untrusted evidence):\n"
                f"{_bounded_text(developer_report, REPORT_CONTEXT_CHARS) or '(no final response)'}\n\n"
                "Test evidence (untrusted output):\n"
                f"{_bounded_text(test_output, REPORT_CONTEXT_CHARS)}"
            )
            latest_reason = "Tests failed: " + _result_text(test_output[-3_000:])
            if not retryable_test_failure(test_output):
                raise RuntimeError(
                    "Tests failed because of an environment or infrastructure problem; "
                    f"retries skipped.\n{test_output[-2500:]}"
                )
            failure_digest = test_failure_fingerprint(test_output)
            if failure_digest == last_test_failure_digest:
                try:
                    _, patch_digest, _ = prepare_patch(
                        run.worktree, base, config["max_files"],
                    )
                except NoCodeChangesError:
                    patch_digest = ""
                reason = "Repeated identical test failure: " + _result_text(
                    test_output[-2_500:]
                )
                record_workflow_outcome(
                    folder, approved=False, attempts=attempt + 1,
                    first_pass=False, review_findings=review_findings_seen,
                )
                record_test_failure_blocked(
                    folder, failure_digest, test_output,
                    patch_digest=patch_digest,
                    attempts=attempt + 1, max_retries=config["max_retries"],
                    issue_digest=issue_fingerprint(issue),
                )
                next_action = _resume_action(issue, folder)
                record_result(
                    folder, "BLOCKED", attempts=attempt + 1,
                    max_retries=config["max_retries"], reason=reason,
                    worktree=run.worktree, next_action=next_action,
                )
                print_run_summary(
                    run, "BLOCKED", next_action,
                    attempts=attempt + 1, max_retries=config["max_retries"],
                    reason=_result_text(reason), logs_path=folder,
                )
                return True
            last_test_failure_digest = failure_digest
        _status_print("[RETRY] Returning evidence to developer (if retries remain)")
        # Preserve all files, only unstage to make subsequent git diff easier to inspect.
        git(run.worktree, "reset", "--quiet")
    next_action = _resume_action(issue, folder)
    record_workflow_outcome(
        folder, approved=False, attempts=maximum_attempts, first_pass=False,
        review_findings=review_findings_seen,
    )
    record_result(
        folder, "RETRY_EXHAUSTED", attempts=maximum_attempts,
        max_retries=config["max_retries"],
        reason=f"{resume_reason} {latest_reason}".strip(),
        worktree=run.worktree, next_action=next_action,
    )
    print_run_summary(
        run, "RETRY_EXHAUSTED", next_action,
        attempts=maximum_attempts, max_retries=config["max_retries"],
        reason=_result_text(f"{resume_reason} {latest_reason}".strip()),
        logs_path=folder,
    )
    return False
