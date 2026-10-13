"""Run the check action's step as a consumer's workflow runs it: catalog validation on, off, or refused."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from jellysin_tooling.common import json_bytes
from tests.fixtures import approved

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "actions" / "check" / "action.yml"
DOCUMENTS = ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md")
# A manifest that parses as JSON, so the policy check reads it, and that catalog validation refuses.
REFUSED = "[1]\n"


def step():
    """Return the action's shell step exactly as the action file carries it."""
    return dedent(ACTION.read_text(encoding="utf-8").split("      run: |\n", 1)[1])


@unittest.skipIf(os.name == "nt", "the action's step is a bash script")
class CheckActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jellysin-check-action-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.repository = root / "repository"
        self.repository.mkdir()
        for name in DOCUMENTS:
            (self.repository / name).write_text("fixture\n", encoding="utf-8")
        (self.repository / "renovate.json").write_text("{}\n", encoding="utf-8")
        (self.repository / "plugins.json").write_bytes(json_bytes({"schemaVersion": 1, "plugins": [approved()]}))
        # The step calls `python`, which setup-python provides in a workflow.
        shims = root / "bin"
        shims.mkdir()
        python = shims / "python"
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        self.path = f"{shims}{os.pathsep}{os.environ['PATH']}"

    def run_step(self, catalog, manifest):
        (self.repository / "manifest.json").write_text(manifest, encoding="utf-8")
        environment = {
            **os.environ,
            "PATH": self.path,
            "TOOL_ROOT": str(ROOT),
            "REPOSITORY_DIRECTORY": str(self.repository),
            "CHECK_CATALOG": catalog,
        }
        # The shell GitHub runs for `shell: bash` in a composite action.
        command = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step()]
        return subprocess.run(command, env=environment, capture_output=True, check=False, timeout=60)

    def test_true_validates_the_catalog(self):
        self.assertEqual(self.run_step("true", "[]\n").returncode, 0)
        refused = self.run_step("true", REFUSED)
        self.assertEqual(refused.returncode, 1)
        self.assertRegex(refused.stderr.decode(), r"\Ajellysin-release-helper: \S")

    def test_false_leaves_the_catalog_unchecked(self):
        self.assertEqual(self.run_step("false", REFUSED).returncode, 0)

    def test_any_other_value_fails_instead_of_skipping_the_catalog(self):
        for catalog in ("True", "yes", "1", "", "true "):
            with self.subTest(catalog=catalog):
                result = self.run_step(catalog, REFUSED)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.decode(), "Invalid catalog input\n")
