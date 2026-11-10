"""Load and soak tests for catalog manifest bounds and incremental updates."""

import copy
import uuid

from jellysin_tooling.catalog import manifest_entry, merge_manifest, update, validate_manifest
from jellysin_tooling.common import MAX_RELEASES, ValidationError, json_bytes
from tests.fixtures import Workspace


class CatalogLoadTests(Workspace):
    """Verify catalog performance, immutability, and boundary enforcement under load."""

    def _make_release(self, base: dict, index: int) -> dict:
        """Create a distinct plugin release entry from a base template."""
        release = copy.deepcopy(base)
        stable = f"1.0.{index}"
        release["version"] = f"{stable}.0"
        release["tag"] = f"v{stable}"
        repo = release["plugin"]["repository"]
        repo_name = repo.split("/")[1]
        release["archive"]["name"] = f"{repo_name}-{stable}.zip"
        release["archive"]["url"] = f"https://github.com/{repo}/releases/download/v{stable}/{repo_name}-{stable}.zip"
        release["archive"]["md5"] = f"{index:032x}"
        release["changelog"] = f"https://github.com/{repo}/releases/tag/v{stable}"
        return release

    def test_volume_max_releases_and_overflow(self) -> None:
        """Validate catalog merges up to MAX_RELEASES versions and rejects an overflow release without mutating state."""
        base_release, _ = self.plugin()
        releases = [self._make_release(base_release, i) for i in range(MAX_RELEASES)]
        catalog = merge_manifest([], releases)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(len(catalog[0]["versions"]), MAX_RELEASES)
        self.assertEqual(catalog, validate_manifest(catalog))

        catalog_copy = copy.deepcopy(catalog)
        overflow_release = self._make_release(base_release, MAX_RELEASES)
        with self.assertRaisesRegex(ValidationError, "Invalid version list"):
            merge_manifest(catalog, [overflow_release])
        self.assertEqual(catalog, catalog_copy)

    def test_width_plugin_manifest_cap(self) -> None:
        """Validate manifest accepts up to 32 plugin definitions and rejects a 33rd entry."""
        base_release, _ = self.plugin()
        sample_version = manifest_entry(base_release)
        manifest32 = [
            {
                "guid": str(uuid.UUID(int=i + 1)),
                "name": f"Plugin {i}",
                "description": f"Description for plugin {i}",
                "overview": f"Overview for plugin {i}",
                "owner": "JellySin",
                "category": "General",
                "versions": [copy.deepcopy(sample_version)],
            }
            for i in range(32)
        ]
        self.assertEqual(validate_manifest(manifest32), manifest32)

        manifest33 = copy.deepcopy(manifest32)
        manifest33.append(
            {
                "guid": str(uuid.UUID(int=33)),
                "name": "Plugin 32",
                "description": "Description for plugin 32",
                "overview": "Overview for plugin 32",
                "owner": "JellySin",
                "category": "General",
                "versions": [copy.deepcopy(sample_version)],
            }
        )
        with self.assertRaisesRegex(ValidationError, "Manifest must be a bounded array"):
            validate_manifest(manifest33)

    def test_soak_repeated_publish_cycles(self) -> None:
        """Validate at least 200 incremental catalog update cycles preserve immutability and skip duplicate writes."""
        catalog_path = self.root / "plugins.json"
        base_release, _ = self.plugin()
        previous = []
        earlier_entries = {}

        for cycle in range(200):
            rel = self._make_release(base_release, cycle)
            merged = merge_manifest(previous, [rel])
            update(catalog_path, previous, merged)

            self.assertEqual(catalog_path.read_bytes(), json_bytes(merged))
            current_entry = next(v for v in merged[0]["versions"] if v["version"] == rel["version"])
            earlier_entries[rel["version"]] = json_bytes(current_entry)
            for version_record in merged[0]["versions"]:
                self.assertEqual(json_bytes(version_record), earlier_entries[version_record["version"]])

            remerged = merge_manifest(merged, [rel])
            self.assertEqual(remerged, merged)
            mtime_before = catalog_path.stat().st_mtime_ns
            bytes_before = catalog_path.read_bytes()
            update(catalog_path, merged, remerged)
            self.assertEqual(catalog_path.stat().st_mtime_ns, mtime_before)
            self.assertEqual(catalog_path.read_bytes(), bytes_before)

            previous = merged
