"""The exact interpreter is pinned once; every setup step reads that file."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PythonPinTests(unittest.TestCase):
    def test_pin_file_holds_one_exact_version(self):
        self.assertRegex((ROOT / ".python-version").read_text(encoding="utf-8"), r"\A\d+\.\d+\.\d+\n\Z")

    def test_every_setup_step_reads_the_pin_file(self):
        paths = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted((ROOT / "actions").rglob("*.yml"))
        setups = 0
        for path in paths:
            contents = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(re.search(r"(?m)^\s*python-version\s*:", contents), "literal interpreter pin")
                uses = len(re.findall(r"(?m)^\s*-\s*uses:\s*actions/setup-python@", contents))
                files = re.findall(r"(?m)^\s*python-version-file:\s*(.+?)\s*$", contents)
                self.assertEqual(len(files), uses)
                for target in files:
                    self.assertTrue(target.endswith(".python-version"), target)
                setups += uses
        # A renamed action or moved folder must not turn this into a vacuous pass.
        self.assertEqual(setups, 6)

    def test_documents_do_not_repeat_the_version(self):
        version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
        for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md"):
            with self.subTest(name=name):
                self.assertFalse(version in (ROOT / name).read_text(encoding="utf-8"), "repeats the pin")
