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
        cls.docs_workflow = (WORKFLOWS / "docs.yml").read_text(encoding="utf-8")
        cls.release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        cls.pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        cls.mkdocs = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

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

    def test_documentation_workflow_builds_strictly_and_deploys_pinned_pages(self):
        jobs = _job_blocks(self.docs_workflow)
        self.assertEqual(set(jobs), {"build", "deploy"})
        for job, block in jobs.items():
            self.assertRegex(block, r"(?m)^    timeout-minutes: [1-9][0-9]*$", msg=job)
        self.assertIn("branches:\n      - main", self.docs_workflow)
        self.assertIn("python -m mkdocs build --strict --site-dir site", self.docs_workflow)
        self.assertIn('python -m pip install --upgrade ".[docs]"', self.docs_workflow)
        for action in (
            "actions/checkout@",
            "actions/setup-python@",
            "actions/configure-pages@",
            "actions/upload-pages-artifact@",
            "actions/deploy-pages@",
        ):
            self.assertRegex(self.docs_workflow, rf"{re.escape(action)}[0-9a-f]{{40}}")
        self.assertIn(
            "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
            self.docs_workflow,
        )
        self.assertIn("contents: read", self.docs_workflow)
        self.assertIn("pages: write", self.docs_workflow)
        self.assertIn("id-token: write", self.docs_workflow)

    def test_mkdocs_navigation_covers_the_public_documentation_structure(self):
        for path in (
            "index.md",
            "getting-started.md",
            "quickstart.md",
            "cli-reference.md",
            "configuration.md",
            "provider-authorization.md",
            "sandbox.md",
            "workflow.md",
            "architecture.md",
            "troubleshooting.md",
            "demo.md",
            "development.md",
            "development-standards.md",
            "agent-instructions.md",
            "decisions/README.md",
        ):
            self.assertIn(f": {path}", self.mkdocs)
        self.assertIn("name: material", self.mkdocs)
        self.assertIn("site_url: https://toanle88.github.io/Agentic/", self.mkdocs)

    def test_documentation_dependency_stays_out_of_runtime_dependencies(self):
        self.assertIn("dependencies = []", self.pyproject)
        self.assertIn("docs = [", self.pyproject)
        self.assertIn('"mkdocs-material==9.6.22"', self.pyproject)

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
