"""Verify reproducibility, the installation boundary, and malformed inputs."""

import copy
import io
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling.common import ValidationError, atomic_write, decode_json, filename, metadata, semver, timestamp
from jellysin_tooling.package import archive_bytes, build, package_files
from tests.fixtures import COMMIT, CREATED, FIRST, SECOND, Workspace


class PackagingTests(Workspace):
    def test_reproducible_across_mtimes_and_independent_plugin_identities(self):
        first, output = self.plugin()
        before = {path.name: path.read_bytes() for path in output.iterdir()}
        source = output.parent / "publish"
        os.utime(source / FIRST["assemblyFile"], (123, 456))
        build(source, FIRST, "1.0.0", COMMIT, CREATED, output)
        self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})
        second, other = self.plugin(SECOND, "2.3.4")
        self.assertNotEqual(first["plugin"]["guid"], second["plugin"]["guid"])
        self.assertEqual("2.3.4.0", second["version"])
        with zipfile.ZipFile(other / second["archive"]["name"]) as archive:
            self.assertEqual([SECOND["assemblyFile"]], archive.namelist())

    def test_only_explicit_plugin_owned_files_are_shipped(self):
        _, output = self.plugin()
        source = output.parent / "publish"
        (source / "Jellyfin.Controller.dll").write_bytes(b"host")
        result = build(source, FIRST, "1.0.1", COMMIT, CREATED, self.root / "next")
        with zipfile.ZipFile(self.root / "next" / result["archive"]["name"]) as archive:
            self.assertEqual([FIRST["assemblyFile"]], archive.namelist())

    def test_refuses_different_existing_release_bytes(self):
        _, output = self.plugin()
        source = output.parent / "publish"
        (source / FIRST["assemblyFile"]).write_bytes(b"different")
        with self.assertRaisesRegex(ValidationError, "replace"):
            build(source, FIRST, "1.0.0", COMMIT, CREATED, output)

    def test_manifested_extra_owned_file_has_fixed_order(self):
        info = {**FIRST, "packageFiles": ["settings.json"]}
        source = self.root / "publish"
        source.mkdir()
        (source / FIRST["assemblyFile"]).write_bytes(b"assembly")
        (source / "settings.json").write_bytes(b"{}")
        result = build(source, info, "1.0.0", COMMIT, CREATED, self.root / "dist")
        with zipfile.ZipFile(self.root / "dist" / result["archive"]["name"]) as archive:
            self.assertEqual(sorted([FIRST["assemblyFile"], "settings.json"]), archive.namelist())

    def test_empty_and_oversized_files_rejected(self):
        for data in (b"", b"x" * 11):
            with (
                self.subTest(size=len(data)),
                patch("jellysin_tooling.package.MAX_ASSET", 10),
                self.assertRaises(ValidationError),
            ):
                self.plugin(contents=data)

    def test_archive_size_is_bounded(self):
        with patch("jellysin_tooling.package.MAX_ASSET", 10), self.assertRaises(ValidationError):
            archive_bytes({"a.dll": b"a"})

    def test_sbom_identifies_every_shipped_file_without_claiming_dependency_licenses(self):
        _, output = self.plugin()
        sbom = decode_json((output / "sbom.spdx.json").read_bytes())
        self.assertEqual("SPDX-2.3", sbom["spdxVersion"])
        self.assertEqual(FIRST["assemblyFile"], sbom["files"][0]["fileName"])
        self.assertEqual("NOASSERTION", sbom["files"][0]["licenseConcluded"])
        self.assertEqual(CREATED, sbom["creationInfo"]["created"])

    def test_rejects_symlink_even_if_it_points_inside_publish_directory(self):
        _, output = self.plugin()
        source = output.parent / "publish"
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaisesRegex(ValidationError, "Symbolic"):
            build(source, FIRST, "1.0.0", COMMIT, CREATED, output)

    def test_atomic_write_leaves_previous_file_on_replace_failure(self):
        target = self.root / "manifest.json"
        target.write_bytes(b"old")
        with patch("jellysin_tooling.common.os.replace", side_effect=OSError), self.assertRaises(OSError):
            atomic_write(target, b"new")
        self.assertEqual(b"old", target.read_bytes())
        self.assertEqual([target], list(self.root.iterdir()))


