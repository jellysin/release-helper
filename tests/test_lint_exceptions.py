"""Lint exceptions for engine-written files are dated and must still be needed."""

import datetime
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Praetor writes these files; see pyproject.toml for the reason (cordanaLLM/praetor#1204).
EXPIRES = datetime.date(2027, 1, 9)
LINT_EXCEPTIONS = {}
FORMAT_EXCEPTIONS = [
    ".config/agent/hooks/block_evasion.py",
    ".config/lefthook/scripts/checkpoint.py",
    ".config/lefthook/scripts/common.py",
]


def ruff(*arguments):
    command = [sys.executable, "-m", "ruff", *arguments]
    return subprocess.run(command, cwd=ROOT, capture_output=True, check=False, timeout=60).returncode


class LintExceptionTests(unittest.TestCase):
    def test_configuration_declares_exactly_the_dated_exceptions(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            config = tomllib.load(stream)["tool"]["ruff"]
        self.assertEqual(config["lint"].get("per-file-ignores", {}), LINT_EXCEPTIONS)
        self.assertEqual(config["format"]["exclude"], FORMAT_EXCEPTIONS)

    def test_exceptions_have_not_expired(self):
        today = datetime.datetime.now(datetime.UTC).date()
        self.assertLessEqual(today, EXPIRES, "renew each exception with a reason, or remove it")

    def test_every_exception_is_still_needed(self):
        for path, rules in LINT_EXCEPTIONS.items():
            for rule in rules:
                with self.subTest(path=path, rule=rule):
                    override = ("--config", "lint.per-file-ignores = {}", "--select", rule)
                    self.assertEqual(ruff("check", "--no-cache", *override, path), 1, "finding is gone")
        for path in FORMAT_EXCEPTIONS:
            with self.subTest(path=path, rule="format"):
                self.assertEqual(ruff("format", "--no-cache", "--check", path), 1, "file is formatted")
