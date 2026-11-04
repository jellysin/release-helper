"""Exercise the package-source action's CLI and publication validators with a real git tag."""

import io
import subprocess  # nosec B404
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling import source
from jellysin_tooling.common import ValidationError, decode_json, repository, require, semver
from tests import test_source as source_fixtures
from tests.fixtures import Workspace

TOOL = Path(__file__).resolve().parents[2] / "tools.py"


class SourcePackageFixture(Workspace):
    """Share CLI and archive checks with local fixtures that need no git tag."""

    def setUp(self):
        """Provide fixed CLI arguments and paths without starting a subprocess."""
        super().setUp()
        self.repo_dir = self.root
        self.tag = "v1.0.0"
        self.output = self.root / "dist"

    def package_source(self, repo):
        """Run the exact package-source entry point used by the composite action."""
        repository(repo)
        require(repo in {"jellysin/.github", "jellysin/.a..b"}, "Unexpected test repository")
        require(isinstance(self.tag, str) and self.tag.startswith("v"), "Release tag must start with v")
        semver(self.tag[1:])
        require(self.tag == "v1.0.0", "Unexpected test tag")
        # Fixed CLI, inputs validated against a fixed allow-list above, argument list without a shell.
        return subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
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

    def assert_archive_members(self, archive):
        """Require a safe relative archive tree under the expected top directory."""
        source.members(archive.read_bytes(), "github-1.0.0")
        with tarfile.open(archive, "r:gz") as tar:
            entries = tar.getmembers()
            self.assertTrue(any(entry.isdir() and entry.name == "github-1.0.0" for entry in entries))
            self.assertTrue(
                all(entry.name == "github-1.0.0" or entry.name.startswith("github-1.0.0/") for entry in entries)
            )


class SourcePackageValidationTests(SourcePackageFixture):
    """Exercise planted unsafe inputs independently of the git fixture."""

    def test_unsafe_archive_members_refused(self):
        """Reject traversal, absolute paths, and members outside the expected directory."""
        for member in ("github-1.0.0/../escape.txt", "/github-1.0.0/absolute.txt", "other-1.0.0/file.txt"):
            with self.subTest(member=member):
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w:gz") as tar:
                    root = tarfile.TarInfo("github-1.0.0")
                    root.type = tarfile.DIRTYPE
                    tar.addfile(root)
                    tar.addfile(tarfile.TarInfo(member))
                archive = self.root / "unsafe.tar.gz"
                archive.write_bytes(data.getvalue())
                with self.assertRaisesRegex(ValidationError, "Unsafe archive path|Archive member outside prefix"):
                    self.assert_archive_members(archive)

    def test_unsafe_package_inputs_refused_before_process(self):
        """Refuse unsafe or unexpected repository and tag values before launching a process."""
        for repo, tag in (
            ("jellysin/.github --help", "v1.0.0"),
            ("jellysin/.github\n", "v1.0.0"),
            ("other/repository", "v1.0.0"),
            ("jellysin/.github", "v1.0.0 --help"),
            ("jellysin/.github", "v1.0.0\n"),
            ("jellysin/.github", "1.0.0"),
            ("jellysin/.github", "v2.0.0"),
        ):
            with self.subTest(repo=repo, tag=tag), patch("subprocess.run") as run:
                self.tag = tag
                with self.assertRaises(ValidationError):
                    self.package_source(repo)
                run.assert_not_called()


class SourceDotRepositoryTests(SourcePackageFixture):
    """Package a tagged dot repository and validate the resulting local release artifacts."""

    def setUp(self):
        """Reuse the existing workspace and tagged source-repository fixture."""
        super().setUp()
        self.repo_dir, self.commit, self.tag = source_fixtures.SourceReleaseTests.make_git_repo(self)

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
        self.assert_archive_members(archive)
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
