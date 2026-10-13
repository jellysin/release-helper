"""The catalog and validate-catalog commands, run as the action runs them (tools.py main)."""

import copy
import zipfile
from unittest.mock import patch

from jellysin_tooling.catalog import digest, merge_manifest, validate_archive
from jellysin_tooling.common import json_bytes, read_json
from tests.fixtures import FIRST, SECOND, FakeGitHub, Workspace, approved
from tests.test_tool_name import message

UNAPPROVED = "jellysin-release-helper: Manifest contains an unapproved plugin\n"
FOREIGN_SOURCE = "jellysin-release-helper: Catalog source differs from approved repository\n"


def moved_to(manifest, repo):
    """The manifest with its one version pointing at the same release of another repository."""
    moved = copy.deepcopy(manifest)
    version = moved[0]["versions"][0]
    stable = version["version"].removesuffix(".0")
    name = repo.split("/")[1]
    version["sourceUrl"] = f"https://github.com/{repo}/releases/download/v{stable}/{name}-{stable}.zip"
    version["changelog"] = f"https://github.com/{repo}/releases/tag/v{stable}"
    return moved


class CatalogCommandTests(Workspace):
    def setUp(self):
        super().setUp()
        self.release, self.output = self.plugin()
        self.current = merge_manifest([], [self.release])
        self.manifest = self.root / "manifest.json"
        self.allowlist = self.root / "plugins.json"

    def write(self, manifest, *plugins):
        self.manifest.write_bytes(json_bytes(manifest))
        self.allowlist.write_bytes(json_bytes({"schemaVersion": 1, "plugins": [approved(info) for info in plugins]}))

    def command(self, name):
        return message([name, "--manifest", str(self.manifest), "--allowlist", str(self.allowlist)])

    def test_validate_catalog_accepts_a_listed_plugin_from_its_own_repository(self):
        self.write(self.current, FIRST, SECOND)
        self.assertEqual(self.command("validate-catalog"), (0, ""))

    def test_an_unapproved_plugin_is_refused_and_nothing_is_written(self):
        self.write(self.current, SECOND)
        before = self.manifest.read_bytes()
        for name in ("validate-catalog", "catalog"):
            with self.subTest(command=name), patch("jellysin_tooling.cli.GitHub") as client:
                self.assertEqual(self.command(name), (1, UNAPPROVED))
                client.assert_not_called()
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_a_source_in_another_repository_is_refused(self):
        # The second repository shares the first one's name as a prefix: a prefix match would pass it.
        for repo in (
            SECOND["repository"],
            FIRST["repository"] + "-fork",
            "someone/" + FIRST["repository"].split("/")[1],
        ):
            self.write(moved_to(self.current, repo), FIRST)
            for name in ("validate-catalog", "catalog"):
                with self.subTest(repo=repo, command=name), patch("jellysin_tooling.cli.GitHub") as client:
                    self.assertEqual(self.command(name), (1, FOREIGN_SOURCE))
                    client.assert_not_called()

    def test_catalog_imports_a_new_release_through_the_real_collect_and_verifier(self):
        client = FakeGitHub()
        client.add(FIRST, self.release, self.output)
        newer, newer_output = self.plugin(version="1.1.0")
        client.add(FIRST, newer, newer_output)
        self.write(self.current, FIRST)
        with (
            patch("jellysin_tooling.cli.GitHub", return_value=client),
            patch("jellysin_tooling.github.command", return_value="") as gh,
        ):
            self.assertEqual(self.command("catalog"), (0, ""))
        versions = [item["version"] for item in read_json(self.manifest)[0]["versions"]]
        self.assertEqual(sorted(versions), ["1.0.0.0", "1.1.0.0"])
        verified = [call.args[0] for call in gh.call_args_list]
        self.assertTrue(verified, "the production verifier must run")
        for arguments in verified:
            self.assertEqual(arguments[:3], ["gh", "attestation", "verify"])
            self.assertIn(f"{FIRST['repository']}/.github/workflows/release.yml", arguments)
            self.assertIn("refs/tags/v1.1.0", arguments)


class ArchiveTests(Workspace):
    def test_bytes_matching_every_checksum_but_not_a_zip_are_refused(self):
        release, _ = self.plugin()
        for data in (b"not a zip archive", b"PK\x03\x04" + b"\0" * 26):
            forged = copy.deepcopy(release)
            forged["archive"].update(size=len(data), sha256=digest(data), md5=digest(data, "md5"))
            with self.subTest(data=data[:4]), self.assertRaisesRegex(ValueError, "Invalid ZIP archive") as caught:
                validate_archive(data, forged)
            self.assertIsInstance(caught.exception.__cause__, zipfile.BadZipFile)
