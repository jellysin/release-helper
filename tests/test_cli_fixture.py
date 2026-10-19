"""The catalog-check command: a local fixture release meets the refusals the catalog path gives."""

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling.cli import parser
from jellysin_tooling.common import MAX_FILES, json_bytes
from tests.fixtures import FIRST, SECOND, FakeGitHub, Workspace, approved
from tests.test_tool_name import message

ROOT = Path(__file__).resolve().parents[1]
NOTICE = "jellysin-release-helper: attestation NOT checked; no manifest is written\n"
MISMATCH = "jellysin-release-helper: Release checksum manifest mismatch\n"


class CatalogCheckTests(Workspace):
    def setUp(self):
        super().setUp()
        self.release, self.fixture = self.plugin()
        self.allowlist = self.root / "plugins.json"
        self.allowlist.write_bytes(json_bytes({"schemaVersion": 1, "plugins": [approved(FIRST)]}))

    def check(self):
        """Exit status, standard output and standard error of one catalog-check run."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status, stderr = message(
                ["catalog-check", "--fixture", str(self.fixture), "--allowlist", str(self.allowlist)]
            )
        return status, stdout.getvalue(), stderr

    def tree(self):
        return {path: path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}

    def test_a_valid_fixture_passes_and_says_attestation_was_not_checked(self):
        before = self.tree()
        status, stdout, stderr = self.check()
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            stdout,
            NOTICE + f"{FIRST['repository']} v1.0.0: fixture passed the catalog checks; attestation NOT checked\n",
        )
        self.assertEqual(self.tree(), before)

    def test_planted_checksum_mismatch_gives_the_catalog_refusal_and_exit_status(self):
        (self.fixture / "checksums.txt").write_bytes(b"forged")
        before = self.tree()
        status, stdout, stderr = self.check()
        self.assertEqual((status, stdout, stderr), (1, NOTICE, MISMATCH))
        self.assertEqual(self.tree(), before)
        # The real path, fed the same bytes through the GitHub client, refuses with the same line.
        client = FakeGitHub()
        client.add(FIRST, self.release, self.fixture)
        manifest = self.root / "manifest.json"
        manifest.write_bytes(b"[]\n")
        with (
            patch("jellysin_tooling.cli.GitHub", return_value=client),
            patch("jellysin_tooling.github.command", return_value=""),
        ):
            real = message(["catalog", "--manifest", str(manifest), "--allowlist", str(self.allowlist)])
        self.assertEqual(real, (1, MISMATCH))
        self.assertEqual(manifest.read_bytes(), b"[]\n")

    def test_refusals_for_identity_layout_and_entry_kind(self):
        cases = {
            "Fixture plugin is not in the allowlist": lambda: self.allowlist.write_bytes(
                json_bytes({"schemaVersion": 1, "plugins": [approved(SECOND)]})
            ),
            "Release is missing metadata, checksums, or SBOM": lambda: (self.fixture / "release.json").unlink(),
            "Release is missing plugin ZIP": lambda: next(self.fixture.glob("*.zip")).unlink(),
            "Fixture entry is not a regular file": lambda: (self.fixture / "link").symlink_to(self.allowlist),
            "Incomplete or oversized asset": lambda: (self.fixture / "empty").write_bytes(b""),
            "Unsafe filename": lambda: (self.fixture / ".hidden").write_bytes(b"x"),
        }
        for expected, plant in cases.items():
            with self.subTest(expected=expected):
                self.setUp()
                plant()
                self.assertEqual(self.check(), (1, NOTICE, f"jellysin-release-helper: {expected}\n"))

    def test_file_count_boundary(self):
        present = len(list(self.fixture.iterdir()))
        for index in range(MAX_FILES - present):
            (self.fixture / f"extra-{index}").write_bytes(b"x")
        self.assertEqual(self.check()[0], 0)
        (self.fixture / "one-too-many").write_bytes(b"x")
        self.assertEqual(self.check(), (1, NOTICE, "jellysin-release-helper: Fixture has too many files\n"))

    def test_production_catalog_cannot_reach_the_fixture_path(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["catalog", "--fixture", str(self.fixture)])
        for action in sorted((ROOT / "actions").glob("*/action.yml")):
            with self.subTest(action=action.parent.name):
                self.assertNotIn("catalog-check", action.read_text(encoding="utf-8"))
        self.assertNotIn("fixture", (ROOT / "jellysin_tooling" / "catalog.py").read_text(encoding="utf-8"))
