"""Exercise the package-source action's CLI and publication validators with a real git tag."""

import subprocess  # nosec B404
import sys
import tarfile
from pathlib import Path

from jellysin_tooling import source
from jellysin_tooling.common import ValidationError, decode_json
from tests import test_source as source_fixtures
from tests.fixtures import Workspace

TOOL = Path(__file__).resolve().parents[2] / "tools.py"


class SourceDotRepositoryTests(Workspace):
    """Package a tagged dot repository and validate the resulting local release artifacts."""

    def setUp(self):
        """Reuse the existing workspace and tagged source-repository fixture."""
        super().setUp()
        self.repo_dir, self.commit, self.tag = source_fixtures.SourceReleaseTests.make_git_repo(self)
        self.output = self.root / "dist"

    def package_source(self, repo):
        """Run the exact package-source entry point used by the composite action."""
        return subprocess.run(  # nosec B603
            [
                sys.executable,
                str(TOOL),
                "package-source",
                "--repo",
                repo,
                "--tag",
                self.tag,
                "--output-directory",
                str(self.output),
            ],
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_dot_repository_packages_and_validates_for_publication(self):
        """Preserve repository identity while emitting and validating a safe archive prefix."""
        result = self.package_source("jellysin/.github")
        self.assertEqual(0, result.returncode, result.stderr)
        archive = self.output / "github-1.0.0.tar.gz"
        self.assertTrue(archive.is_file())
        release = decode_json((self.output / "release.json").read_bytes())
        self.assertEqual("jellysin/.github", release["repository"])
        self.assertEqual(self.commit, release["commit"])
        self.assertEqual("github-1.0.0.tar.gz", release["archive"]["name"])
        with tarfile.open(archive, "r:gz") as tar:
            entries = tar.getmembers()
            self.assertTrue(any(entry.isdir() and entry.name == "github-1.0.0" for entry in entries))
            self.assertTrue(
                all(entry.name == "github-1.0.0" or entry.name.startswith("github-1.0.0/") for entry in entries)
            )
        self.assertEqual(release, source.validate_release(release, "jellysin/.github", self.tag))
        files = source.local_artifacts(self.output, release, cwd=self.repo_dir)
        self.assertEqual({archive.name, "release.json", "sbom.spdx.json", "checksums.txt"}, files.keys())

    def test_double_dot_archive_stem_refused_without_writes(self):
        """Refuse a valid repository identity whose archive stem contains consecutive dots."""
        result = self.package_source("jellysin/.a..b")
        self.assertEqual(1, result.returncode)
        self.assertIn("Unsafe filename", result.stderr)
        self.assertFalse(self.output.exists())

    def test_packaged_release_with_tampered_checksums_refused(self):
        """Reject a planted checksum manifest change through publication's local validator."""
        result = self.package_source("jellysin/.github")
        self.assertEqual(0, result.returncode, result.stderr)
        release = decode_json((self.output / "release.json").read_bytes())
        (self.output / "checksums.txt").write_bytes(b"planted invalid checksum manifest\n")
        with self.assertRaisesRegex(ValidationError, "Local release checksum manifest mismatch"):
            source.local_artifacts(self.output, release, cwd=self.repo_dir)
