import os
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_pyright_targets_packaged_runtime_and_retained_wrappers(self):
        config = json.loads((ROOT / "pyrightconfig.json").read_text(encoding="utf-8"))
        self.assertEqual(
            config["include"],
            [
                "src/agentic_runner",
                "agentic.py",
                "agentic_dashboard.py",
                "agentic_shell.py",
            ],
        )

    def test_console_scripts_and_src_package_are_declared(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('agentic = "agentic_runner.cli:main"', pyproject)
        self.assertIn('agentic-shell = "agentic_runner.shell:main"', pyproject)
        self.assertIn('agentic-dashboard = "agentic_runner.dashboard:main"', pyproject)
        self.assertIn('package-dir = {"" = "src"}', pyproject)
        self.assertNotIn("py-modules =", pyproject)

    def test_wheel_contains_package_and_runs_outside_checkout(self):
        if importlib.util.find_spec("wheel") is None:
            self.skipTest("wheel is unavailable; CI installs the package build tooling")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    str(folder),
                    str(ROOT),
                ],
                cwd=folder,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                self.fail(f"wheel build failed:\n{result.stdout}\n{result.stderr}")
            wheels = sorted(folder.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
                self.assertIn("agentic_runner/__init__.py", names)
                self.assertIn("agentic_runner/cli.py", names)
                metadata = archive.read(next(name for name in names if name.endswith("METADATA"))).decode()
            self.assertIn("Version: 1.1.97", metadata)

            extracted = folder / "installed"
            with zipfile.ZipFile(wheels[0]) as archive:
                archive.extractall(extracted)
            probe_environment = os.environ.copy()
            probe_environment["PYTHONPATH"] = str(extracted)
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import agentic_runner, agentic_runner.cli; print(agentic_runner.__version__)",
                ],
                cwd=tempfile.gettempdir(),
                env=probe_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "1.1.97")

    def test_root_command_wrappers_remain_thin_compatibility_entrypoints(self):
        wrapper = (ROOT / "agentic.py").read_text(encoding="utf-8")
        self.assertIn("from agentic_runner import cli as _implementation", wrapper)
        self.assertNotIn("def main", wrapper)

    def test_runtime_modules_import_without_repository_root(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import importlib, pkgutil, agentic_runner; "
                    "[importlib.import_module(item.name) for item in "
                    "pkgutil.iter_modules(agentic_runner.__path__, 'agentic_runner.')]; "
                    "print(agentic_runner.__version__)"
                ),
            ],
            cwd=tempfile.gettempdir(),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "1.1.97")


if __name__ == "__main__":
    unittest.main()
