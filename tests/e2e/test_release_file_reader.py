"""Exercise read_release_files end-to-end through plugin and source publication callers."""

from unittest.mock import Mock, patch

from jellysin_tooling import release, source
from jellysin_tooling.common import ValidationError
from tests import test_source as source_fixtures
from tests.fixtures import COMMIT, CREATED, FIRST, FakeGitHub, Workspace, write_bundles


class ReleaseFileReaderBase(Workspace):
    """Shared helper harness for intercepting uploads and publication across release kinds."""

    def make_runner(self):
        """Create a runner callback that records gh invocations and updates fake GitHub assets."""

        def runner(arguments):
            self.writes.append(arguments)
            if "POST" in arguments:
                name = arguments[4].split("name=")[1]
                asset_id = len(self.remote["assets"]) + 1
                data = (self.output / name).read_bytes()
                url = f"https://github.com/{self.repo}/releases/download/untagged-fixture-1/{name}"
                self.remote["assets"].append(
                    {
                        "id": asset_id,
                        "url": f"https://api.github.com/repos/{self.repo}/releases/assets/{asset_id}",
                        "name": name,
                        "state": "uploaded",
                        "size": len(data),
                        "browser_download_url": url,
                    }
                )
                self.client.assets[self.repo, asset_id] = data
                self.client.downloads[url] = data
            elif "PATCH" in arguments:
                self.client.publish(self.remote)

        return runner


class PluginReleaseFileReaderTests(ReleaseFileReaderBase):
    """Drive read_release_files through the plugin publication and artifact paths."""

    def setUp(self):
        """Build plugin artifacts and register a draft release on fake GitHub."""
        super().setUp()
        self.release, self.output = self.plugin()
        write_bundles(self.output)
        self.repo = FIRST["repository"]
        self.tag = self.release["tag"]
        self.client = FakeGitHub()
        self.remote = self.client.add(FIRST, self.release, self.output, draft=True)
        self.remote["assets"].clear()
        self.writes = []
        self.runner = self.make_runner()

    def read_artifacts(self):
        """Drive local_artifacts and read_release_files through release.plugin_artifacts."""
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.release.remote_tag", return_value=COMMIT),
        ):
            return release.plugin_artifacts(self.client, self.output, self.repo, self.tag, COMMIT)

    def publish(self):
        """Execute release.publish with kind='plugin' and recorded runner writes."""
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.release.remote_tag", return_value=COMMIT),
        ):
            return release.publish(
                self.client,
                self.output,
                self.repo,
                self.tag,
                ".github/workflows/release.yml",
                verifier=Mock(),
                runner=self.runner,
                kind="plugin",
            )

    def test_positive_artifacts_read_matching_disk_and_published(self):
        """Four expected artifacts are read matching disk bytes, extra files ignored, and release published."""
        extra_file = self.output / "notes.txt"
        extra_file.write_text("Release notes\n", encoding="utf-8")
        expected_names = {self.release["archive"]["name"], "release.json", "sbom.spdx.json", "checksums.txt"}
        files = self.read_artifacts()
        self.assertEqual(expected_names, files.keys())
        self.assertNotIn("notes.txt", files)
        for name in sorted(expected_names):
            self.assertEqual((self.output / name).read_bytes(), files[name])
        published = self.publish()
        self.assertEqual(self.release["tag"], published["tag"])
        self.assertFalse(self.remote["draft"])
        self.assertTrue(self.remote["immutable"])
        self.assertEqual(5, len(self.writes))

    def test_missing_artifact_refused_without_writes(self):
        """Refuse release publication and artifact reading when an expected artifact is missing."""
        archive_path = self.output / self.release["archive"]["name"]
        archive_path.unlink()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.read_artifacts()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.publish()
        self.assertEqual([], self.writes)
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])

    def test_symlink_artifact_refused_without_writes(self):
        """Refuse publication and artifact reading when an artifact is replaced by a symlink."""
        archive_path = self.output / self.release["archive"]["name"]
        archive_bytes = archive_path.read_bytes()
        archive_path.unlink()
        target = self.root / "external_archive.bin"
        target.write_bytes(archive_bytes)
        archive_path.symlink_to(target)
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.read_artifacts()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.publish()
        self.assertEqual([], self.writes)
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])

    def test_directory_artifact_refused_without_writes(self):
        """Refuse release publication when an artifact path is a directory instead of a regular file."""
        archive_path = self.output / self.release["archive"]["name"]
        archive_path.unlink()
        archive_path.mkdir()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.read_artifacts()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.publish()
        self.assertEqual([], self.writes)
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])

    def test_max_asset_size_boundary(self):
        """Accept release artifacts at MAX_ASSET size and reject size MAX_ASSET + 1.

        Patches jellysin_tooling.common.MAX_ASSET to the exact byte size of the
        largest artifact so boundary checks execute without writing 64 MiB files.
        """
        expected_names = {self.release["archive"]["name"], "release.json", "sbom.spdx.json", "checksums.txt"}
        max_size = max((self.output / name).stat().st_size for name in sorted(expected_names))
        with patch("jellysin_tooling.common.MAX_ASSET", max_size - 1):
            with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
                self.read_artifacts()
            with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
                self.publish()
            self.assertEqual([], self.writes)
            self.assertTrue(self.remote["draft"])
            self.assertFalse(self.remote["immutable"])

        with patch("jellysin_tooling.common.MAX_ASSET", max_size):
            files = self.read_artifacts()
            self.assertEqual(expected_names, files.keys())
            published = self.publish()
            self.assertEqual(self.release["tag"], published["tag"])
            self.assertFalse(self.remote["draft"])
            self.assertTrue(self.remote["immutable"])
            self.assertEqual(5, len(self.writes))


