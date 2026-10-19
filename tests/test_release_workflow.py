"""Tests for release workflows and publication gate invariants."""

import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from jellysin_tooling import source
from jellysin_tooling.common import ValidationError
from jellysin_tooling.github import command
from jellysin_tooling.release import publish
from tests.fixtures import CREATED, FakeGitHub, Workspace
from tests.test_tool_name import message

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
BUILD_SIGNED_WORKFLOW = ROOT / ".github" / "workflows" / "build-signed.yml"


class ReleaseWorkflowShapeTests(unittest.TestCase):
    def setUp(self):
        self.release_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.build_text = BUILD_SIGNED_WORKFLOW.read_text(encoding="utf-8")
        self.release_data = yaml.safe_load(self.release_text)
        self.build_data = yaml.safe_load(self.build_text)

    def test_release_workflow_has_only_workflow_dispatch_trigger(self):
        triggers = self.release_data.get("on") or self.release_data.get(True)
        self.assertEqual(list(triggers.keys()), ["workflow_dispatch"])

    def test_publish_job_needs_build(self):
        publish_job = self.release_data["jobs"]["publish"]
        needs = publish_job["needs"]
        self.assertTrue(needs == "build" or "build" in needs)

    def test_publish_is_last_step_and_uses_source_kind(self):
        publish_job = self.release_data["jobs"]["publish"]
        last_step = publish_job["steps"][-1]
        self.assertEqual(last_step.get("uses"), "./actions/publish")
        step_with = last_step.get("with", {})
        self.assertEqual(step_with.get("kind"), "source")
        self.assertIn("build-signed.yml", step_with.get("signer-workflow", ""))
        all_uses = re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*(\S+)", self.release_text)
        self.assertEqual(all_uses[-1], "./actions/publish")

    def test_publish_copies_bundles_after_verify_step(self):
        publish_job = self.release_data["jobs"]["publish"]
        steps = publish_job["steps"]
        step_runs = []
        for i, step in enumerate(steps):
            if "run" in step:
                step_runs.append((i, step["run"]))

        verify_indices = [i for i, r in step_runs if "cosign verify-blob" in r]
        self.assertEqual(len(verify_indices), 1, "verify step must exist in publish job")

        copy_indices = [i for i, r in step_runs if "signatures/*.sigstore.json" in r and "dist" in r]
        self.assertEqual(len(copy_indices), 1, "copy bundles step must exist in publish job")

        self.assertLess(verify_indices[0], copy_indices[0])
        self.assertLess(copy_indices[0], len(steps) - 1)

    def test_build_signed_step_order_and_artifact_upload(self):
        steps = self.build_data["jobs"]["build"]["steps"]
        step_runs = []
        for i, step in enumerate(steps):
            if "run" in step:
                step_runs.append((i, step["run"]))

        dist_indices = [i for i, r in step_runs if "make dist" in r]
        self.assertEqual(len(dist_indices), 1, "make dist step must exist")
        dist_idx = dist_indices[0]

        attest_indices = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/attest@")]
        self.assertEqual(len(attest_indices), 1, "actions/attest step must exist")
        self.assertLess(dist_idx, attest_indices[0])

        sbom_indices = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/attest-sbom@")]
        self.assertEqual(len(sbom_indices), 1, "actions/attest-sbom step must exist")
        self.assertLess(dist_idx, sbom_indices[0])

        cosign_indices = [i for i, r in step_runs if "cosign sign-blob" in r]
        self.assertEqual(len(cosign_indices), 1, "cosign sign-blob step must exist")
        self.assertLess(dist_idx, cosign_indices[0])

        upload_steps = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(upload_steps), 1, "upload-artifact step must exist")
        self.assertEqual(upload_steps[0].get("with", {}).get("if-no-files-found"), "error")

    def test_no_continue_on_error_in_workflows(self):
        for path, text in ((RELEASE_WORKFLOW, self.release_text), (BUILD_SIGNED_WORKFLOW, self.build_text)):
            with self.subTest(workflow=path.name):
                self.assertNotIn("continue-on-error", text)


class ReleasePublicationTests(Workspace):
    def make_git_repo(self, tag="v1.0.0"):
        repo_dir = self.root / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        command(["git", "init"], cwd=str(repo_dir))
        command(["git", "config", "user.name", "Test User"], cwd=str(repo_dir))
        command(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir))
        (repo_dir / "README.md").write_text("Test", encoding="utf-8")
        command(["git", "add", "."], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir))
        commit = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))
        command(["git", "tag", "-a", tag, "-m", tag], cwd=str(repo_dir))
        return repo_dir, commit

    def test_release_without_assets_stays_draft_and_run_fails(self):
        repo, tag = "jellysin/source-repo", "v1.0.0"
        dist = self.root / "dist"
        repo_dir, commit = self.make_git_repo(tag)
        release = source.build(repo, tag, commit, CREATED, dist, cwd=str(repo_dir))
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, dist, draft=True)
        remote["assets"].clear()
        (dist / release["archive"]["name"]).unlink()
        runner = Mock()

        # Direct publish invocation fails and never calls runner (never PATCHes draft=false)
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"),
        ):
            publish(client, dist, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()
        self.assertTrue(remote["draft"])
        self.assertFalse(remote["immutable"])

        # CLI invocation also exits nonzero and never marks the draft published
        cli_args = [
            "publish",
            "--directory",
            str(dist),
            "--repo",
            repo,
            "--tag",
            tag,
            "--kind",
            "source",
            "--signer-workflow",
            ".github/workflows/build-signed.yml",
        ]
        with (
            patch("jellysin_tooling.cli.GitHub", return_value=client),
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch("jellysin_tooling.release.command"),
        ):
            status, stderr = message(cli_args)
        self.assertEqual(status, 1)
        self.assertIn("Missing or unsafe release artifact", stderr)
        self.assertTrue(remote["draft"])
        self.assertFalse(remote["immutable"])
