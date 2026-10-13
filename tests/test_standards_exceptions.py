"""Every dated exception in .standards.yaml names a file that exists.

praetorctl audit accepts an exception without checking its path, so an entry that names a
removed or misspelled file keeps excusing its rule for the whole repository.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = re.compile(r"^  - rule: (?P<rule>\S+)\n(?P<body>(?:    .*\n)*)", re.MULTILINE)
PATH = re.compile(r"^    path: (?P<path>\S+)$", re.MULTILINE)


def exception_paths(text):
    """Return (rule, path) for each entry of the exceptions list that names a path."""
    section = text.split("\nexceptions:\n", 1)[1]
    found = []
    for entry in ENTRY.finditer(section):
        for match in PATH.finditer(entry["body"]):
            found.append((entry["rule"], match["path"]))
    return found


class StandardsExceptionTests(unittest.TestCase):
    def test_reader_finds_paths_and_skips_entries_without_one(self):
        text = (
            "version: 1\nexceptions:\n"
            "  - rule: HISS-11\n    path: a.yml\n    reason: r\n    expires: x\n"
            "  - rule: declared-linters\n    tool: trivy\n    reason: r\n    expires: x\n"
            "  - rule: HISS-18\n    path: b.yml\n    reason: r\n    expires: x\n"
        )
        self.assertEqual(exception_paths(text), [("HISS-11", "a.yml"), ("HISS-18", "b.yml")])

    def test_every_exception_path_exists(self):
        entries = exception_paths((ROOT / ".standards.yaml").read_text(encoding="utf-8"))
        self.assertTrue(entries, "the HISS-11 exception names a path")
        for rule, path in entries:
            with self.subTest(rule=rule, path=path):
                self.assertTrue((ROOT / path).is_file(), f"{rule} exception names a missing file: {path}")
