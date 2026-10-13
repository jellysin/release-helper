"""Release notes: one changelog section, bounded, deterministic, and never an arbitrary link."""

import copy
from unittest.mock import patch

from jellysin_tooling.catalog import merge_manifest, validate_manifest, validate_release
from jellysin_tooling.changelog import bounded, notes, section
from jellysin_tooling.cli import parser, run
from jellysin_tooling.common import MAX_NOTES, ValidationError, json_bytes, read_json, release_notes
from jellysin_tooling.package import build
from tests.fixtures import COMMIT, CREATED, FIRST, Workspace, approved

REPO = FIRST["repository"]
DOCUMENT = """# Changelog

## [1.1.0](https://github.com/jellysin/plugin-lastfm/compare/v1.0.0...v1.1.0) (2026-10-11)


### Features

* scrobble queue survives a restart ([abc1234](https://github.com/jellysin/plugin-lastfm/commit/abc1234))   


### Bug Fixes

* retry after a rate limit

## 1.0.0 (2026-09-08)

### Features

* first release
"""


class SectionTests(Workspace):
    def write(self, contents):
        path = self.root / "CHANGELOG.md"
        path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
        return path

    def test_cuts_the_section_of_one_version_in_both_heading_forms(self):
        path = self.write(DOCUMENT)
        expected = (
            "### Features\n\n* scrobble queue survives a restart "
            "([abc1234](https://github.com/jellysin/plugin-lastfm/commit/abc1234))\n\n"
            "### Bug Fixes\n\n* retry after a rate limit"
        )
        self.assertEqual(expected, notes(path, "1.1.0", REPO))
        self.assertEqual("### Features\n\n* first release", notes(path, "1.0.0", REPO))

    def test_line_endings_do_not_change_the_result(self):
        first = notes(self.write(DOCUMENT), "1.1.0", REPO)
        self.assertEqual(first, notes(self.write(DOCUMENT.replace("\n", "\r\n")), "1.1.0", REPO))

    def test_absent_empty_or_similar_sections_yield_nothing(self):
        path = self.write(DOCUMENT + "\n## 0.9.0\n\n## Unreleased\n\n* pending\n")
        for version in ("2.0.0", "1.1.1", "0.9.0", "11.1.0"):
            with self.subTest(version=version):
                self.assertIsNone(notes(path, version, REPO))
        self.assertIsNone(section("## 1.1.0-beta\n\n* preview\n", "1.1.0"))

    def test_long_sections_are_cut_at_a_line_and_marked(self):
        lines = [f"* change number {index} with a description of ordinary length" for index in range(200)]
        path = self.write("## 1.0.0\n\n" + "\n".join(lines) + "\n")
        result = notes(path, "1.0.0", REPO)
        marker = "[Full changelog](https://github.com/jellysin/plugin-lastfm/releases/tag/v1.0.0)"
        self.assertLessEqual(len(result), MAX_NOTES)
        self.assertEqual(["", marker], result.split("\n")[-2:])
        kept = result.split("\n")[:-2]
        self.assertEqual(lines[: len(kept)], kept)
        self.assertGreater(len("\n".join(lines[: len(kept) + 1])) + len(marker) + 2, MAX_NOTES)
        self.assertEqual(result, notes(path, "1.0.0", REPO))
        self.assertEqual(marker, notes(self.write("## 1.0.0\n\n" + "x" * 5000 + "\n"), "1.0.0", REPO))

    def test_unsafe_or_oversized_changelogs_fail(self):
        cases = (
            b"## 1.0.0\n\n* tab\there\n",
            b"## 1.0.0\n\n* escape \x1b[31m\n",
            "## 1.0.0\n\n* hidden ‮ order\n".encode(),
            b"## 1.0.0\n\n* \xff\n",
        )
        for contents in cases:
            with self.subTest(contents=contents), self.assertRaises(ValidationError):
                notes(self.write(contents), "1.0.0", REPO)
        with patch("jellysin_tooling.changelog.MAX_JSON", 16), self.assertRaisesRegex(ValidationError, "size limit"):
            notes(self.write(DOCUMENT), "1.0.0", REPO)
        for version, repo in (("v1.0.0", REPO), ("1.0.0", "https://evil.test/repo")):
            with self.subTest(version=version, repo=repo), self.assertRaises(ValidationError):
                notes(self.write(DOCUMENT), version, repo)

    def test_notes_are_bounded_text(self):
        self.assertEqual("### Features\n\n* one", release_notes("### Features\n\n* one"))
        for value in ("", " padded", "trailing\n", "x" * (MAX_NOTES + 1), "carriage\rreturn", "delete\x7f", None, 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                release_notes(value)


class CatalogNotesTests(Workspace):
    def release(self, text=None, version="1.0.0"):
        source = self.root / "publish"
        source.mkdir(exist_ok=True)
        (source / FIRST["assemblyFile"]).write_bytes(b"assembly")
        return build(source, FIRST, version, COMMIT, CREATED, self.root / version, None, text)

    def test_attested_notes_reach_the_catalog_and_older_entries_keep_their_link(self):
        linked = merge_manifest([], [self.release()])
        before = json_bytes(linked[0]["versions"][0])
        merged = merge_manifest(linked, [self.release("### Features\n\n* one", "1.1.0")])
        self.assertEqual("### Features\n\n* one", merged[0]["versions"][0]["changelog"])
        self.assertEqual(
            "https://github.com/jellysin/plugin-lastfm/releases/tag/v1.0.0", linked[0]["versions"][0]["changelog"]
        )
        self.assertEqual(before, json_bytes(merged[0]["versions"][1]))
        self.assertNotIn("changelog", self.release())

    def test_release_metadata_with_unsafe_notes_is_refused(self):
        release = self.release("### Features\n\n* one")
        validate_release(release, approved(), "v1.0.0")
        for value in ("", "x" * (MAX_NOTES + 1), "bell\x07", None, ["* one"]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_release({**release, "changelog": value}, approved(), "v1.0.0")
        with self.assertRaises(ValidationError):
            self.release("escape \x1b[0m")

    def test_catalog_changelog_is_notes_or_the_exact_release_link(self):
        manifest = merge_manifest([], [self.release()])
        cases = (
            "https://evil.test/notes",
            "https://github.com/jellysin/plugin-lastfm/releases/tag/v9.9.9",
            "https://github.com/jellysin/cinema/releases/tag/v1.0.0",
            "javascript://%0aalert(1)",
            "",
            "x" * (MAX_NOTES + 1),
            "bell\x07",
            None,
        )
        for value in cases:
            changed = copy.deepcopy(manifest)
            changed[0]["versions"][0]["changelog"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_manifest(changed)
        changed = copy.deepcopy(manifest)
        changed[0]["versions"][0]["changelog"] = "### Bug Fixes\n\n* see https://example.test/advisory"
        validate_manifest(changed)


class CommandLineNotesTests(Workspace):
    def arguments(self, changelog, output):
        _, built = self.plugin()
        descriptor = self.root / "plugin.json"
        descriptor.write_bytes(json_bytes(FIRST))
        version = self.root / "version.txt"
        version.write_text("1.0.0\n")
        path = self.root / "CHANGELOG.md"
        path.write_text(changelog)
        values = {
            "--publish-directory": built.parent / "publish",
            "--metadata-path": descriptor,
            "--version-path": version,
            "--output-directory": self.root / output,
            "--changelog-path": path,
        }
        return parser().parse_args(["package", *(str(item) for pair in values.items() for item in pair)])

    def test_tagged_package_carries_its_changelog_section(self):
        args = self.arguments(DOCUMENT, "tagged")
        args.tag = "v1.0.0"
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.cli.command", return_value=CREATED),
        ):
            run(args)
        self.assertEqual(
            "### Features\n\n* first release", read_json(self.root / "tagged" / "release.json")["changelog"]
        )

    def test_tagged_package_without_its_section_fails_and_writes_nothing(self):
        args = self.arguments("# Changelog\n\n## Unreleased\n\n* pending\n", "missing")
        args.tag = "v1.0.0"
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.cli.command", return_value=CREATED),
            self.assertRaisesRegex(ValidationError, "no section"),
        ):
            run(args)
        self.assertFalse((self.root / "missing").exists())

    def test_untagged_check_before_the_release_pull_request_omits_notes(self):
        args = self.arguments("# Changelog\n\n## Unreleased\n\n* pending\n", "untagged")
        with patch("jellysin_tooling.cli.command", side_effect=[COMMIT, CREATED]):
            run(args)
        self.assertNotIn("changelog", read_json(self.root / "untagged" / "release.json"))

    def test_version_file_must_hold_a_stable_version(self):
        args = self.arguments(DOCUMENT, "invalid")
        (self.root / "version.txt").write_text("1.0.0-rc.1\n")
        with patch("jellysin_tooling.cli.command", side_effect=[COMMIT, CREATED]), self.assertRaises(ValidationError):
            run(args)


class BoundedNotesTests(Workspace):
    def test_a_cut_section_drops_the_blank_lines_before_the_link(self):
        link = "https://github.com/jellysin/plugin-lastfm/releases/tag/v1.0.0"
        lines = ["- first change", "", "", "x" * MAX_NOTES]
        self.assertEqual(bounded(lines, link), f"- first change\n\n[Full changelog]({link})")

    def test_a_section_exactly_at_the_limit_is_kept_whole(self):
        link = "https://github.com/jellysin/plugin-lastfm/releases/tag/v1.0.0"
        lines = ["x" * (MAX_NOTES - 2), "", ""]
        self.assertEqual(bounded(lines, link), "\n".join(lines))
        self.assertIn("[Full changelog]", bounded([*lines, "y"], link))
