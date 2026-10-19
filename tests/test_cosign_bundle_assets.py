"""Tests for cosign bundle asset verification, reconciliation, and publication."""

import base64
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jellysin_tooling import source
from jellysin_tooling.common import MAX_BUNDLE, MAX_FILES, ValidationError
from jellysin_tooling.github import command
from jellysin_tooling.release import publish, validate_bundle
from tests.fixtures import COMMIT, CREATED, FIRST, FakeGitHub, Workspace, make_bundle, write_bundles

BUILD_SIGNED_WORKFLOW = ".github/workflows/build-signed.yml"
RELEASE_WORKFLOW = ".github/workflows/release.yml"


class CosignBundlePublicationTests(Workspace):
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
        return repo_dir, commit, tag

    def setup_source_release(self, tag="v1.0.0"):
        repo_dir, commit, tag = self.make_git_repo(tag=tag)
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        files = {
            release["archive"]["name"]: (output / release["archive"]["name"]).read_bytes(),
            "release.json": (output / "release.json").read_bytes(),
            "sbom.spdx.json": (output / "sbom.spdx.json").read_bytes(),
            "checksums.txt": (output / "checksums.txt").read_bytes(),
        }
        return repo_dir, commit, tag, repo, output, release, files

    def make_runner(self, client, remote, output, writes):
        def runner(arguments):
            writes.append(arguments)
            if "POST" in arguments:
                name = arguments[4].split("name=")[1]
                asset_id = max([a["id"] for a in remote["assets"]] + [0]) + 1
                data = (output / name).read_bytes()
                repo = remote["url"].removeprefix("https://api.github.com/repos/").rsplit("/releases/", 1)[0]
                url = f"https://github.com/{repo}/releases/download/untagged-fixture-{remote['id']}/{name}"
                remote["assets"].append(
                    {
                        "id": asset_id,
                        "url": f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}",
                        "name": name,
                        "state": "uploaded",
                        "size": len(data),
                        "browser_download_url": url,
                    }
                )
                client.assets[repo, asset_id] = data
                client.downloads[url] = data
            elif "PATCH" in arguments:
                client.publish(remote)

        return runner

    def test_positive_build_signed_publish_with_four_valid_bundles_uploads_eight_assets(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        remote["assets"].clear()
        writes = []
        runner = self.make_runner(client, remote, output, writes)
        verifier = Mock()

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
        ):
            result = publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                verifier,
                runner,
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

        self.assertEqual(release, result)
        # 8 POST calls for the 8 assets (4 base + 4 bundles) + 1 PATCH call marking draft=false
        self.assertEqual(9, len(writes))
        post_methods = [w[3] for w in writes[:8]]
        self.assertEqual(["POST"] * 8, post_methods)
        self.assertEqual("PATCH", writes[8][3])
        uploaded_names = [w[4].split("name=")[1] for w in writes[:8]]
        expected_names = sorted(list(files.keys()) + [f"{name}.sigstore.json" for name in files])
        self.assertEqual(expected_names, sorted(uploaded_names))
        # Verifier is called only on the 4 base assets, not on the bundles
        self.assertEqual(4, verifier.call_count)
        verified_paths = sorted(call.args[0].name for call in verifier.call_args_list)
        self.assertEqual(sorted(files.keys()), verified_paths)
        self.assertFalse(remote["draft"])
        self.assertTrue(remote["immutable"])

    def test_positive_plugin_publish_with_build_signed_uploads_eight_assets(self):
        release, output = self.plugin()
        files = {
            release["archive"]["name"]: (output / release["archive"]["name"]).read_bytes(),
            "release.json": (output / "release.json").read_bytes(),
            "sbom.spdx.json": (output / "sbom.spdx.json").read_bytes(),
            "checksums.txt": (output / "checksums.txt").read_bytes(),
        }
        write_bundles(output, files)
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        remote["assets"].clear()
        writes = []
        runner = self.make_runner(client, remote, output, writes)
        verifier = Mock()

        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            result = publish(
                client,
                output,
                FIRST["repository"],
                release["tag"],
                RELEASE_WORKFLOW,
                verifier,
                runner,
                kind="plugin",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

        self.assertEqual(release, result)
        self.assertEqual(9, len(writes))
        self.assertEqual(4, verifier.call_count)

    def test_negative_missing_bundle_refused_naming_it(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        missing_target = "release.json.sigstore.json"
        (output / missing_target).unlink()
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Missing signature bundle: {missing_target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_digest_mismatch_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "checksums.txt.sigstore.json"
        wrong_digest = base64.b64encode(b"0" * 32).decode("ascii")
        (output / target).write_bytes(make_bundle(files["checksums.txt"], digest=wrong_digest))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Signature bundle digest mismatch: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_wrong_media_type_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "sbom.spdx.json.sigstore.json"
        (output / target).write_bytes(make_bundle(files["sbom.spdx.json"], media_type="application/json"))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Invalid signature bundle mediaType: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_oversized_bundle_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "release.json.sigstore.json"
        (output / target).write_bytes(make_bundle(files["release.json"], padding="x" * (MAX_BUNDLE + 100)))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Signature bundle exceeds size limit: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_symlink_bundle_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "checksums.txt.sigstore.json"
        real_file = output / "real_bundle.json"
        real_file.write_bytes((output / target).read_bytes())
        (output / target).unlink()
        (output / target).symlink_to(real_file)
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Symlink signature bundle refused: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_stray_bundle_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        stray = "stray.tar.gz.sigstore.json"
        (output / stray).write_bytes(make_bundle(b"stray content"))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Unexpected signature bundle: {stray}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_boundary_release_directory_file_count(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        cases = []
        for outcome in (None, "Release directory holds too many files"):
            client = FakeGitHub()
            remote = client.add({"repository": repo}, release, output, draft=True)
            remote["assets"].clear()
            cases.append((client, self.make_runner(client, remote, output, []), outcome))
        present = sum(1 for _ in output.iterdir())
        for index in range(MAX_FILES - present):
            (output / f"extra-{index}.txt").write_bytes(b"x")
        for client, runner, outcome in cases:
            if outcome:
                (output / "extra-over.txt").write_bytes(b"x")
            with (
                self.subTest(outcome=outcome),
                patch("jellysin_tooling.release.exact_tag", return_value=commit),
                patch("jellysin_tooling.release.remote_tag", return_value=commit),
                patch(
                    "jellysin_tooling.source.archive_bytes",
                    return_value=(output / release["archive"]["name"]).read_bytes(),
                ),
            ):
                arguments = (client, output, repo, tag, RELEASE_WORKFLOW, Mock(), runner)
                if outcome:
                    with self.assertRaisesRegex(ValidationError, outcome):
                        publish(*arguments, kind="source", signer_workflow=BUILD_SIGNED_WORKFLOW)
                else:
                    self.assertEqual(release, publish(*arguments, kind="source", signer_workflow=BUILD_SIGNED_WORKFLOW))

    def test_negative_non_json_bundle_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "release.json.sigstore.json"
        (output / target).write_bytes(b"not json at all")
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, "Invalid JSON"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_non_dict_json_bundle_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "release.json.sigstore.json"
        (output / target).write_bytes(b'["list", "instead", "of", "dict"]')
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Signature bundle must be a JSON object: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_negative_wrong_algorithm_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "release.json.sigstore.json"
        (output / target).write_bytes(make_bundle(files["release.json"], algorithm="SHA2_512"))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Invalid signature bundle algorithm: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_boundary_bundle_at_exact_size_bound_accepted(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "release.json.sigstore.json"
        base_raw = make_bundle(files["release.json"], padding="")
        exact_bytes = make_bundle(files["release.json"], padding="x" * (MAX_BUNDLE - len(base_raw)))
        self.assertEqual(MAX_BUNDLE, len(exact_bytes))
        (output / target).write_bytes(exact_bytes)

        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        remote["assets"].clear()
        writes = []
        runner = self.make_runner(client, remote, output, writes)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                runner,
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )
        self.assertEqual(9, len(writes))

    def test_boundary_bundle_plus_one_byte_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        target = "release.json.sigstore.json"
        base_raw = make_bundle(files["release.json"], padding="")
        plus_one_bytes = make_bundle(files["release.json"], padding="x" * (MAX_BUNDLE + 1 - len(base_raw)))
        self.assertEqual(MAX_BUNDLE + 1, len(plus_one_bytes))
        (output / target).write_bytes(plus_one_bytes)

        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, f"Signature bundle exceeds size limit: {target}"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

    def test_boundary_release_workflow_signer_path_unchanged_no_bundle_required(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        # No bundles written to output
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        remote["assets"].clear()
        writes = []
        runner = self.make_runner(client, remote, output, writes)
        verifier = Mock()

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
        ):
            result = publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                verifier,
                runner,
                kind="source",
                signer_workflow=RELEASE_WORKFLOW,
            )

        self.assertEqual(release, result)
        # 4 POST calls for the 4 base assets + 1 PATCH call
        self.assertEqual(5, len(writes))
        uploaded = [w[4].split("name=")[1] for w in writes if "POST" in w]
        self.assertEqual(sorted(files.keys()), sorted(uploaded))
        self.assertEqual(4, verifier.call_count)

    def test_resume_draft_holding_some_bundles_only_missing_uploaded(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        # In FakeGitHub.add, all 8 assets were added to remote["assets"].
        # Pop 2 bundles so they are missing:
        missing_bundles = ["checksums.txt.sigstore.json", "sbom.spdx.json.sigstore.json"]
        remote["assets"] = [a for a in remote["assets"] if a["name"] not in missing_bundles]
        writes = []
        runner = self.make_runner(client, remote, output, writes)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                runner,
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )

        # Only the 2 missing bundles should be uploaded via POST
        post_names = [w[4].split("name=")[1] for w in writes if "POST" in w]
        self.assertEqual(sorted(missing_bundles), sorted(post_names))
        self.assertIn("PATCH", [w[3] for w in writes])

    def test_resume_draft_differing_existing_bundle_bytes_refused(self):
        _, commit, tag, repo, output, release, files = self.setup_source_release()
        write_bundles(output, files)
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        # Modify the remote asset bytes for one bundle
        bundle_asset = next(a for a in remote["assets"] if a["name"] == "release.json.sigstore.json")
        client.assets[repo, bundle_asset["id"]] = b"different-bundle-bytes"

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / release["archive"]["name"]).read_bytes(),
            ),
            self.assertRaisesRegex(ValidationError, "Existing asset differs; use a new release version"),
        ):
            publish(
                client,
                output,
                repo,
                tag,
                RELEASE_WORKFLOW,
                Mock(),
                Mock(),
                kind="source",
                signer_workflow=BUILD_SIGNED_WORKFLOW,
            )


class PublishedBundleReplayTests(unittest.TestCase):
    """Replay the v1.4.2 checksums.txt and the cosign bundle that build-signed.yml made for it."""

    def test_published_bundle_matches_its_asset_and_no_other(self):
        data = Path(__file__).parent / "data" / "v1.4.2"
        asset = (data / "checksums.txt").read_bytes()
        bundle_path = data / "checksums.txt.sigstore.json"
        self.assertEqual(bundle_path.read_bytes(), validate_bundle(bundle_path, bundle_path.name, asset))
        with self.assertRaisesRegex(ValidationError, "Signature bundle digest mismatch"):
            validate_bundle(bundle_path, bundle_path.name, asset[:-1] + b"x")
