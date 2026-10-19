"""Tests for milestone version alignment, drift refusal, and release PR gating."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from jellysin_tooling import milestone
from jellysin_tooling.cli import parser, run
from jellysin_tooling.common import ValidationError
from tests.action_steps import ROOT, SKIP_REASON, ActionRunner, bash
from tests.fixtures import Workspace

REPO = "jellysin/release-helper"
# CI runs these tests on tag and release-please branches; the runner's ref must not pick the mode.
NEUTRAL_REF = patch.dict(os.environ, {"GITHUB_REF_TYPE": "", "GITHUB_HEAD_REF": "", "GITHUB_REF_NAME": ""})


def setUpModule():
    NEUTRAL_REF.start()


def tearDownModule():
    NEUTRAL_REF.stop()


class MilestoneFakeGitHub:
    def __init__(self, open_milestones=None, closed_milestones=None, pulls=None):
        self.open_milestones = list(open_milestones if open_milestones is not None else [])
        self.closed_milestones = list(closed_milestones if closed_milestones is not None else [])
        self.pulls = list(pulls if pulls is not None else [])
        self.calls = []

    def api(self, path):
        self.calls.append(path)
        if "milestones?state=open" in path:
            return self.open_milestones
        if "milestones?state=closed" in path:
            return self.closed_milestones
        if "pulls?" in path:
            return self.pulls
        raise ValidationError(f"Unexpected API call: {path}")


class MilestoneRuleTests(Workspace):
    def set_manifest(self, version):
        content = {".": version} if isinstance(version, str) else version
        (self.root / ".release-please-manifest.json").write_text(json.dumps(content), encoding="utf-8")

    # Positive tests
    def test_positive_normal_open_1_6_0_with_manifest_1_5_0_passes(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        milestone.check(client, self.root, REPO, branch="main")
        self.assertIn(f"repos/{REPO}/milestones?state=open&per_page=100", client.calls)

    def test_positive_normal_open_follow_ups_passes(self):
        self.set_manifest("0.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "0.6.0 — Open follow-ups", "state": "open"}])
        milestone.check(client, self.root, REPO, branch="main")

    def test_positive_normal_zero_version_milestones_passes(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[])
        out = io.StringIO()
        with redirect_stdout(out):
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("notice: zero open version milestones", out.getvalue())

    def test_positive_normal_ongoing_ignored(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(
            open_milestones=[
                {"title": "1.6.0", "state": "open"},
                {"title": "Ongoing — backlog triage", "state": "open"},
            ]
        )
        milestone.check(client, self.root, REPO, branch="main")

    def test_positive_release_mode_ongoing_ignored(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[{"title": "Ongoing — triage", "state": "open"}],
            closed_milestones=[{"title": "1.6.0", "state": "closed"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        milestone.check(client, self.root, REPO, branch=branch)

    # Negative tests
    def test_negative_open_1_3_0_with_manifest_1_5_0_refused(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.3.0", "state": "open"}])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        message = str(ctx.exception)
        self.assertIn("Milestone '1.3.0' drifts from manifest 1.5.0: the next milestone is 1.6.0", message)
        self.assertIn("'1.3.0'", message)
        self.assertIn("1.5.0", message)

    def test_negative_two_open_version_milestones_refused(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(
            open_milestones=[
                {"title": "1.6.0", "state": "open"},
                {"title": "1.7.0", "state": "open"},
            ]
        )
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("More than one open version milestone", str(ctx.exception))

    def test_negative_jump_1_7_0_refused(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.7.0", "state": "open"}])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("Milestone '1.7.0' drifts from manifest 1.5.0: the next milestone is 1.6.0", str(ctx.exception))

    def test_negative_manifest_rc_refused(self):
        self.set_manifest("1.5.0-rc1")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("Expected stable x.y.z version", str(ctx.exception))

    def test_negative_release_mode_open_v_refused_waits_message(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[{"title": "1.6.0", "state": "open"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch=branch)
        expected = (
            "Release 1.6.0 waits: close milestone 1.6.0 or add label release-ready, "
            f"then re-run CI (gh workflow run ci.yml --ref {branch})"
        )
        self.assertEqual(str(ctx.exception), expected)

    def test_negative_release_mode_patch_without_label_refused(self):
        self.set_manifest("1.5.1")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[],
            closed_milestones=[{"title": "1.5.1", "state": "closed"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch=branch)
        expected = (
            "Release 1.5.1 waits: close milestone 1.5.1 or add label release-ready, "
            f"then re-run CI (gh workflow run ci.yml --ref {branch})"
        )
        self.assertEqual(str(ctx.exception), expected)

    def test_negative_full_page_of_100_refused(self):
        self.set_manifest("1.5.0")
        full_page = [{"title": f"1.6.{i}", "state": "open"} for i in range(100)]
        client = MilestoneFakeGitHub(open_milestones=full_page)
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("unbounded", str(ctx.exception))

        branch = "release-please--branches--main"
        self.set_manifest("1.6.0")
        closed_full = [{"title": f"1.6.{i}", "state": "closed"} for i in range(100)]
        client_closed = MilestoneFakeGitHub(
            open_milestones=[],
            closed_milestones=closed_full,
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client_closed, self.root, REPO, branch=branch)
        self.assertIn("unbounded", str(ctx.exception))

    def test_negative_wrong_field_types_refused(self):
        self.set_manifest("1.5.0")
        cases = [
            ("milestone item non-dict", ["not-a-dict"]),
            ("milestone title non-str", [{"title": 123, "state": "open"}]),
            ("milestone state non-str", [{"title": "1.6.0", "state": 123}]),
        ]
        for name, open_data in cases:
            with self.subTest(case=name):
                client = MilestoneFakeGitHub(open_milestones=open_data)
                with self.assertRaises(ValidationError):
                    milestone.check(client, self.root, REPO, branch="main")

        branch = "release-please--branches--main"
        self.set_manifest("1.6.0")
        pr_cases = [
            ("PR non-dict", ["string-pr"]),
            ("PR base non-dict", [{"base": "main", "labels": []}]),
            ("PR base ref non-str", [{"base": {"ref": 123}, "labels": []}]),
            ("PR labels non-list", [{"base": {"ref": "main"}, "labels": "none"}]),
            ("PR label item non-dict", [{"base": {"ref": "main"}, "labels": ["none"]}]),
            ("PR label name non-str", [{"base": {"ref": "main"}, "labels": [{"name": 123}]}]),
        ]
        for name, pulls_data in pr_cases:
            with self.subTest(case=name):
                client = MilestoneFakeGitHub(open_milestones=[], pulls=pulls_data)
                with self.assertRaises(ValidationError):
                    milestone.check(client, self.root, REPO, branch=branch)

    def test_negative_pr_not_found_or_two_prs_refused(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        valid_pr = {"state": "open", "base": {"ref": "main"}, "labels": []}

        # PR not found (0 returned)
        client_zero = MilestoneFakeGitHub(open_milestones=[], closed_milestones=[], pulls=[])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client_zero, self.root, REPO, branch=branch)
        self.assertIn("Expected exactly one open release pull request", str(ctx.exception))

        # Two PRs returned
        client_two = MilestoneFakeGitHub(open_milestones=[], closed_milestones=[], pulls=[valid_pr, valid_pr])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client_two, self.root, REPO, branch=branch)
        self.assertIn("Expected exactly one open release pull request", str(ctx.exception))

        # PR with base not main
        bad_base_pr = {"state": "open", "base": {"ref": "feature"}, "labels": []}
        client_bad_base = MilestoneFakeGitHub(open_milestones=[], closed_milestones=[], pulls=[bad_base_pr])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client_bad_base, self.root, REPO, branch=branch)
        self.assertIn("Release pull request base must be main", str(ctx.exception))

    def test_negative_release_mode_drift_refused(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.3.0", "state": "open"}])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch=branch)
        self.assertIn("Milestone '1.3.0' drifts from manifest 1.6.0: the next milestone is 1.7.0", str(ctx.exception))

    def test_negative_release_mode_closed_milestone_mismatch_refused(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[],
            closed_milestones=[{"title": "1.5.0", "state": "closed"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch=branch)
        expected = (
            "Release 1.6.0 waits: close milestone 1.6.0 or add label release-ready, "
            f"then re-run CI (gh workflow run ci.yml --ref {branch})"
        )
        self.assertEqual(str(ctx.exception), expected)

    def test_negative_release_mode_duplicate_open_milestones_refused(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[
                {"title": "1.6.0", "state": "open"},
                {"title": "v1.6.0", "state": "open"},
            ],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch=branch)
        self.assertIn("More than one open version milestone", str(ctx.exception))

    def test_negative_manifest_missing_or_malformed(self):
        # Missing file
        client = MilestoneFakeGitHub(open_milestones=[])
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("Missing .release-please-manifest.json", str(ctx.exception))

        # Missing key "."
        self.set_manifest({"other": "1.5.0"})
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("missing root version", str(ctx.exception))

        # Non-string version
        self.set_manifest({".": 150})
        with self.assertRaises(ValidationError) as ctx:
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("Expected stable x.y.z version", str(ctx.exception))

    # Boundary tests
    def test_boundary_release_mode_v_closed_none_open_passes(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[],
            closed_milestones=[{"title": "1.6.0", "state": "closed"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        milestone.check(client, self.root, REPO, branch=branch)

    def test_boundary_label_release_ready_passes_even_with_milestone_v_open(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[{"title": "1.6.0", "state": "open"}],
            closed_milestones=[],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": [{"name": "release-ready"}]}],
        )
        milestone.check(client, self.root, REPO, branch=branch)

    def test_boundary_release_mode_open_next_v_only_passes(self):
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client = MilestoneFakeGitHub(
            open_milestones=[{"title": "1.7.0", "state": "open"}],
            closed_milestones=[{"title": "1.6.0", "state": "closed"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        milestone.check(client, self.root, REPO, branch=branch)

    def test_boundary_v_prefixed_title_accepted(self):
        # Normal mode accepts v1.6.0
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "v1.6.0", "state": "open"}])
        milestone.check(client, self.root, REPO, branch="main")

        # Release mode accepts v1.6.0 as closed milestone
        self.set_manifest("1.6.0")
        branch = "release-please--branches--main"
        client_rel = MilestoneFakeGitHub(
            open_milestones=[],
            closed_milestones=[{"title": "v1.6.0", "state": "closed"}],
            pulls=[{"state": "open", "base": {"ref": "main"}, "labels": []}],
        )
        milestone.check(client_rel, self.root, REPO, branch=branch)

    def test_boundary_title_1_6_0x_not_a_version_milestone(self):
        self.assertIsNone(milestone.parse_version_milestone("1.6.0x"))
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0x", "state": "open"}])
        out = io.StringIO()
        with redirect_stdout(out):
            milestone.check(client, self.root, REPO, branch="main")
        self.assertIn("notice: zero open version milestones", out.getvalue())

    def test_mode_detection_from_environment(self):
        self.set_manifest("1.5.0")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        with patch.dict(os.environ, {"GITHUB_HEAD_REF": "release-please--branches--main", "GITHUB_REF_NAME": "main"}):
            with self.assertRaises(ValidationError):
                milestone.check(client, self.root, REPO)

        with patch.dict(os.environ, {"GITHUB_HEAD_REF": "", "GITHUB_REF_NAME": "release-please--branches--main"}):
            with self.assertRaises(ValidationError):
                milestone.check(client, self.root, REPO)


class MilestoneCliTests(Workspace):
    def test_cli_milestone_check_runs_and_passes(self):
        (self.root / ".release-please-manifest.json").write_text(json.dumps({".": "1.5.0"}), encoding="utf-8")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        args = parser().parse_args(["milestone-check", "--directory", str(self.root), "--repo", REPO])
        with patch("jellysin_tooling.cli.GitHub", return_value=client):
            run(args)
        self.assertEqual(len(client.calls), 1)

    def test_cli_milestone_check_reads_github_repository_from_env(self):
        (self.root / ".release-please-manifest.json").write_text(json.dumps({".": "1.5.0"}), encoding="utf-8")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        args = parser().parse_args(["milestone-check", "--directory", str(self.root)])
        with (
            patch("jellysin_tooling.cli.GitHub", return_value=client),
            patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO}),
        ):
            run(args)
        self.assertEqual(len(client.calls), 1)

    def test_cli_milestone_check_refuses_invalid_repository(self):
        (self.root / ".release-please-manifest.json").write_text(json.dumps({".": "1.5.0"}), encoding="utf-8")
        args = parser().parse_args(["milestone-check", "--directory", str(self.root), "--repo", "invalid_repo"])
        with self.assertRaises(ValidationError) as ctx:
            run(args)
        self.assertIn("Invalid repository", str(ctx.exception))

    def test_cli_milestone_check_refuses_missing_repository(self):
        (self.root / ".release-please-manifest.json").write_text(json.dumps({".": "1.5.0"}), encoding="utf-8")
        args = parser().parse_args(["milestone-check", "--directory", str(self.root), "--repo", ""])
        with (
            patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}, clear=True),
            self.assertRaises(ValidationError) as ctx,
        ):
            run(args)
        self.assertIn("Missing repository", str(ctx.exception))


@unittest.skipIf(bash() is None, SKIP_REASON)
class MilestoneActionTests(unittest.TestCase):
    def setUp(self):
        self.runner = ActionRunner(record=True)
        self.addCleanup(self.runner.close)

    def test_action_declares_permissions_and_inputs(self):
        action_text = (ROOT / "actions" / "milestone" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("issues: read", action_text)
        self.assertIn("pull-requests: read", action_text)
        self.assertIn("directory:", action_text)
        self.assertIn("token:", action_text)

    def test_action_runs_tools_with_passed_inputs(self):
        result = self.runner.run("milestone", {"directory": "custom-dir", "token": "custom-token"})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = self.runner.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], [f"{ROOT}/tools.py", "milestone-check", "--directory", "custom-dir"])
        self.assertEqual(calls[0]["GH_TOKEN"], "custom-token")

    def test_action_runs_tools_with_defaults(self):
        result = self.runner.run("milestone", {})
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        calls = self.runner.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], [f"{ROOT}/tools.py", "milestone-check", "--directory", "."])
        self.assertEqual(calls[0]["GH_TOKEN"], "${{ github.token }}")


class MilestoneTagRunTests(Workspace):
    def test_tag_run_skips_without_api_calls(self):
        (self.root / ".release-please-manifest.json").write_text(json.dumps({".": "1.6.0"}), encoding="utf-8")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        out = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REF_TYPE": "tag"}), redirect_stdout(out):
            milestone.check(client, self.root, REPO, branch="v1.6.0")
        self.assertEqual(client.calls, [])
        self.assertIn("tag run, milestone check skipped", out.getvalue())

    def test_branch_run_with_same_state_refuses(self):
        (self.root / ".release-please-manifest.json").write_text(json.dumps({".": "1.6.0"}), encoding="utf-8")
        client = MilestoneFakeGitHub(open_milestones=[{"title": "1.6.0", "state": "open"}])
        with patch.dict(os.environ, {"GITHUB_REF_TYPE": "branch"}), self.assertRaises(ValidationError):
            milestone.check(client, self.root, REPO, branch="main")
