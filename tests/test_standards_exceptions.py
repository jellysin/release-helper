"""Every dated exception in .standards.yaml names a file that exists.

praetorctl audit accepts an exception without checking its path, so an entry that names a
removed or misspelled file keeps excusing its rule for the whole repository.
"""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def exception_paths(text):
    """Return (rule, path) for each entry of the exceptions list that names a path.

    The YAML parser reads every header form (comments, flow lists); a present section that is
    not a list of mappings fails instead of reading as empty.
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise ValueError("standards file is not a mapping")
    entries = document.get("exceptions", [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise ValueError("exceptions is not a list of mappings")
    return [(entry["rule"], entry["path"]) for entry in entries if "path" in entry]


class StandardsExceptionTests(unittest.TestCase):
    """The exceptions reader and the paths the repository file names."""

    def test_reader_finds_paths_and_skips_entries_without_one(self):
        """Entries without a path, such as a tool exception, are skipped."""
        text = (
            "version: 1\nexceptions:\n"
            "  - rule: HISS-11\n    path: a.yml\n    reason: r\n    expires: x\n"
            "  - rule: declared-linters\n    tool: trivy\n    reason: r\n    expires: x\n"
            "  - rule: HISS-18\n    path: b.yml\n    reason: r\n    expires: x\n"
        )
        self.assertEqual(exception_paths(text), [("HISS-11", "a.yml"), ("HISS-18", "b.yml")])

    def test_reader_returns_nothing_without_an_exceptions_list(self):
        """A file without the section has no exceptions."""
        self.assertEqual(exception_paths("version: 1\nrelease:\n  e2e: {}\n"), [])

    def test_reader_reads_a_commented_header_and_a_flow_list(self):
        """Header comments and flow style read like the block form."""
        self.assertEqual(exception_paths("exceptions: # temporary\n  - rule: R\n    path: a.yml\n"), [("R", "a.yml")])
        self.assertEqual(exception_paths("exceptions: [{rule: R, path: b.yml}]\n"), [("R", "b.yml")])

    def test_reader_refuses_an_exceptions_section_it_cannot_read(self):
        """A section that is not a list of mappings fails instead of reading as empty."""
        for text in ("exceptions: none\n", "exceptions:\n  - just-a-string\n", "- a list\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                exception_paths(text)

    def test_every_exception_path_exists(self):
        """Each path an exception names is a file in the repository."""
        entries = exception_paths((ROOT / ".standards.yaml").read_text(encoding="utf-8"))
        for rule, path in entries:
            with self.subTest(rule=rule, path=path):
                self.assertTrue((ROOT / path).is_file(), f"{rule} exception names a missing file: {path}")
