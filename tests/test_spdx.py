"""SPDX semantic checks tie files and external dependencies to the released bytes."""

import copy
from unittest.mock import Mock

from jellysin_tooling.catalog import verified_release
from jellysin_tooling.common import ValidationError, decode_json, digest, json_bytes
from jellysin_tooling.inventory import from_restore
from jellysin_tooling.package import build
from jellysin_tooling.release import local_artifacts
from tests.fixtures import COMMIT, CREATED, FIRST, SECOND, FakeGitHub, Workspace, approved
from tests.inventory_fixtures import restore_files


def refresh_checksums(output):
    contents = "".join(
        f"{digest(path.read_bytes())}  {path.name}\n"
        for path in sorted(output.iterdir())
        if path.name != "checksums.txt"
    )
    (output / "checksums.txt").write_text(contents, encoding="utf-8", newline="\n")


class SpdxTests(Workspace):
    def test_analyzed_files_have_sha1_sha256_and_verification_code(self):
        release, output = self.plugin(contents=b"abc")
        document = decode_json((output / "sbom.spdx.json").read_bytes())
        root = document["packages"][0]
        self.assertTrue(root["filesAnalyzed"])
        self.assertEqual(
            [
                {"algorithm": "SHA1", "checksumValue": "a9993e364706816aba3e25717850c26c9cd0d89d"},
                {
                    "algorithm": "SHA256",
                    "checksumValue": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                },
            ],
            document["files"][0]["checksums"],
        )
        self.assertEqual(
            "9ef2bdeea2b1bae79b9ddb930427d0b2c880bdac",
            root["packageVerificationCode"]["packageVerificationCodeValue"],
        )
        local_artifacts(output, release)

    def test_external_dependency_graph_is_reproducible_for_independent_plugins(self):
        paths, _ = restore_files(self.root)
        inventory = from_restore(*paths)
        for info in (FIRST, SECOND):
            _, old = self.plugin(info)
            output = old.parent / "inventory-dist"
            release = build(old.parent / "publish", info, "1.0.0", COMMIT, CREATED, output, inventory)
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            build(old.parent / "publish", info, "1.0.0", COMMIT, CREATED, output, inventory)
            self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})
            document = decode_json((output / "sbom.spdx.json").read_bytes())
            self.assertEqual(3, len(document["packages"]))
            self.assertEqual(1, len(document["files"]))
            self.assertFalse(document["packages"][1]["filesAnalyzed"])
            self.assertEqual(
                "pkg:nuget/Jellyfin.Controller@12.0.0", document["packages"][1]["externalRefs"][0]["referenceLocator"]
            )
            self.assertTrue(
                any(edge["relationshipType"] == "RUNTIME_DEPENDENCY_OF" for edge in document["relationships"])
            )
            self.assertEqual(inventory, release["dependencyInventory"])
            client = FakeGitHub()
            remote = client.add(info, release, output)
            self.assertEqual(release, verified_release(client, approved(info), remote, Mock()))

    def test_publisher_and_catalog_reject_resigned_semantically_wrong_inventory(self):
        release, output = self.plugin()
        original = decode_json((output / "sbom.spdx.json").read_bytes())
        for mutation in ("missing", "analyzed", "sha1", "verification", "identity", "dependencies"):
            document = copy.deepcopy(original)
            if mutation == "missing":
                document = {"spdxVersion": "SPDX-2.3"}
            elif mutation == "analyzed":
                document["packages"][0]["filesAnalyzed"] = False
            elif mutation == "sha1":
                document["files"][0]["checksums"].pop(0)
            elif mutation == "verification":
                document["packages"][0]["packageVerificationCode"]["packageVerificationCodeValue"] = "0" * 40
            elif mutation == "identity":
                document["packages"][0]["versionInfo"] = "9.0.0"
            else:
                document["relationships"] = []
            (output / "sbom.spdx.json").write_bytes(json_bytes(document))
            refresh_checksums(output)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValidationError, "SBOM differs"):
                local_artifacts(output, release)
            client = FakeGitHub()
            remote = client.add(FIRST, release, output)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValidationError, "SBOM differs"):
                verified_release(client, approved(), remote, Mock())
