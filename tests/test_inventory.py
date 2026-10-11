"""Production lock/restore consistency, host ownership and bounded inventory inputs."""

import copy
import json
from unittest.mock import patch

from jellysin_tooling.common import ValidationError, json_bytes
from jellysin_tooling.inventory import from_restore, package_id, package_version, sha512, validate
from tests.fixtures import Workspace
from tests.inventory_fixtures import restore_files


class InventoryTests(Workspace):
    def test_records_full_production_graph_and_explicit_host_runtime_subset(self):
        paths, _ = restore_files(self.root)
        result = from_restore(*paths)
        self.assertEqual("net10.0", result["targetFramework"])
        self.assertEqual("10.0.400", result["sdkVersion"])
        self.assertEqual(["Jellyfin.Controller", "Jellyfin.Data"], [entry["id"] for entry in result["packages"]])
        self.assertTrue(result["packages"][0]["hostProvided"])
        self.assertFalse(result["packages"][1]["hostProvided"])
        self.assertEqual(["Jellyfin.Data"], result["packages"][0]["dependencies"])
        self.assertEqual((b"a" * 64).hex(), result["packages"][0]["sha512"])

    def test_checkout_paths_and_lock_line_endings_do_not_change_inventory(self):
        paths, documents = restore_files(self.root)
        first = from_restore(*paths)
        documents[1]["packageFolders"] = {"D:\\different\\checkout\\": {}}
        paths[1].write_bytes(json_bytes(documents[1]))
        paths[0].write_bytes(json.dumps(documents[0], indent=4).replace("\n", "\r\n").encode())
        self.assertEqual(first, from_restore(*paths))

    def test_lock_and_restore_version_hash_or_edges_must_match(self):
        for mutation in ("version", "hash", "edges", "absent", "type"):
            paths, documents = restore_files(self.root)
            assets = documents[1]
            if mutation == "version":
                assets["targets"]["net10.0"]["Jellyfin.Controller/13.0.0"] = assets["targets"]["net10.0"].pop(
                    "Jellyfin.Controller/12.0.0"
                )
            elif mutation == "hash":
                assets["libraries"]["Jellyfin.Controller/12.0.0"]["sha512"] = assets["libraries"][
                    "Jellyfin.Data/12.0.0"
                ]["sha512"]
            elif mutation == "edges":
                assets["targets"]["net10.0"]["Jellyfin.Controller/12.0.0"]["dependencies"] = {}
            elif mutation == "absent":
                del assets["libraries"]["Jellyfin.Controller/12.0.0"]
            else:
                assets["targets"]["net10.0"]["Jellyfin.Controller/12.0.0"]["type"] = "project"
            paths[1].write_bytes(json_bytes(assets))
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                from_restore(*paths)

    def test_unknown_or_changed_host_dependency_and_invalid_framework_fail(self):
        for field, value in (("id", "Missing"), ("version", "13.0.0"), ("providedBy", "bundled")):
            paths, documents = restore_files(self.root)
            documents[2]["runtimeDependencies"][0][field] = value
            paths[2].write_bytes(json_bytes(documents[2]))
            with self.subTest(field=field), self.assertRaises(ValidationError):
                from_restore(*paths)
        paths, documents = restore_files(self.root)
        documents[2]["targetFramework"] = "net9.0"
        paths[2].write_bytes(json_bytes(documents[2]))
        with self.assertRaises(ValidationError):
            from_restore(*paths)

    def test_malformed_and_oversized_documents_fail_closed(self):
        for index, value in ((0, []), (1, {"version": 3, "targets": []}), (2, {}), (3, {"sdk": None})):
            paths, _ = restore_files(self.root)
            paths[index].write_bytes(json_bytes(value))
            with self.subTest(index=index), self.assertRaises(ValidationError):
                from_restore(*paths)
        paths, _ = restore_files(self.root)
        with patch("jellysin_tooling.inventory.MAX_PACKAGES", 1), self.assertRaises(ValidationError):
            from_restore(*paths)

    def test_serialized_inventory_rejects_duplicate_unresolved_or_noncanonical_packages(self):
        paths, _ = restore_files(self.root)
        original = from_restore(*paths)
        for mutation in ("duplicate", "edge", "order", "hash", "owner"):
            value = copy.deepcopy(original)
            if mutation == "duplicate":
                value["packages"].append(value["packages"][0])
            elif mutation == "edge":
                value["packages"][0]["dependencies"] = ["Missing"]
            elif mutation == "order":
                value["packages"].reverse()
            elif mutation == "hash":
                value["packages"][0]["sha512"] = None
            else:
                value["packages"][0]["hostProvided"] = "true"
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                validate(value)

    def test_package_identifiers_versions_and_hashes_are_bounded(self):
        for function, value in (
            (package_id, "../escape"),
            (package_version, "latest"),
            (sha512, "%%%"),
            (sha512, "YQ=="),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                function(value)
