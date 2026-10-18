"""This repository keeps the workflow the command accepts as the fallback behind the merge train."""

import re
import unittest
from pathlib import Path

from jellysin_tooling import prepare

ROOT = Path(__file__).resolve().parents[1]


class OwnReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.contents = (ROOT / prepare.WORKFLOW).read_text(encoding="utf-8")

    def test_only_a_manual_dispatch_starts_it(self):
        triggers = self.contents.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual(re.findall(r"(?m)^  (\S+):", triggers), ["workflow_dispatch"])
        self.assertEqual(re.findall(r"(?m)^      (\S+):$", triggers), ["tag", "commit"])

    def test_one_job_holds_the_only_write_permission(self):
        self.assertEqual(
            re.findall(r"(?m)^( *)permissions:\n\1  (.+)$", self.contents),
            [("", "contents: read"), ("    ", "contents: write")],
        )
        self.assertEqual(len(re.findall(r"(?m)^\s+\S+: write$", self.contents)), 1)
        self.assertEqual(re.findall(r"(?m)^  (\S+):$", self.contents.split("\njobs:\n", 1)[1]), ["prepare"])

    def test_checkout_has_the_history_the_command_needs_and_keeps_no_credentials(self):
        checkout = self.contents.split("actions/checkout@", 1)[1].split("      - ", 1)[0]
        self.assertIn("          fetch-depth: 0\n", checkout)
        self.assertIn("          persist-credentials: false\n", checkout)

    def test_runs_the_command_of_this_checkout_with_the_built_in_token_and_skips_a_train_tag(self):
        step = self.contents.split("        uses: ./actions/prepare\n", 1)[1]
        self.assertEqual(
            step,
            "        with:\n"
            "          tag: ${{ inputs.tag }}\n"
            "          commit: ${{ inputs.commit }}\n"
            "          token: ${{ github.token }}\n"
            "          changelog-path: CHANGELOG.md\n"
            "          skip-tagged: 'true'\n",
        )
        self.assertNotIn("run:", self.contents)
        self.assertNotIn("secrets.", self.contents)
