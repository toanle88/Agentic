"""Test-package bootstrap for the source-layout runtime."""

import sys
import importlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# A few characterization tests retain string-based patch targets from the
# pre-src layout. Point those targets at the canonical package modules while
# keeping all test imports and runtime imports package-qualified.
for _legacy, _canonical in {
    "agentic_config": "agentic_runner.config",
    "agentic_context": "agentic_runner.context",
    "agentic_diagnostics": "agentic_runner.diagnostics",
    "agentic_dashboard_data": "agentic_runner.dashboard_data",
    "agentic_execution": "agentic_runner.execution",
    "agentic_execution_stream": "agentic_runner.execution_stream",
    "agentic_guidance": "agentic_runner.guidance",
    "agentic_models": "agentic_runner.models",
    "agentic_openrouter": "agentic_runner.openrouter",
    "agentic_openrouter_tools": "agentic_runner.openrouter_tools",
    "agentic_persistence": "agentic_runner.persistence",
    "agentic_providers": "agentic_runner.providers",
    "agentic_review": "agentic_runner.review",
    "agentic_sandbox": "agentic_runner.sandbox",
    "agentic_security": "agentic_runner.security",
    "agentic_shell_input": "agentic_runner.shell_input",
    "agentic_shell_jobs": "agentic_runner.shell_jobs",
    "agentic_telemetry": "agentic_runner.telemetry",
    "agentic_test_discovery": "agentic_runner.test_discovery",
    "agentic_workflow": "agentic_runner.workflow",
    "agentic_workflow_state": "agentic_runner.workflow_state",
    "agentic_worktrees": "agentic_runner.worktrees",
}.items():
    sys.modules.setdefault(_legacy, importlib.import_module(_canonical))
