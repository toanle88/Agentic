#!/usr/bin/env python3
"""Deterministic provider stand-in for exercising the benchmark harness.

This is a workflow fixture, not an agent-quality result. It makes the corpus
and metric pipeline runnable without credentials or network access.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def write_result(path: Path, **values: object) -> None:
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    result = Path(args.result_file)
    if args.mode == "reviewer":
        write_result(
            result,
            status="success",
            completed=True,
            approved=False,
            attempts=1,
            interventions=0,
            recovered=False,
            review={"approved": False, "finding_ids": ["signed-subtract-bug"]},
            usage={"total_tokens": 80, "estimated_cost_usd": 0.0},
        )
        return 0

    (repo / "calculator.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def subtract(a, b):\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    if args.mode == "agentic":
        (repo / "tests" / "test_calculator.py").write_text(
            "import unittest\n\n"
            "from calculator import add, subtract\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n\n"
            "    def test_subtract_positive(self):\n"
            "        self.assertEqual(subtract(5, 2), 3)\n\n"
            "    def test_subtract_negative(self):\n"
            "        self.assertEqual(subtract(2, 5), -3)\n\n"
            "    def test_subtract_zero(self):\n"
            "        self.assertEqual(subtract(5, 5), 0)\n",
            encoding="utf-8",
        )
    write_result(
        result,
        status="success",
        completed=True,
        approved=True,
        attempts=2 if args.mode == "agentic" else 1,
        interventions=0,
        recovered=args.mode == "agentic",
        review={"approved": True, "finding_ids": []},
        usage={"total_tokens": 100 if args.mode == "agentic" else 60, "estimated_cost_usd": 0.0},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("alone", "agentic", "reviewer"), required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--task-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--reviewer")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
