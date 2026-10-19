"""Tests for source release packaging, validation, member safety, and publication."""

import copy
import gzip
import io
import os
import tarfile
from unittest.mock import Mock, patch

from jellysin_tooling import source
from jellysin_tooling.cli import parser, run
from jellysin_tooling.common import (
    MAX_ASSET,
    MAX_FILES,
    ValidationError,
    decode_json,
    digest,
    json_bytes,
)
from jellysin_tooling.github import command
from jellysin_tooling.release import publish
from tests.fixtures import COMMIT, CREATED, FakeGitHub, Workspace


def make_tar_gz(members_dict, prefix="test-1.0.0"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        # Root directory entry
        dir_entry = tarfile.TarInfo(name=prefix)
        dir_entry.type = tarfile.DIRTYPE
        dir_entry.mode = 0o755
        tar.addfile(dir_entry)
        for name, info in members_dict.items():
            full_name = f"{prefix}/{name}"
            entry = tarfile.TarInfo(name=full_name)
            if isinstance(info, bytes):
                entry.size = len(info)
                entry.type = tarfile.REGTYPE
                entry.mode = 0o644
                tar.addfile(entry, io.BytesIO(info))
            elif isinstance(info, str):
                data = info.encode("utf-8")
                entry.size = len(data)
                entry.type = tarfile.REGTYPE
                entry.mode = 0o644
                tar.addfile(entry, io.BytesIO(data))
            elif isinstance(info, dict) and info.get("type") == "symlink":
                entry.type = tarfile.SYMTYPE
                entry.linkname = info.get("target", "target")
                tar.addfile(entry)
            elif isinstance(info, dict) and info.get("type") == "dir":
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
                tar.addfile(entry)
    return buffer.getvalue()


class SourceReleaseTests(Workspace):
    def setUp(self):
        super().setUp()
        self.current_commit = COMMIT
        patcher = patch(
            "jellysin_tooling.release.remote_tag",
            side_effect=lambda client, repo, tag: self.current_commit,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_git_repo(self, files=None):
        repo_dir = self.root / "source-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        command(["git", "init", "-b", "main"], cwd=str(repo_dir))
        command(["git", "config", "user.name", "Test User"], cwd=str(repo_dir))
        command(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir))
        if files is None:
            files = {"README.md": "# Source Repo\n", "main.py": "print('hello')\n"}
        for rel_path, content in files.items():
            target = repo_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                target.write_text(content, encoding="utf-8")
            else:
                target.write_bytes(content)
        command(["git", "add", "."], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir))
        commit = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))
        self.current_commit = commit
        tag = "v1.0.0"
        command(["git", "tag", "-a", tag, "-m", tag], cwd=str(repo_dir))
        return repo_dir, commit, tag

    def test_build_twice_on_temp_git_repo_produces_identical_bytes(self):
        repo_dir, commit, tag = self.make_git_repo()
        out1 = self.root / "dist1"
        out2 = self.root / "dist2"
        rel1 = source.build("jellysin/source-repo", tag, commit, CREATED, out1, cwd=str(repo_dir))
        rel2 = source.build("jellysin/source-repo", tag, commit, CREATED, out2, cwd=str(repo_dir))
        self.assertEqual(rel1, rel2)
        for name in ("source-repo-1.0.0.tar.gz", "release.json", "sbom.spdx.json", "checksums.txt"):
            self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes())

    def test_publish_source_uploads_four_assets_and_patches_draft_false(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        remote["assets"].clear()
        writes = []

        def runner(arguments):
            writes.append(arguments)
            if "POST" in arguments:
                name = arguments[4].split("name=")[1]
                asset_id = len(remote["assets"]) + 1
                data = (output / name).read_bytes()
                url = f"https://github.com/{repo}/releases/download/untagged-fixture-1/{name}"
                remote["assets"].append(
                    {
                        "id": asset_id,
                        "url": f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}",
                        "name": name,
                        "state": "uploaded",
                        "size": len(data),
                        "browser_download_url": url,
                    }
                )
                client.assets[repo, asset_id] = data
                client.downloads[url] = data
            elif "PATCH" in arguments:
                client.publish(remote)

        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=(output / "source-repo-1.0.0.tar.gz").read_bytes(),
            ),
        ):
            result = publish(
                client,
                output,
                repo,
                tag,
                ".github/workflows/release.yml",
                Mock(),
                runner,
                kind="source",
            )
        self.assertEqual(release, result)
        self.assertEqual(5, len(writes))
        self.assertEqual(["POST", "POST", "POST", "POST", "PATCH"], [w[3] for w in writes])
        self.assertIn("draft=false", writes[4])
        self.assertFalse(remote["draft"])
        self.assertTrue(remote["immutable"])

    def test_source_release_without_assets_stays_draft(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        client = FakeGitHub()
        remote = client.add({"repository": repo}, release, output, draft=True)
        remote["assets"].clear()
        (output / release["archive"]["name"]).unlink()
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.release.remote_tag", return_value=commit),
            self.assertRaisesRegex(ValidationError, "Missing or unsafe release artifact"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()
        self.assertTrue(remote["draft"])
        self.assertFalse(remote["immutable"])

    def test_substituted_tarball_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        tar_name = release["archive"]["name"]
        (output / tar_name).write_bytes(gzip.compress(b"substituted content", compresslevel=9, mtime=0))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.source.archive_bytes", return_value=b"original-archive-bytes"),
            self.assertRaisesRegex(ValidationError, "Archive checksum mismatch|Local archive differs"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_extra_or_missing_tar_member_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        prefix = f"source-repo-{release['semver']}"
        tampered_archive = make_tar_gz({"extra.txt": b"unexpected"}, prefix=prefix)
        tar_name = release["archive"]["name"]
        (output / tar_name).write_bytes(tampered_archive)
        release["archive"]["size"] = len(tampered_archive)
        release["archive"]["sha256"] = digest(tampered_archive)
        (output / "release.json").write_bytes(json_bytes(release))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.source.archive_bytes", return_value=b"different-original-bytes"),
            self.assertRaises(ValidationError),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_symlink_member_refused_before_upload(self):
        prefix = "source-repo-1.0.0"
        tar_bytes = make_tar_gz({"link": {"type": "symlink", "target": "README.md"}}, prefix=prefix)
        with self.assertRaisesRegex(ValidationError, "Unsupported archive member type"):
            source.members(tar_bytes, prefix=prefix)

    def test_dotdot_member_refused_before_upload(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            entry = tarfile.TarInfo(name="source-repo-1.0.0/../escape.txt")
            entry.size = 4
            entry.type = tarfile.REGTYPE
            tar.addfile(entry, io.BytesIO(b"data"))
        with self.assertRaisesRegex(ValidationError, "Unsafe archive path"):
            source.members(buffer.getvalue(), prefix="source-repo-1.0.0")

    def test_tampered_sbom_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        sbom = decode_json((output / "sbom.spdx.json").read_bytes())
        sbom["packages"][0]["versionInfo"] = "9.9.9"
        (output / "sbom.spdx.json").write_bytes(json_bytes(sbom))
        # Update checksums
        lines = "".join(
            f"{digest(p.read_bytes())}  {p.name}\n" for p in sorted(output.iterdir()) if p.name != "checksums.txt"
        )
        (output / "checksums.txt").write_bytes(lines.encode())
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes", return_value=(output / release["archive"]["name"]).read_bytes()
            ),
            self.assertRaisesRegex(ValidationError, "SBOM differs"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_tampered_checksums_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        (output / "checksums.txt").write_bytes(b"tampered checksums\n")
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch(
                "jellysin_tooling.source.archive_bytes", return_value=(output / release["archive"]["name"]).read_bytes()
            ),
            self.assertRaisesRegex(ValidationError, "checksum manifest mismatch"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_release_json_extra_key_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        release["extraKey"] = "not allowed"
        (output / "release.json").write_bytes(json_bytes(release))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            self.assertRaisesRegex(ValidationError, "Invalid release schema"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_wrong_url_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        release["archive"]["url"] = f"https://github.com/evil/repo/releases/download/{tag}/source-repo-1.0.0.tar.gz"
        (output / "release.json").write_bytes(json_bytes(release))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            self.assertRaisesRegex(ValidationError, "Archive URL is outside approved release"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_wrong_tag_refused_before_upload(self):
        repo_dir, commit, tag = self.make_git_repo()
        repo = "jellysin/source-repo"
        output = self.root / "dist"
        release = source.build(repo, tag, commit, CREATED, output, cwd=str(repo_dir))
        release["tag"] = "v2.0.0"
        (output / "release.json").write_bytes(json_bytes(release))
        client = FakeGitHub()
        client.add({"repository": repo}, release, output, draft=True)
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            self.assertRaisesRegex(ValidationError, "Release version mismatch"),
        ):
            publish(client, output, repo, tag, ".github/workflows/release.yml", Mock(), runner, kind="source")
        runner.assert_not_called()

    def test_max_asset_boundary(self):
        small_limit = 50
        with patch("jellysin_tooling.source.MAX_ASSET", small_limit):
            data_ok = b"x" * small_limit
            with self.assertRaisesRegex(ValidationError, "Invalid source archive"):
                source.members(data_ok)
            data_exceeds = b"x" * (small_limit + 1)
            with self.assertRaisesRegex(ValidationError, "Invalid archive data"):
                source.members(data_exceeds)

    def test_source_member_bound_is_its_own(self):
        # The plugin ZIP bound (MAX_FILES) must not cap a source tree.
        self.assertGreater(source.MAX_SOURCE_MEMBERS, MAX_FILES)
        ok = make_tar_gz({f"f{i}": b"" for i in range(source.MAX_SOURCE_MEMBERS - 1)}, prefix="big-1.0.0")
        self.assertEqual(source.MAX_SOURCE_MEMBERS - 1, len(source.members(ok, prefix="big-1.0.0")))
        over = make_tar_gz({f"f{i}": b"" for i in range(source.MAX_SOURCE_MEMBERS)}, prefix="big-1.0.0")
        with self.assertRaisesRegex(ValidationError, "Too many archive members"):
            source.members(over, prefix="big-1.0.0")

    def test_this_repository_tree_fits_the_source_archive(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = command(["git", "rev-parse", "HEAD"], cwd=root)
        extracted = source.members(source.archive_bytes(head, "release-helper-0.0.0", cwd=root), "release-helper-0.0.0")
        self.assertGreater(len(extracted), MAX_FILES)

    def test_package_source_cli(self):
        repo_dir, commit, tag = self.make_git_repo()
        output = self.root / "cli-dist"
        github_output = self.root / "github_output.txt"
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=commit),
            patch("jellysin_tooling.cli.command", return_value=CREATED),
            patch(
                "jellysin_tooling.source.archive_bytes",
                return_value=make_tar_gz({"f.txt": b"a"}, prefix="source-repo-1.0.0"),
            ),
            patch.dict(os.environ, {"GITHUB_OUTPUT": str(github_output)}),
        ):
            run(
                parser().parse_args(
                    [
                        "package-source",
                        "--repo",
                        "jellysin/source-repo",
                        "--tag",
                        tag,
                        "--output-directory",
                        str(output),
                    ]
                )
            )
        self.assertTrue((output / "release.json").is_file())
        self.assertIn("archive=", github_output.read_text(encoding="utf-8"))
        self.assertIn("directory=", github_output.read_text(encoding="utf-8"))

    def test_archive_bytes_validates_commit_and_prefix(self):
        with self.assertRaisesRegex(ValidationError, "Expected full lowercase commit SHA"):
            source.archive_bytes("invalid", "prefix")
        with self.assertRaisesRegex(ValidationError, "Invalid archive prefix"):
            source.archive_bytes(COMMIT, "/unsafe")

    def test_members_validates_path_safety(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            entry = tarfile.TarInfo(name="/absolute/path.txt")
            entry.size = 3
            tar.addfile(entry, io.BytesIO(b"abc"))
        with self.assertRaisesRegex(ValidationError, "Unsafe archive path"):
            source.members(buffer.getvalue(), prefix="prefix")

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            entry = tarfile.TarInfo(name="prefix/win\\slash.txt")
            entry.size = 3
            tar.addfile(entry, io.BytesIO(b"abc"))
        with self.assertRaisesRegex(ValidationError, "Unsafe archive path"):
            source.members(buffer.getvalue(), prefix="prefix")

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            entry = tarfile.TarInfo(name="other-prefix/file.txt")
            entry.size = 3
            tar.addfile(entry, io.BytesIO(b"abc"))
        with self.assertRaisesRegex(ValidationError, "Archive member outside prefix"):
            source.members(buffer.getvalue(), prefix="my-prefix")

    def test_build_refuses_replacing_different_bytes(self):
        repo_dir, commit, tag = self.make_git_repo()
        output = self.root / "replace-dist"
        source.build("jellysin/source-repo", tag, commit, CREATED, output, cwd=str(repo_dir))
        (output / "checksums.txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValidationError, "Refusing to replace different release bytes"):
            source.build("jellysin/source-repo", tag, commit, CREATED, output, cwd=str(repo_dir))

    def test_validate_release_checks_all_fields(self):
        valid = {
            "schemaVersion": 1,
            "kind": "source",
            "repository": "jellysin/source-repo",
            "semver": "1.0.0",
            "tag": "v1.0.0",
            "commit": COMMIT,
            "timestamp": CREATED,
            "archive": {
                "name": "source-repo-1.0.0.tar.gz",
                "size": 100,
                "sha256": "a" * 64,
                "url": "https://github.com/jellysin/source-repo/releases/download/v1.0.0/source-repo-1.0.0.tar.gz",
            },
        }
        source.validate_release(copy.deepcopy(valid), "jellysin/source-repo", "v1.0.0")

        for key in ("schemaVersion", "kind", "repository", "semver", "tag", "commit", "timestamp", "archive"):
            mutated = copy.deepcopy(valid)
            del mutated[key]
            with self.subTest(missing=key), self.assertRaises(ValidationError):
                source.validate_release(mutated, "jellysin/source-repo", "v1.0.0")

        for mutation, val in (
            ("schemaVersion", 2),
            ("kind", "plugin"),
            ("repository", "other/repo"),
            ("semver", "invalid"),
            ("tag", "v2.0.0"),
            ("commit", "not-sha"),
            ("timestamp", "invalid-ts"),
        ):
            mutated = copy.deepcopy(valid)
            mutated[mutation] = val
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                source.validate_release(mutated, "jellysin/source-repo", "v1.0.0")

        for archive_mutation, val in (
            ("size", 0),
            ("size", MAX_ASSET + 1),
            ("sha256", "tooshort"),
            ("sha256", "z" * 64),
            ("name", "wrong-name.tar.gz"),
        ):
            mutated = copy.deepcopy(valid)
            mutated["archive"][archive_mutation] = val
            with self.subTest(archive_mutation=archive_mutation), self.assertRaises(ValidationError):
                source.validate_release(mutated, "jellysin/source-repo", "v1.0.0")

    def test_members_infers_prefix_when_none_provided(self):
        archive = make_tar_gz({"hello.txt": b"world"}, prefix="inferred-prefix-1.0")
        files = source.members(archive)
        self.assertEqual({"hello.txt": b"world"}, files)

    def test_members_handles_subdirectories(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            dir_entry = tarfile.TarInfo(name="myprefix/subdir")
            dir_entry.type = tarfile.DIRTYPE
            tar.addfile(dir_entry)
            file_entry = tarfile.TarInfo(name="myprefix/subdir/file.txt")
            file_entry.size = 5
            file_entry.type = tarfile.REGTYPE
            tar.addfile(file_entry, io.BytesIO(b"hello"))
        files = source.members(buffer.getvalue(), prefix="myprefix")
        self.assertEqual({"subdir/file.txt": b"hello"}, files)
