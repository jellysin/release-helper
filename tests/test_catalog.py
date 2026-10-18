"""Exercise the complete catalog import, trust boundary, and immutable history."""

import contextlib
import copy
import io
import zipfile
from unittest.mock import Mock, patch

from jellysin_tooling.catalog import (
    allowlist,
    collect,
    merge_manifest,
    release_assets,
    update,
    validate_archive,
    validate_manifest,
    validate_release,
    verified_release,
)
from jellysin_tooling.common import ValidationError, digest
from tests.fixtures import FIRST, SECOND, FakeGitHub, Workspace, approved


class CatalogTests(Workspace):
    def test_two_plugin_end_to_end_import_and_idempotency(self):
        client = FakeGitHub()
        for info in (FIRST, SECOND):
            release, output = self.plugin(info)
            client.add(info, release, output)
        verifier = Mock()
        result = collect(client, [approved(FIRST), approved(SECOND)], [], verifier)
        self.assertEqual({FIRST["guid"], SECOND["guid"]}, {item["guid"] for item in result})
        self.assertEqual(8, verifier.call_count)
        before = list(client.calls)
        self.assertEqual(result, collect(client, [approved(FIRST), approved(SECOND)], result, verifier))
        self.assertEqual(2, len(client.calls) - len(before))

    def test_addition_preserves_old_compatible_versions(self):
        initial, _ = self.plugin()
        newer, _ = self.plugin(version="1.1.0")
        current = merge_manifest([], [initial])
        result = merge_manifest(current, [newer])
        self.assertEqual(["1.1.0.0", "1.0.0.0"], [item["version"] for item in result[0]["versions"]])
        self.assertEqual(current[0]["versions"][0], result[0]["versions"][1])
        self.assertEqual(1, len(current[0]["versions"]))

    def test_same_version_different_bytes_rejected(self):
        initial, _ = self.plugin()
        current = merge_manifest([], [initial])
        tampered = copy.deepcopy(initial)
        tampered["archive"]["md5"] = "0" * 32
        with self.assertRaisesRegex(ValidationError, "immutable"):
            merge_manifest(current, [tampered])

    def test_failed_attestation_never_changes_catalog(self):
        release, output = self.plugin()
        client = FakeGitHub()
        client.add(FIRST, release, output)
        manifest = self.root / "manifest.json"
        manifest.write_bytes(b"[]\n")
        with self.assertRaises(ValidationError):
            merged = collect(client, [approved()], [], Mock(side_effect=ValidationError("untrusted signer")))
            update(manifest, [], merged)
        self.assertEqual(b"[]\n", manifest.read_bytes())

    def test_skip_draft_prerelease_and_non_version_tags(self):
        client = FakeGitHub()
        client.releases[FIRST["repository"]] = [{"draft": True}, {"prerelease": True}, {"tag_name": "preview"}]
        self.assertEqual([], collect(client, [approved()], [], Mock()))

    def test_limits_pending_imports_and_api_pagination(self):
        release, _ = self.plugin()
        client = Mock()
        repo = approved()["repository"]
        client.api.return_value = [
            {
                "tag_name": f"v1.0.{index}",
                "immutable": True,
                "assets": [
                    {
                        "name": name,
                        "state": "uploaded",
                        "size": 100,
                        "browser_download_url": f"https://github.com/{repo}/releases/download/v1.0.{index}/{name}",
                    }
                    for name in ("release.json", "checksums.txt", "sbom.spdx.json")
                ],
            }
            for index in range(21)
        ]
        with patch("jellysin_tooling.catalog.verified_release", return_value=release) as verify:
            collect(client, [approved()], [])
        self.assertEqual(20, verify.call_count)
        client.api.return_value = [{"draft": True}] * 100
        with self.assertRaisesRegex(ValidationError, "1000"):
            collect(client, [approved()], [])

    def test_empty_assets_release_skipped_with_warning_and_valid_releases_imported(self):
        client = FakeGitHub()
        release_0, output_0 = self.plugin(version="1.0.0")
        release_1, output_1 = self.plugin(version="1.0.1")
        remote_0 = client.add(FIRST, release_0, output_0)
        remote_1 = client.add(FIRST, release_1, output_1)
        repo = FIRST["repository"]
        incomplete = {
            "tag_name": "v1.0.2",
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "assets": [],
        }
        client.releases[repo] = [incomplete, remote_1, remote_0]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = collect(client, [approved(FIRST)], [], Mock())
        expected_warning = (
            f"::warning title=Catalog import skipped::{repo} v1.0.2: release has no metadata, checksums, or SBOM\n"
        )
        self.assertEqual(expected_warning, stream.getvalue())
        self.assertEqual(["1.0.1.0", "1.0.0.0"], [v["version"] for v in result[0]["versions"]])

    def test_release_missing_sbom_skipped_with_warning_and_others_imported(self):
        client = FakeGitHub()
        release_0, output_0 = self.plugin(version="1.0.0")
        release_1, output_1 = self.plugin(version="1.0.1")
        remote_0 = client.add(FIRST, release_0, output_0)
        remote_1 = client.add(FIRST, release_1, output_1)
        repo = FIRST["repository"]
        remote_1["assets"] = [asset for asset in remote_1["assets"] if asset["name"] != "sbom.spdx.json"]
        client.releases[repo] = [remote_1, remote_0]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = collect(client, [approved(FIRST)], [], Mock())
        expected_warning = (
            f"::warning title=Catalog import skipped::{repo} v1.0.1: release has no metadata, checksums, or SBOM\n"
        )
        self.assertEqual(expected_warning, stream.getvalue())
        self.assertEqual(["1.0.0.0"], [v["version"] for v in result[0]["versions"]])

    def test_tampered_checksums_aborts_collect_and_imports_nothing(self):
        client = FakeGitHub()
        release_0, output_0 = self.plugin(version="1.0.0")
        release_1, output_1 = self.plugin(version="1.0.1")
        remote_0 = client.add(FIRST, release_0, output_0)
        remote_1 = client.add(FIRST, release_1, output_1)
        repo = FIRST["repository"]
        url = next(item["browser_download_url"] for item in remote_1["assets"] if item["name"] == "checksums.txt")
        client.downloads[url] = b"forged"
        client.releases[repo] = [remote_1, remote_0]
        with self.assertRaisesRegex(ValidationError, "Release checksum manifest mismatch"):
            collect(client, [approved(FIRST)], [], Mock())

    def test_missing_plugin_zip_is_not_skipped_and_raises(self):
        client = FakeGitHub()
        release, output = self.plugin(version="1.0.0")
        remote = client.add(FIRST, release, output)
        remote["assets"] = [asset for asset in remote["assets"] if asset["name"] != release["archive"]["name"]]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            with self.assertRaisesRegex(ValidationError, "Release is missing plugin ZIP"):
                collect(client, [approved(FIRST)], [], Mock())
        self.assertEqual("", stream.getvalue())

    def test_atomic_update_and_noop(self):
        release, _ = self.plugin()
        merged = merge_manifest([], [release])
        manifest = self.root / "manifest.json"
        update(manifest, [], merged)
        before = manifest.stat().st_mtime_ns
        update(manifest, merged, merged)
        self.assertEqual(before, manifest.stat().st_mtime_ns)

    def test_allowlist_duplicate_identity_and_workflow(self):
        self.assertEqual([approved()], allowlist({"schemaVersion": 1, "plugins": [approved()]}))
        build_signed = {**approved(), "signerWorkflow": ".github/workflows/build-signed.yml"}
        self.assertEqual([build_signed], allowlist({"schemaVersion": 1, "plugins": [build_signed]}))
        unapproved = (
            "evil.yml",
            "./.github/workflows/build-signed.yml",
            "./.github/workflows/release.yml",
            ".github/workflows/Release.yml",
            ".github/workflows/BUILD-SIGNED.YML",
            ".github/workflows/release.yml ",
            ".github/workflows/build-signed.yml ",
            ".github/workflows/recover-release.yml",
            "other/repo/.github/workflows/build-signed.yml",
            "",
        )
        for workflow in unapproved:
            with (
                self.subTest(workflow=workflow),
                self.assertRaisesRegex(ValidationError, "Unapproved release workflow"),
            ):
                allowlist({"schemaVersion": 1, "plugins": [{**approved(), "signerWorkflow": workflow}]})
        for plugins in ([], [approved(), approved()], [None]):
            with self.subTest(plugins=plugins), self.assertRaises(ValidationError):
                allowlist({"schemaVersion": 1, "plugins": plugins})

    def test_manifest_rejects_malformed_and_duplicate_versions(self):
        release, _ = self.plugin()
        entry = merge_manifest([], [release])[0]
        for value in (
            {},
            [None],
            [entry, entry],
            [{**entry, "versions": []}],
            [{**entry, "versions": entry["versions"] * 2}],
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_manifest(value)


class ReleaseBoundaryTests(Workspace):
    def test_release_metadata_cannot_change_source_identity_or_url(self):
        release, _ = self.plugin()
        mutations = [
            ("schemaVersion", 2),
            ("version", "2.0.0.0"),
            ("commit", "short"),
            ("timestamp", "2026-09-08T14:00:00+02:00"),
            ("tag", "v2.0.0"),
        ]
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                validate_release({**release, key: value}, approved(), "v1.0.0")
        for field, value in (
            ("url", "https://evil.test/x.zip"),
            ("name", "evil.zip"),
            ("size", True),
            ("sha256", "bad"),
        ):
            bad = copy.deepcopy(release)
            bad["archive"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_release(bad, approved(), "v1.0.0")
        with self.assertRaises(ValidationError):
            validate_release(release, approved(SECOND), "v1.0.0")

    def test_duplicate_assets_external_urls_and_incomplete_uploads_rejected(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output)
        for field, value in (("browser_download_url", "https://evil.test/a"), ("state", "starter"), ("size", -1)):
            bad = copy.deepcopy(remote)
            bad["assets"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                release_assets(bad, FIRST["repository"], release["tag"])
        remote["assets"].append(remote["assets"][0])
        with self.assertRaises(ValidationError):
            release_assets(remote, FIRST["repository"], release["tag"])

    def test_crc_contents_and_compression_validated_without_extraction(self):
        release, output = self.plugin()
        original = (output / release["archive"]["name"]).read_bytes()
        with self.assertRaises(ValidationError):
            validate_archive(original + b"tampered", release)
        for name, compression in (("../escape.dll", zipfile.ZIP_STORED), (FIRST["assemblyFile"], zipfile.ZIP_DEFLATED)):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
                archive.writestr(name, b"a")
            data = buffer.getvalue()
            release["archive"].update(size=len(data), sha256=digest(data), md5=digest(data, "md5"))
            with self.subTest(name=name), self.assertRaises(ValidationError):
                validate_archive(data, release)

    def test_missing_asset_or_wrong_checksums_abort_import(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output)
        url = next(item["browser_download_url"] for item in remote["assets"] if item["name"] == "checksums.txt")
        client.downloads[url] = b"forged"
        with self.assertRaisesRegex(ValidationError, "checksum"):
            verified_release(client, approved(), remote, Mock())
        remote["assets"] = []
        with self.assertRaisesRegex(ValidationError, "missing"):
            verified_release(client, approved(), remote, Mock())
