"""The coverage gate fails below its configured threshold, and the gates run it unweakened.

`coverage report` reads fail_under from pyproject.toml; the Makefile test target and the CI test
step run it. The planted cases run coverage with that same threshold over a module whose branch
coverage is just below and at it.
"""

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Ten functions of four statements and two branch arms each: 40 statements, 20 arms.
MODULE = "".join(f"def f{i}(x):\n    if x:\n        return 1\n    return 0\n\n\n" for i in range(10))


def configured_threshold():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["tool"]["coverage"]["report"]["fail_under"]


def driver(calls):
    """A driver script that exercises the given (function, argument) pairs of the module."""
    return "import planted\n" + "".join(f"planted.f{i}({arg})\n" for i, arg in calls)


def run_gate(calls):
    """Run coverage over the planted module with the repository's threshold; return the report."""
    with tempfile.TemporaryDirectory(prefix="jellysin-coverage-gate-") as name:
        work = Path(name)
        (work / "planted.py").write_text(MODULE, encoding="utf-8")
        (work / "drive.py").write_text(driver(calls), encoding="utf-8")
        (work / "gate.toml").write_text(
            f'[tool.coverage.run]\nsource = ["planted"]\nbranch = true\n\n'
            f"[tool.coverage.report]\nfail_under = {configured_threshold()}\n",
            encoding="utf-8",
        )
        ran = coverage(work, "run", "drive.py")
        if ran.returncode:
            raise AssertionError(ran.stderr.decode())
        return coverage(work, "report")


def coverage(work, command, *arguments):
    """Run one coverage command over the planted data in work."""
    options = ["--rcfile=gate.toml", "--data-file=.planted-coverage"]
    return subprocess.run(
        [sys.executable, "-m", "coverage", command, *options, *arguments],
        cwd=work,
        capture_output=True,
        check=False,
        timeout=60,
    )


# Calling every function with a true argument runs 30 of the 40 statements and 10 of the 20 arms;
# each false call adds one statement and one arm. coverage counts both in its total, so n false
# calls give (40 + 2n) / 60: n = 5 is 83.3 %, the nearest below 85, and n = 6 is 86.7 %.
def calls_covering(false_calls):
    return [(i, 1) for i in range(10)] + [(i, 0) for i in range(false_calls)]


class CoverageGateTests(unittest.TestCase):
    def test_threshold_is_configured(self):
        self.assertEqual(configured_threshold(), 85)

    def test_gate_fails_just_below_the_threshold(self):
        report = run_gate(calls_covering(5))
        self.assertEqual(report.returncode, 2, report.stdout.decode())
        self.assertIn("Coverage failure: total of 83 is less than fail-under=85", report.stdout.decode())

    def test_gate_passes_just_above_the_threshold(self):
        for false_calls in (6, 10):
            with self.subTest(false_calls=false_calls):
                report = run_gate(calls_covering(false_calls))
                self.assertEqual(report.returncode, 0, report.stdout.decode())

    def test_gates_run_the_report_without_weakening_it(self):
        for path in ("Makefile", ".github/workflows/ci.yml"):
            text = (ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("-m coverage report\n", text)
                for weakening in ("--fail-under", "|| true", "continue-on-error"):
                    self.assertNotIn(weakening, text)