class InputTests(Workspace):
    def test_invalid_versions(self):
        # packaging 26.3 parses 1.2.beta, v1.2.3, 01.2.3 and 1.2.3-rc.1 as PEP 440 versions; semver() refuses them.
        planted = ("1.2.beta", "v1.2.3", "01.2.3", "1.2.3-rc.1")
        for value in (None, *planted, "1.2", "1.2.3.0", "1.2.3-beta", "1.2.65535", "1.2.3\n"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                semver(value)

    def test_path_traversal_and_shell_metacharacters(self):
        for value in ("../x", "a\\b", "/a", "a:stream", "a\n", "a$(env)", "a.", "a..dll", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                filename(value)

    def test_invalid_json_is_bounded_and_rejects_duplicate_keys(self):
        for value in (b'{"a":1,"a":2}', b"NaN", b"\xff", b"{", b"[" * 2000):
            with self.subTest(value=value[:20]), self.assertRaises(ValidationError):
                decode_json(value)
        with self.assertRaises(ValidationError):
            decode_json(b"{}", limit=1)

    def test_invalid_metadata_and_host_assemblies(self):
        mutations = [
            ("guid", "bad"),
            ("targetAbi", "11.0.0.0"),
            ("targetAbi", "12.99999.0.0"),
            ("repository", "https://evil.test/repo"),
            ("name", "a\n"),
            ("assemblyFile", "a.txt"),
            ("assemblyFile", "Jellyfin.Controller.dll"),
            ("packageFiles", [FIRST["assemblyFile"]]),
            ("packageFiles", ["System.Runtime.dll"]),
            ("packageFiles", ["a"] * 32),
        ]
        for key, value in mutations:
            info = copy.deepcopy(FIRST)
            info[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                metadata(info)
        with self.assertRaises(ValidationError):
            metadata({**FIRST, "unexpected": True})

    def test_timestamp_timezone_and_normalization(self):
        self.assertEqual(CREATED, timestamp("2026-09-08T14:00:00+02:00"))
        for value in ("yesterday", "2026-09-08T12:00:00", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                timestamp(value)

    def test_archive_entries_have_fixed_permissions_and_timestamp(self):
        with zipfile.ZipFile(io.BytesIO(archive_bytes({"x.dll": b"abc"}))) as archive:
            entry = archive.infolist()[0]
            self.assertEqual((1980, 1, 1, 0, 0, 0), entry.date_time)
            self.assertEqual(0o100644, entry.external_attr >> 16)


class PackageLimitTests(Workspace):
    def test_package_files_exactly_at_the_size_limit_are_read(self):
        source = self.root / "publish"
        source.mkdir()
        (source / FIRST["assemblyFile"]).write_bytes(b"x" * 6)
        (source / "settings.json").write_bytes(b"{}  ")
        info = {**FIRST, "packageFiles": ["settings.json"]}
        with patch("jellysin_tooling.package.MAX_ASSET", 10):
            self.assertEqual(sum(map(len, package_files(source, info).values())), 10)
        with patch("jellysin_tooling.package.MAX_ASSET", 9), self.assertRaisesRegex(ValidationError, "oversized"):
            package_files(source, info)

    def test_archive_exactly_at_the_size_limit_is_written(self):
        files = {FIRST["assemblyFile"]: b"assembly"}
        size = len(archive_bytes(files))
        with patch("jellysin_tooling.package.MAX_ASSET", size):
            self.assertEqual(len(archive_bytes(files)), size)
        with (
            patch("jellysin_tooling.package.MAX_ASSET", size - 1),
            self.assertRaisesRegex(ValidationError, "asset limit"),
        ):
            archive_bytes(files)
