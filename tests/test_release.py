"""Interrupted upload recovery and exact-tag publication protections."""

import copy
import os
from pathlib import Path
from unittest.mock import Mock, patch

from jellysin_tooling.catalog import collect
from jellysin_tooling.common import MAX_BUNDLE, ValidationError
from jellysin_tooling.release import (
    draft_starters,
    exact_tag,
    local_artifacts,
    publish,
    reconcile_assets,
    remote_tag,
    remove_draft_starters,
    validate_bundles,
)
from tests.fixtures import COMMIT, FIRST, SECOND, FakeGitHub, Workspace, approved, make_bundle, write_bundles


class BundleRetryTests(Workspace):
    def retry_fixture(self):
        """Build deterministic artifacts with one differently signed uploaded checksum bundle."""
        release, output = self.plugin()
        write_bundles(output)
        files = local_artifacts(output, release)
        files.update(validate_bundles(output, files))
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        bundle = next(asset for asset in remote["assets"] if asset["name"] == "checksums.txt.sigstore.json")
        return release, output, client, remote, files, bundle

    def replace_bundle(self, client, bundle, data):
        """Replace synthetic remote bundle bytes and its size without a network call."""
        bundle["size"] = len(data)
        client.assets[FIRST["repository"], bundle["id"]] = data

    def test_verified_bundle_is_kept_when_its_artifact_is_not_yet_uploaded(self):
        """Verify against the local artifact even when its upload was interrupted before starting."""
        release, _, client, remote, files, bundle = self.retry_fixture()
        uploaded = make_bundle(files["checksums.txt"], padding="earlier run")
        self.replace_bundle(client, bundle, uploaded)
        remote["assets"] = [asset for asset in remote["assets"] if asset["name"] != "checksums.txt"]
        runner = Mock()
        self.assertEqual(
            ["checksums.txt"],
            reconcile_assets(client, remote, files, FIRST["repository"], release["tag"], runner=runner),
        )
        self.assertEqual(uploaded, files[bundle["name"]])
        runner.assert_called_once()
        self.assertIn(
            f"https://github.com/{FIRST['repository']}/.github/workflows/release.yml@refs/tags/{release['tag']}",
            runner.call_args.args[0],
        )

    def test_cosign_failure_or_missing_command_refuses_the_uploaded_bundle(self):
        """A missing cosign executable or failed cryptographic verification must preserve refusal."""
        for error in (ValidationError("cosign command failed (1)"), FileNotFoundError("cosign")):
            with self.subTest(error=error):
                release, _, client, remote, files, bundle = self.retry_fixture()
                original = files[bundle["name"]]
                self.replace_bundle(client, bundle, make_bundle(files["checksums.txt"], padding="earlier run"))
                runner = Mock(side_effect=error)
                with self.assertRaisesRegex(ValidationError, "cosign verification unavailable or failed") as caught:
                    reconcile_assets(client, remote, files, FIRST["repository"], release["tag"], runner=runner)
                self.assertIs(error, caught.exception.__cause__)
                self.assertEqual(original, files[bundle["name"]])
                runner.assert_called_once()

    def test_bad_remote_bundle_digest_structure_or_size_is_refused_before_cosign(self):
        """Shared bundle validation rejects wrong digests, malformed structures and oversized downloads."""
        release, _, client, remote, files, bundle = self.retry_fixture()
        invalid = (
            (make_bundle(b"different artifact"), "digest mismatch"),
            (make_bundle(files["checksums.txt"], media_type="wrong"), "mediaType"),
            (make_bundle(files["checksums.txt"], algorithm="SHA2_512"), "algorithm"),
            (b"[]", "JSON object"),
            (b"{", "JSON"),
            (b"x" * (MAX_BUNDLE + 1), "size limit"),
        )
        for data, message in invalid:
            with self.subTest(message=message):
                self.replace_bundle(client, bundle, data)
                runner = Mock()
                with self.assertRaisesRegex(ValidationError, message):
                    reconcile_assets(client, remote, files, FIRST["repository"], release["tag"], runner=runner)
                runner.assert_not_called()

    def test_differing_signed_asset_is_refused_before_bundle_verification(self):
        """A valid bundle cannot authorize replacing an already uploaded artifact with different bytes."""
        release, _, client, remote, files, bundle = self.retry_fixture()
        self.replace_bundle(client, bundle, make_bundle(files["checksums.txt"], padding="earlier run"))
        artifact = next(asset for asset in remote["assets"] if asset["name"] == "checksums.txt")
        client.assets[FIRST["repository"], artifact["id"]] = b"x" * artifact["size"]
        runner = Mock()
        with self.assertRaisesRegex(ValidationError, "Existing asset differs"):
            reconcile_assets(client, remote, files, FIRST["repository"], release["tag"], runner=runner)
        runner.assert_not_called()

    def test_same_size_differing_bundle_is_verified_and_nonbundle_bytes_are_still_refused(self):
        """Handle nondeterministic bundles of equal length without relaxing artifact byte equality."""
        release, _, client, remote, files, bundle = self.retry_fixture()
        # JSON permits both whitespace bytes, giving a structurally valid equal-length bundle.
        uploaded = files[bundle["name"]].replace(b"\n", b" ")
        self.assertEqual(len(files[bundle["name"]]), len(uploaded))
        self.assertNotEqual(files[bundle["name"]], uploaded)
        self.replace_bundle(client, bundle, uploaded)
        runner = Mock()
        self.assertEqual(
            [], reconcile_assets(client, remote, files, FIRST["repository"], release["tag"], runner=runner)
        )
        runner.assert_called_once()
        artifact = next(asset for asset in remote["assets"] if asset["name"] == "release.json")
        client.assets[FIRST["repository"], artifact["id"]] = b"x" * artifact["size"]
        runner.reset_mock()
        with self.assertRaisesRegex(ValidationError, "Existing asset differs"):
            reconcile_assets(client, remote, files, FIRST["repository"], release["tag"], runner=runner)
        runner.assert_not_called()


