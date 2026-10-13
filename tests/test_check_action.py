"""Run the check action's step as a consumer's workflow runs it: catalog validation on, off, or refused."""

import unittest

from jellysin_tooling.common import json_bytes
from tests.action_steps import SKIP_REASON, ActionRunner, bash
from tests.fixtures import approved

DOCUMENTS = ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md")
# A manifest that parses as JSON, so the policy check reads it, and that catalog validation refuses.
REFUSED = "[1]\n"


@unittest.skipIf(bash() is None, SKIP_REASON)
class CheckActionTests(unittest.TestCase):
    def setUp(self):
        self.runner = ActionRunner()
        self.addCleanup(self.runner.close)
        self.repository = self.runner.work / "repository"
        self.repository.mkdir()
        for name in DOCUMENTS:
            (self.repository / name).write_text("fixture\n", encoding="utf-8")
        (self.repository / "renovate.json").write_text("{}\n", encoding="utf-8")
        (self.repository / "plugins.json").write_bytes(json_bytes({"schemaVersion": 1, "plugins": [approved()]}))

    def run_step(self, catalog, manifest):
        (self.repository / "manifest.json").write_text(manifest, encoding="utf-8")
        return self.runner.run("check", {"directory": "repository", "catalog": catalog})

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
