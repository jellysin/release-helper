"""One supported Jellyfin major, and server references held at the release the target ABI names."""

import copy
from unittest.mock import patch

from jellysin_tooling.catalog import merge_manifest, validate_manifest, validate_release
from jellysin_tooling.common import ValidationError, json_bytes, metadata, target_abi
from jellysin_tooling.inventory import from_restore, require_floor
from jellysin_tooling.package import build
from tests.fixtures import COMMIT, CREATED, FIRST, Workspace, approved
from tests.inventory_fixtures import restore_files


class TargetAbiTests(Workspace):
    def test_names_the_server_release_of_the_supported_major(self):
        self.assertEqual("12.0.0", target_abi("12.0.0.0"))
        self.assertEqual("12.2.1", target_abi("12.2.1.0"))

    def test_rejects_other_majors_and_malformed_values(self):
        for value in (
            "11.0.0.0",
            "13.0.0.0",
            "12.0.0",
            "12.0.0.1",
            "12.01.0.0",
            "12.0.0.0\n",
            "12.65535.0.0",
            12,
            None,
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, "ABI"):
                target_abi(value)

    def test_descriptor_and_catalog_follow_the_same_constant(self):
        release, _ = self.plugin()
        manifest = merge_manifest([], [release])
        raised = copy.deepcopy(manifest)
        raised[0]["versions"][0]["targetAbi"] = "13.0.0.0"
        with self.assertRaises(ValidationError):
            metadata({**FIRST, "targetAbi": "13.0.0.0"})
        with self.assertRaises(ValidationError):
            validate_manifest(raised)
        with patch("jellysin_tooling.common.JELLYFIN_ABI_MAJOR", 13):
            metadata({**FIRST, "targetAbi": "13.0.0.0"})
            validate_manifest(raised)
            with self.assertRaises(ValidationError):
                metadata(copy.deepcopy(FIRST))
            with self.assertRaises(ValidationError):
                validate_manifest(manifest)


class FloorTests(Workspace):
    def inventory(self, version="12.0.0"):
        paths, _ = restore_files(self.root, version)
        return from_restore(*paths)

    def test_reference_equal_to_the_floor_is_accepted(self):
        value = self.inventory()
        self.assertIs(value, require_floor(value, "12.0.0"))
        raised = self.inventory("12.2.0")
        self.assertIs(raised, require_floor(raised, "12.2.0"))

    def test_floor_above_the_reference_fails(self):
        for floor in ("13.0.0", "12.2.0", "12.0.1"):
            with self.subTest(floor=floor), self.assertRaisesRegex(ValidationError, "differs from the target ABI"):
                require_floor(self.inventory(), floor)

    def test_reference_above_the_floor_fails(self):
        for reference in ("13.0.0", "12.1.0", "12.0.1"):
            with self.subTest(reference=reference), self.assertRaisesRegex(ValidationError, "differs from the target"):
                require_floor(self.inventory(reference), "12.0.0")

    def test_every_host_provided_server_package_is_compared(self):
        value = self.inventory()
        value["packages"][1].update(hostProvided=True, version="12.1.0")
        with self.assertRaisesRegex(ValidationError, "differs from the target ABI"):
            require_floor(value, "12.0.0")

    def test_packages_outside_the_server_family_keep_their_own_versions(self):
        value = self.inventory()
        value["packages"][1].update(id="Vendor.Logging", hostProvided=True, version="9.4.1")
        require_floor(value, "12.0.0")

    def test_nothing_to_compare_fails(self):
        for mutation in ("undeclared", "renamed"):
            value = self.inventory()
            if mutation == "undeclared":
                value["packages"][0]["hostProvided"] = False
            else:
                value["packages"][0]["id"] = "Jellyfin.Renamed"
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValidationError, "Jellyfin.Controller"):
                require_floor(value, "12.0.0")

    def test_direct_server_reference_must_be_declared_host_provided(self):
        paths, documents = restore_files(self.root)
        documents[0]["dependencies"]["net10.0"]["Jellyfin.Data"]["type"] = "Direct"
        paths[0].write_bytes(json_bytes(documents[0]))
        with self.assertRaisesRegex(ValidationError, "not declared host-provided"):
            from_restore(*paths)
        documents[2]["runtimeDependencies"].append(
            {"id": "Jellyfin.Data", "version": "12.0.0", "providedBy": "jellyfin"}
        )
        paths[2].write_bytes(json_bytes(documents[2]))
        self.assertTrue(all(entry["hostProvided"] for entry in from_restore(*paths)["packages"]))

    def test_package_refuses_a_floor_that_differs_from_the_reference(self):
        source = self.root / "publish"
        source.mkdir()
        (source / FIRST["assemblyFile"]).write_bytes(b"assembly")
        for abi, reference in (("12.2.0.0", "12.0.0"), ("12.0.0.0", "12.2.0")):
            output = self.root / abi / reference
            with self.subTest(abi=abi, reference=reference), self.assertRaisesRegex(ValidationError, "differs from"):
                build(source, {**FIRST, "targetAbi": abi}, "1.0.0", COMMIT, CREATED, output, self.inventory(reference))
            self.assertFalse(output.exists())
        build(source, FIRST, "1.0.0", COMMIT, CREATED, self.root / "dist", self.inventory())

    def test_catalog_refuses_a_release_whose_floor_differs_from_the_reference(self):
        source = self.root / "publish"
        source.mkdir()
        (source / FIRST["assemblyFile"]).write_bytes(b"assembly")
        release = build(source, FIRST, "1.0.0", COMMIT, CREATED, self.root / "dist", self.inventory())
        validate_release(release, approved(), "v1.0.0")
        for abi in ("12.2.0.0", "12.0.1.0"):
            above = copy.deepcopy(release)
            above["plugin"]["targetAbi"] = abi
            with self.subTest(abi=abi), self.assertRaisesRegex(ValidationError, "differs from the target ABI"):
                validate_release(above, approved(), "v1.0.0")
        below = copy.deepcopy(release)
        for entry in below["dependencyInventory"]["packages"]:
            entry["version"] = "12.1.0"
        with self.assertRaisesRegex(ValidationError, "differs from the target ABI"):
            validate_release(below, approved(), "v1.0.0")