class SourceReleaseFileReaderTests(ReleaseFileReaderBase):
    """Drive read_release_files through the source publication and artifact paths."""

    def setUp(self):
        """Build source release artifacts from a tagged repository and register draft on fake GitHub."""
        super().setUp()
        self.repo_dir, self.commit, self.tag = source_fixtures.SourceReleaseTests.make_git_repo(self)
        self.repo = "jellysin/source-repo"
        self.output = self.root / "dist"
        self.release = source.build(self.repo, self.tag, self.commit, CREATED, self.output, cwd=str(self.repo_dir))
        self.archive_bytes = (self.output / self.release["archive"]["name"]).read_bytes()
        write_bundles(self.output)
        self.client = FakeGitHub()
        self.remote = self.client.add({"repository": self.repo}, self.release, self.output, draft=True)
        self.remote["assets"].clear()
        self.writes = []
        self.runner = self.make_runner()

    def read_artifacts(self):
        """Drive source.local_artifacts and read_release_files through release.source_artifacts."""
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=self.commit),
            patch("jellysin_tooling.release.remote_tag", return_value=self.commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=self.archive_bytes,
            ),
        ):
            return release.source_artifacts(self.client, self.output, self.repo, self.tag, self.commit)

    def publish(self):
        """Execute release.publish with kind='source' and recorded runner writes."""
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=self.commit),
            patch("jellysin_tooling.release.remote_tag", return_value=self.commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=self.archive_bytes,
            ),
        ):
            return release.publish(
                self.client,
                self.output,
                self.repo,
                self.tag,
                ".github/workflows/release.yml",
                verifier=Mock(),
                runner=self.runner,
                kind="source",
            )

    def test_positive_artifacts_read_matching_disk_and_published(self):
        """Four expected artifacts are read matching disk bytes, extra files ignored, and release published."""
        extra_file = self.output / "notes.txt"
        extra_file.write_text("Release notes\n", encoding="utf-8")
        expected_names = {self.release["archive"]["name"], "release.json", "sbom.spdx.json", "checksums.txt"}
        files = self.read_artifacts()
        self.assertEqual(expected_names, files.keys())
        self.assertNotIn("notes.txt", files)
        for name in sorted(expected_names):
            self.assertEqual((self.output / name).read_bytes(), files[name])
        published = self.publish()
        self.assertEqual(self.release["tag"], published["tag"])
        self.assertFalse(self.remote["draft"])
        self.assertTrue(self.remote["immutable"])
        self.assertEqual(5, len(self.writes))

    def test_missing_artifact_refused_without_writes(self):
        """Refuse release publication and artifact reading when an expected artifact is missing."""
        archive_path = self.output / self.release["archive"]["name"]
        archive_path.unlink()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.read_artifacts()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.publish()
        self.assertEqual([], self.writes)
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])

    def test_symlink_artifact_refused_without_writes(self):
        """Refuse publication and artifact reading when an artifact is replaced by a symlink."""
        archive_path = self.output / self.release["archive"]["name"]
        archive_bytes = archive_path.read_bytes()
        archive_path.unlink()
        target = self.root / "external_archive.bin"
        target.write_bytes(archive_bytes)
        archive_path.symlink_to(target)
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.read_artifacts()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.publish()
        self.assertEqual([], self.writes)
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])

    def test_directory_artifact_refused_without_writes(self):
        """Refuse release publication when an artifact path is a directory instead of a regular file."""
        archive_path = self.output / self.release["archive"]["name"]
        archive_path.unlink()
        archive_path.mkdir()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.read_artifacts()
        with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
            self.publish()
        self.assertEqual([], self.writes)
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])

    def test_max_asset_size_boundary(self):
        """Accept release artifacts at MAX_ASSET size and reject size MAX_ASSET + 1.

        Patches jellysin_tooling.common.MAX_ASSET to the exact byte size of the
        largest artifact so boundary checks execute without writing 64 MiB files.
        """
        expected_names = {self.release["archive"]["name"], "release.json", "sbom.spdx.json", "checksums.txt"}
        max_size = max((self.output / name).stat().st_size for name in sorted(expected_names))
        with patch("jellysin_tooling.common.MAX_ASSET", max_size - 1):
            with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
                self.read_artifacts()
            with self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"):
                self.publish()
            self.assertEqual([], self.writes)
            self.assertTrue(self.remote["draft"])
            self.assertFalse(self.remote["immutable"])

        with patch("jellysin_tooling.common.MAX_ASSET", max_size):
            files = self.read_artifacts()
            self.assertEqual(expected_names, files.keys())
            published = self.publish()
            self.assertEqual(self.release["tag"], published["tag"])
            self.assertFalse(self.remote["draft"])
            self.assertTrue(self.remote["immutable"])
            self.assertEqual(5, len(self.writes))
