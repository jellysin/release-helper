"""Run the workflow's dispatch program without contacting GitHub."""

import json
import os
import subprocess
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock, call, patch


class ReleasePrWorkflowTests(unittest.TestCase):
    def setUp(self):
        workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-please.yml"
        contents = workflow.read_text(encoding="utf-8")
        source = contents.split("          python3 - <<'PY'\n", 1)[1].split("          PY", 1)[0]
        self.program = compile(dedent(source), str(workflow), "exec")
        self.dispatch = Mock()

    def execute(self, prs):
        environment = {
            "RELEASE_PRS": json.dumps(prs),
            "GITHUB_REPOSITORY": "jellysin/plugin-lastfm",
            # A stale caller environment must not enable the removed publication path.
            "RELEASE_TAG": "v1.0.0",
            "PUBLISH_RELEASE": "true",
            "PUBLICATION_WORKFLOW": "release.yml",
        }
        with patch.dict(os.environ, environment, clear=True), patch("subprocess.run", self.dispatch):
            exec(self.program, {})

    def test_no_pull_requests_never_dispatches_publication(self):
        self.execute([])
        self.dispatch.assert_not_called()

    def test_dispatches_only_ci_for_returned_release_pr_branches(self):
        branches = ["release-please--branches--main", "release-please--branches--main--components--second"]
        self.execute([{"headBranchName": branch, "baseBranchName": "main"} for branch in branches])
        self.assertEqual(
            self.dispatch.call_args_list,
            [
                call(
                    ["gh", "workflow", "run", "ci.yml", "--repo", "jellysin/plugin-lastfm", "--ref", branch],
                    check=True,
                    timeout=60,
                )
                for branch in branches
            ],
        )

    def test_accepts_at_most_twenty_release_prs(self):
        pr = {"headBranchName": "release-please--branches--main", "baseBranchName": "main"}
        self.execute([pr] * 20)
        self.assertEqual(self.dispatch.call_count, 20)
        self.dispatch.reset_mock()
        with self.assertRaisesRegex(SystemExit, "Unexpected release PR output"):
            self.execute([pr] * 21)
        self.dispatch.assert_not_called()

    def test_rejects_invalid_output_and_unexpected_branches(self):
        cases = [
            {},
            [{"headBranchName": "main", "baseBranchName": "main"}],
            [{"headBranchName": "release-please--branches--main", "baseBranchName": "other"}],
        ]
        for prs in cases:
            with self.subTest(prs=prs), self.assertRaisesRegex(SystemExit, "Unexpected release PR"):
                self.execute(prs)
            self.dispatch.assert_not_called()

    def test_dispatch_failure_stops_the_remaining_batch(self):
        pr = {"headBranchName": "release-please--branches--main", "baseBranchName": "main"}
        for failure in (subprocess.CalledProcessError(1, "gh"), subprocess.TimeoutExpired("gh", 60)):
            with self.subTest(failure=type(failure).__name__):
                self.dispatch.reset_mock()
                self.dispatch.side_effect = failure
                with self.assertRaises(type(failure)):
                    self.execute([pr, pr])
                self.dispatch.assert_called_once()
