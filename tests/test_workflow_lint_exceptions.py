"""The workflow linter's one exception is dated, narrow and must still be needed."""

import datetime
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github" / "actionlint.yaml"
WORKFLOW = ".github/workflows/release-please.yml"
OTHER = ".github/workflows/other.yml"
# actionlint 1.7.12 does not know these documented values of the job context (rhysd/actionlint#707).
EXPIRES = datetime.date(2027, 1, 9)
PROPERTIES = ("workflow_repository", "workflow_sha")
UNDEFINED = re.compile(r'property "(\w+)" is not defined in object type \{(\w+): .+\}')
DECLARED = """\
# One exception, held by tests/test_workflow_lint_exceptions.py: the test fails after 2027-01-09
# and as soon as actionlint accepts the workflow without this entry.
# Reason: actionlint 1.7.12 does not know the documented job.workflow_repository and
# job.workflow_sha values (rhysd/actionlint#707).
paths:
  .github/workflows/release-please.yml:
    ignore:
      - '^property "workflow_(repository|sha)" is not defined in object type \\{check_run_id: number; container: .+\\}$'
"""
PLANTED = """\
on: workflow_call
jobs:
  planted:
    runs-on: ubuntu-24.04
    steps:
      - run: 'true'
        env:
          REPOSITORY: ${{ job.workflow_repository }}
          COMMIT: ${{ job.workflow_sha }}
          TYPO: ${{ job.workflow_shaa }}
          OTHER_OBJECT: ${{ runner.workflow_sha }}
"""
TYPO = ("workflow_shaa", "check_run_id")
OTHER_OBJECT = ("workflow_sha", "arch")


def undefined(directory, config, *workflows):
    """Return (file, property, first key of its object) for every finding actionlint reports."""
    program = shutil.which("actionlint")
    if program is None:
        raise AssertionError("actionlint is not on PATH; CONTRIBUTING.md lists it among the local tools")
    options = ["-config-file", str(config), "-shellcheck=", "-pyflakes=", "-format", "{{json .}}"]
    command = [program, *options, *workflows]
    result = subprocess.run(command, cwd=directory, capture_output=True, check=False, text=True, timeout=60)
    if result.returncode not in (0, 1):
        raise AssertionError(f"actionlint exited {result.returncode}: {result.stderr}")
    found = []
    for finding in json.loads(result.stdout):
        match = UNDEFINED.fullmatch(finding["message"])
        found.append((finding["filepath"], *(match.groups() if match else (finding["message"], ""))))
    return sorted(found)


class WorkflowLintExceptionTests(unittest.TestCase):
    def test_configuration_declares_exactly_the_dated_exception(self):
        self.assertEqual(CONFIG.read_text(encoding="utf-8"), DECLARED)

    def test_the_exception_has_not_expired(self):
        today = datetime.datetime.now(datetime.UTC).date()
        self.assertLessEqual(today, EXPIRES, "renew the exception with a reason, or remove it")

    def test_the_exception_is_still_needed(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory, "actionlint.yaml")
            empty.write_text("", encoding="utf-8")
            found = undefined(ROOT, empty, WORKFLOW)
        self.assertEqual(found, [(WORKFLOW, name, "check_run_id") for name in PROPERTIES], "finding is gone")

    def test_the_workflow_passes_with_the_exception(self):
        self.assertEqual(undefined(ROOT, CONFIG, WORKFLOW), [])

    def test_the_exception_covers_no_other_property_object_or_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = Path(directory, ".github", "workflows")
            workflows.mkdir(parents=True)
            for name in (WORKFLOW, OTHER):
                Path(directory, name).write_text(PLANTED, encoding="utf-8")
            found = undefined(directory, CONFIG, WORKFLOW, OTHER)
        everything = [*((name, "check_run_id") for name in PROPERTIES), TYPO, OTHER_OBJECT]
        expected = [(OTHER, *finding) for finding in everything] + [(WORKFLOW, *TYPO), (WORKFLOW, *OTHER_OBJECT)]
        self.assertEqual(found, sorted(expected))

    def test_a_missing_linter_fails_instead_of_skipping(self):
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(AssertionError, "actionlint is not on PATH"):
                undefined(ROOT, CONFIG, WORKFLOW)


if __name__ == "__main__":
    unittest.main()
