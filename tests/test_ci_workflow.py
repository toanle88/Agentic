"""Static regression coverage for the GitHub Actions verification contract."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _job_blocks(text: str) -> dict[str, str]:
    jobs = text.split("jobs:\n", 1)[1]
    matches = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\n(?=    )", jobs, re.MULTILINE))
    return {
        match.group(1): jobs[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(jobs)]
        for index, match in enumerate(matches)
    }


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        cls.codeql = (WORKFLOWS / "codeql.yml").read_text(encoding="utf-8")
        cls.release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        cls.pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    def test_every_ci_job_has_a_finite_timeout(self):
        jobs = _job_blocks(self.ci)
        self.assertEqual(
            set(jobs), {"test", "windows", "package", "lint", "coverage", "sandbox"}
        )
        for job, block in jobs.items():
            self.assertRegex(block, r"(?m)^    timeout-minutes: [1-9][0-9]*$", msg=job)

    def test_codeql_and_release_jobs_have_finite_timeouts(self):
        for workflow in (self.codeql, self.release):
            for job, block in _job_blocks(workflow).items():
                self.assertRegex(block, r"(?m)^    timeout-minutes: [1-9][0-9]*$", msg=job)

    def test_ci_has_weekly_schedule_and_coverage_gate_artifact_and_sdist_smoke(self):
        self.assertIn("schedule:\n    - cron: \"17 3 * * 1\"", self.ci)
        self.assertIn("coverage report --fail-under=70 -m", self.ci)
        self.assertIn("coverage xml -o coverage.xml", self.ci)
        self.assertIn("actions/upload-artifact@", self.ci)
        self.assertIn("name: coverage-report", self.ci)
        self.assertIn("dist/*.tar.gz", self.ci)
        self.assertIn("--no-build-isolation", self.ci)
        self.assertIn("Smoke-test wheel and sdist in clean environments", self.release)
        self.assertIn("dist/*.whl", self.release)
        self.assertIn("dist/*.tar.gz", self.release)

    def test_ci_tool_versions_match_development_dependencies(self):
        for requirement in (
            '"build==1.6.1"',
            '"coverage==7.6.10"',
            '"pyright==1.1.414"',
            '"ruff==0.17.2"',
        ):
            self.assertIn(requirement, self.pyproject)
            self.assertIn(requirement, self.ci)
        self.assertIn('requires = ["setuptools==84.0.0"]', self.pyproject)
        self.assertIn('"setuptools==84.0.0"', self.ci)

    def test_documentation_names_required_checks_and_runner_drift_policy(self):
        development = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
        for check in (
            "Test (Python 3.10)",
            "Test (Python 3.11)",
            "Test (Python 3.12)",
            "Test (Python 3.13)",
            "Test (Python 3.14)",
            "Native Windows (Python 3.12)",
            "Build package",
            "Lint (Ruff)",
            "Coverage (Python 3.12)",
            "Docker sandbox integration",
            "Analyze Python",
        ):
            self.assertIn(check, development)
        self.assertIn("ubuntu-latest", development)
        self.assertIn("windows-latest", development)
        self.assertIn("accepted image-drift policy", development)
        self.assertIn("70%", development)


if __name__ == "__main__":
    unittest.main()