class PublicationTests(Workspace):
    def test_fresh_retry_keeps_verified_uploaded_bundle_and_uploads_only_missing_asset(self):
        """Plant the retry bug: a new keyless bundle must not replace a verified uploaded bundle."""
        release, output = self.plugin()
        write_bundles(output)
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        bundle = next(asset for asset in remote["assets"] if asset["name"] == "checksums.txt.sigstore.json")
        uploaded = make_bundle((output / "checksums.txt").read_bytes(), padding="earlier signing run")
        bundle["size"] = len(uploaded)
        client.assets[FIRST["repository"], bundle["id"]] = uploaded
        missing = next(asset for asset in remote["assets"] if asset["name"] == "sbom.spdx.json")
        remote["assets"].remove(missing)
        writes, verifications = [], []

        def runner(arguments, *, timeout=None):
            """Verify exact downloaded bytes locally and simulate the draft's remaining writes."""
            if arguments[0] == "cosign":
                self.assertEqual("verify-blob", arguments[1])
                self.assertEqual(uploaded, Path(arguments[3]).read_bytes())
                self.assertEqual((output / "checksums.txt").read_bytes(), Path(arguments[-1]).read_bytes())
                self.assertEqual(
                    [
                        "--certificate-identity",
                        f"https://github.com/{FIRST['repository']}/.github/workflows/build-signed.yml@refs/tags/{release['tag']}",
                        "--certificate-oidc-issuer",
                        "https://token.actions.githubusercontent.com",
                    ],
                    arguments[4:-1],
                )
                self.assertEqual(120, timeout)
                verifications.append(arguments)
                return "verified"
            writes.append(arguments)
            if "POST" in arguments:
                remote["assets"].append(missing)
            elif "PATCH" in arguments:
                client.publish(remote)

        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            self.assertEqual(
                release,
                publish(
                    client,
                    output,
                    FIRST["repository"],
                    release["tag"],
                    ".github/workflows/release.yml",
                    Mock(),
                    runner,
                    signer_workflow=".github/workflows/build-signed.yml",
                ),
            )
        self.assertTrue(verifications)
        self.assertEqual(["POST", "PATCH"], [arguments[3] for arguments in writes])
        self.assertIn(str(output / missing["name"]), writes[0])
        self.assertEqual(uploaded, client.assets[FIRST["repository"], bundle["id"]])
        self.assertNotEqual(uploaded, (output / bundle["name"]).read_bytes())

    def test_second_plugin_draft_publication_retry_and_catalog_keep_its_own_identity(self):
        first, first_output = self.plugin()
        second, output = self.plugin(SECOND, "2.3.4", b"MZindependent cinema fixture")
        client = FakeGitHub()
        client.add(FIRST, first, first_output)
        remote = client.add(SECOND, second, output, draft=True)
        missing = remote["assets"].pop()
        writes = []

        def runner(arguments):
            writes.append(arguments)
            self.assertIn(f"repos/{SECOND['repository']}/releases/{remote['id']}", arguments[4])
            if "POST" in arguments:
                remote["assets"].append(missing)
            elif "PATCH" in arguments:
                client.publish(remote)

        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            result = publish(
                client, output, SECOND["repository"], second["tag"], ".github/workflows/release.yml", Mock(), runner
            )
            self.assertEqual(second, result)
            self.assertEqual(["POST", "PATCH"], [arguments[3] for arguments in writes])
            publish(
                client, output, SECOND["repository"], second["tag"], ".github/workflows/release.yml", Mock(), runner
            )
            self.assertEqual(2, len(writes))
        catalog = collect(client, [approved(FIRST), approved(SECOND)], [], Mock())
        cinema = next(plugin for plugin in catalog if plugin["guid"] == SECOND["guid"])
        self.assertEqual("2.3.4.0", cinema["versions"][0]["version"])
        self.assertEqual(second["archive"]["url"], cinema["versions"][0]["sourceUrl"])
        self.assertEqual({FIRST["guid"], SECOND["guid"]}, {plugin["guid"] for plugin in catalog})

    def test_failed_upload_starter_is_removed_only_from_verified_draft_then_resumed(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        complete = copy.deepcopy(remote["assets"][0])
        remote["assets"][0].update(state="starter", size=0)
        writes = []

        def runner(args):
            writes.append(args)
            if "DELETE" in args:
                remote["assets"] = [asset for asset in remote["assets"] if asset["id"] != complete["id"]]
            elif "POST" in args:
                remote["assets"].append(complete)
            elif "PATCH" in args:
                client.publish(remote)

        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            publish(
                client, output, FIRST["repository"], release["tag"], ".github/workflows/release.yml", Mock(), runner
            )
        self.assertEqual(["DELETE", "POST", "PATCH"], [args[3] for args in writes])
        self.assertIn("draft=false", writes[2])

    def test_starter_recovery_refuses_published_nonempty_unexpected_or_changed_assets(self):
        release, output = self.plugin()
        files = local_artifacts(output, release)
        client = FakeGitHub()
        original = client.add(FIRST, release, output, draft=True)
        original["assets"][0].update(state="starter", size=0)
        for mutation in ("published", "nonempty", "unexpected", "id", "duplicate"):
            remote = copy.deepcopy(original)
            if mutation == "published":
                remote["draft"] = False
            elif mutation == "nonempty":
                remote["assets"][0]["size"] = 1
            elif mutation == "unexpected":
                remote["assets"][0]["name"] = "unknown.dll"
            elif mutation == "id":
                remote["assets"][0]["id"] = False
            else:
                remote["assets"].append(remote["assets"][0])
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                draft_starters(remote, files, FIRST["repository"], release["tag"])
        _, pending = draft_starters(copy.deepcopy(original), files, FIRST["repository"], release["tag"])
        original["assets"][0]["state"] = "uploaded"
        runner = Mock()
        with self.assertRaises(ValidationError):
            remove_draft_starters(client, pending, FIRST["repository"], release["tag"], original["id"], runner)
        runner.assert_not_called()

    def test_existing_different_uploaded_bytes_prevent_starter_deletion(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        remote["assets"][0].update(state="starter", size=0)
        client.assets[FIRST["repository"], remote["assets"][1]["id"]] = b"different"
        runner = Mock()
        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT), self.assertRaises(ValidationError):
            publish(
                client, output, FIRST["repository"], release["tag"], ".github/workflows/release.yml", Mock(), runner
            )
        runner.assert_not_called()

    def test_complete_release_is_verified_without_writes(self):
        release, output = self.plugin()
        client = FakeGitHub()
        client.add(FIRST, release, output)
        runner = Mock()
        verifier = Mock()
        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            self.assertEqual(
                release,
                publish(
                    client,
                    output,
                    FIRST["repository"],
                    release["tag"],
                    ".github/workflows/release.yml",
                    verifier,
                    runner,
                ),
            )
        runner.assert_not_called()
        self.assertEqual(4, verifier.call_count)
        for call in verifier.call_args_list:
            self.assertEqual(".github/workflows/release.yml", call.args[2])

    def test_publish_with_build_signed_signer_workflow(self):
        release, output = self.plugin()
        write_bundles(output)
        client = FakeGitHub()
        client.add(FIRST, release, output)
        runner = Mock()
        verifier = Mock()
        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            self.assertEqual(
                release,
                publish(
                    client,
                    output,
                    FIRST["repository"],
                    release["tag"],
                    ".github/workflows/release.yml",
                    verifier,
                    runner,
                    signer_workflow=".github/workflows/build-signed.yml",
                ),
            )
        runner.assert_not_called()
        self.assertEqual(4, verifier.call_count)
        for call in verifier.call_args_list:
            self.assertEqual(".github/workflows/build-signed.yml", call.args[2])

    def test_publish_refuses_unapproved_signer_workflows(self):
        release, output = self.plugin()
        client = FakeGitHub()
        runner = Mock()
        verifier = Mock()
        unapproved = (
            ".github/workflows/Release.yml",
            ".github/workflows/BUILD-SIGNED.YML",
            "./.github/workflows/release.yml",
            "./.github/workflows/build-signed.yml",
            "other/repo/.github/workflows/release.yml",
            "evil.yml",
            ".github/workflows/release.yml ",
            ".github/workflows/build-signed.yml\n",
            ".github/workflows/recover-release.yml",
            "",
        )
        for workflow in unapproved:
            with (
                self.subTest(workflow=workflow),
                self.assertRaisesRegex(ValidationError, "Unapproved signer workflow"),
            ):
                publish(
                    client,
                    output,
                    FIRST["repository"],
                    release["tag"],
                    ".github/workflows/release.yml",
                    verifier,
                    runner,
                    signer_workflow=workflow,
                )
        verifier.assert_not_called()
        runner.assert_not_called()

    def test_publish_refuses_build_signed_as_publisher_workflow(self):
        release, output = self.plugin()
        client = FakeGitHub()
        runner = Mock()
        verifier = Mock()
        with self.assertRaisesRegex(ValidationError, "Unapproved publisher workflow"):
            publish(
                client,
                output,
                FIRST["repository"],
                release["tag"],
                ".github/workflows/build-signed.yml",
                verifier,
                runner,
            )
        verifier.assert_not_called()
        runner.assert_not_called()

    def test_draft_resume_uploads_only_missing_bytes_and_publishes_after_verification(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        missing = remote["assets"].pop()
        writes = []

        def runner(args):
            writes.append(args)
            if "POST" in args:
                remote["assets"].append(missing)
            if "PATCH" in args:
                client.publish(remote)

        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            publish(
                client, output, FIRST["repository"], release["tag"], ".github/workflows/release.yml", Mock(), runner
            )
        self.assertEqual(["POST", "PATCH"], [args[3] for args in writes])
        self.assertIn(str(output / missing["name"]), writes[0])
        self.assertNotIn("--clobber", writes[0])
        self.assertIn("draft=false", writes[-1])

    def test_existing_mismatch_prevents_all_uploads(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        client.assets[FIRST["repository"], remote["assets"][0]["id"]] = b"different"
        remote["assets"].pop()
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            self.assertRaisesRegex(ValidationError, "differs"),
        ):
            publish(
                client, output, FIRST["repository"], release["tag"], ".github/workflows/release.yml", Mock(), runner
            )
        runner.assert_not_called()

    def test_published_incomplete_release_is_never_repaired_by_mutation(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output)
        remote["assets"].pop()
        with self.assertRaisesRegex(ValidationError, "do not mutate"):
            reconcile_assets(client, remote, local_artifacts(output, release), FIRST["repository"], release["tag"])

    def test_wrong_event_ref_or_commit_blocks_publication(self):
        release, output = self.plugin()
        values = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": FIRST["repository"],
            "GITHUB_SHA": COMMIT,
        }
        with (
            patch.dict(os.environ, values),
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            self.assertRaisesRegex(ValidationError, "tag ref"),
        ):
            publish(Mock(), output, FIRST["repository"], release["tag"], ".github/workflows/release.yml")
        values.update(GITHUB_REF="refs/tags/v1.0.0", GITHUB_SHA="b" * 40)
        with (
            patch.dict(os.environ, values),
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            self.assertRaisesRegex(ValidationError, "source differs"),
        ):
            publish(Mock(), output, FIRST["repository"], release["tag"], ".github/workflows/release.yml")

    def test_changed_checksum_file_missing_file_and_extra_remote_asset_fail(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output)
        extra = copy.deepcopy(remote["assets"][0])
        extra["name"] = "extra.txt"
        extra["browser_download_url"] = extra["browser_download_url"].rsplit("/", 1)[0] + "/extra.txt"
        remote["assets"].append(extra)
        with self.assertRaisesRegex(ValidationError, "Unexpected"):
            reconcile_assets(client, remote, local_artifacts(output, release), FIRST["repository"], release["tag"])
        (output / "checksums.txt").write_bytes(b"forged")
        with self.assertRaisesRegex(ValidationError, "checksum"):
            local_artifacts(output, release)
        (output / "checksums.txt").unlink()
        with self.assertRaisesRegex(ValidationError, "Missing"):
            local_artifacts(output, release)

    def test_local_exact_tag_checks_head_and_tracked_changes(self):
        with patch("jellysin_tooling.release.command", side_effect=[COMMIT, COMMIT, ""]) as run:
            self.assertEqual(COMMIT, exact_tag("v1.0.0"))
        self.assertIn("HEAD", run.call_args.args[0])
        with (
            patch("jellysin_tooling.release.command", side_effect=[COMMIT, "b" * 40]),
            self.assertRaisesRegex(ValidationError, "exact"),
        ):
            exact_tag("v1.0.0")
        with self.assertRaises(ValidationError):
            exact_tag("main")

    def test_remote_tag_resolves_annotated_tags_and_bounds_nesting(self):
        client = Mock()
        client.api.side_effect = [
            {"object": {"type": "tag", "sha": "b" * 40}},
            {"object": {"type": "commit", "sha": COMMIT}},
        ]
        self.assertEqual(COMMIT, remote_tag(client, FIRST["repository"], "v1.0.0"))
        client.api.side_effect = None
        client.api.return_value = {"object": {"type": "tag", "sha": "b" * 40}}
        with self.assertRaisesRegex(ValueError, "nesting"):
            remote_tag(client, FIRST["repository"], "v1.0.0")
