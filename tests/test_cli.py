"""Exercise the public command interface with temporary workspaces."""

import contextlib
import io
import os
from unittest.mock import patch

from jellysin_tooling.cli import main, parser, run
from jellysin_tooling.common import ValidationError, json_bytes, read_json
from tests.fixtures import COMMIT, CREATED, FIRST, Workspace, approved
from tests.inventory_fixtures import restore_files


class CommandLineTests(Workspace):
    def test_package_interface_accepts_complete_inventory_and_rejects_partial_inputs(self):
        _, output = self.plugin()
        paths, _ = restore_files(self.root)
        descriptor = self.root / "plugin.json"
        descriptor.write_bytes(json_bytes(FIRST))
        version = self.root / "version.txt"
        version.write_text("1.0.0\n")
        destination = self.root / "inventory-dist"
        arguments = [
            "package",
            "--publish-directory",
            str(output.parent / "publish"),
            "--metadata-path",
            str(descriptor),
            "--version-path",
            str(version),
            "--output-directory",
            str(destination),
        ]
        options = ("--nuget-lock-path", "--nuget-assets-path", "--runtime-dependencies-path", "--global-json-path")
        for option, path in zip(options, paths, strict=True):
            arguments.extend([option, str(path)])
        with patch("jellysin_tooling.cli.command", side_effect=[COMMIT, CREATED]):
            run(parser().parse_args(arguments))
        self.assertEqual(2, len(read_json(destination / "release.json")["dependencyInventory"]["packages"]))
        args = parser().parse_args(arguments)
        args.nuget_assets_path = None
        with patch("jellysin_tooling.cli.command", side_effect=[COMMIT, CREATED]), self.assertRaises(ValidationError):
            run(args)

    def test_package_interface_and_github_outputs(self):
        _, output = self.plugin()
        descriptor = self.root / "plugin.json"
        descriptor.write_bytes(json_bytes(FIRST))
        version = self.root / "version.txt"
        version.write_text("1.0.0\n")
        github_output = self.root / "outputs"
        args = parser().parse_args(
            [
                "package",
                "--publish-directory",
                str(output.parent / "publish"),
                "--metadata-path",
                str(descriptor),
                "--version-path",
                str(version),
                "--output-directory",
                str(output),
            ]
        )
        with (
            patch("jellysin_tooling.cli.command", side_effect=[COMMIT, CREATED]),
            patch.dict(os.environ, {"GITHUB_OUTPUT": str(github_output)}),
        ):
            run(args)
        self.assertIn("archive=", github_output.read_text())
        args.tag = "v1.0.0"
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.cli.command", return_value=CREATED),
        ):
            run(args)

    def test_catalog_validation_accepts_empty_manifest_and_reviewed_allowlist(self):
        manifest = self.root / "manifest.json"
        manifest.write_text("[]\n")
        plugins = self.root / "plugins.json"
        plugins.write_bytes(json_bytes({"schemaVersion": 1, "plugins": [approved()]}))
        arguments = ["--manifest", str(manifest), "--allowlist", str(plugins)]
        run(parser().parse_args(["validate-catalog", *arguments]))
        with patch("jellysin_tooling.catalog.collect", return_value=[]):
            run(parser().parse_args(["catalog", *arguments]))

    def test_publish_interface_passes_parameters_without_shell_interpolation(self):
        with patch("jellysin_tooling.release.publish") as publish:
            run(parser().parse_args(["publish", "--repo", FIRST["repository"], "--tag", "v1.0.0"]))
        self.assertEqual(FIRST["repository"], publish.call_args.args[2])
        self.assertFalse(publish.call_args.kwargs["verified_tag_recovery"])
        with patch("jellysin_tooling.release.publish") as publish:
            run(
                parser().parse_args(
                    [
                        "publish",
                        "--repo",
                        FIRST["repository"],
                        "--tag",
                        "v1.0.0",
                        "--verified-tag-recovery",
                    ]
                )
            )
        self.assertTrue(publish.call_args.kwargs["verified_tag_recovery"])

    def test_invalid_input_exits_nonzero_without_dumping_contents(self):
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["tools.py", "validate-catalog", "--manifest", str(self.root / "missing")]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(1, caught.exception.code)
        self.assertNotIn(str(self.root), stderr.getvalue())

    def test_missing_required_arguments_refused_and_full_publish_accepted(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            parser().parse_args(["publish", "--repo", "o/r"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("the following arguments are required: --tag", stderr.getvalue())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            parser().parse_args(["catalog-check"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--fixture", stderr.getvalue())

        arguments = parser().parse_args(["publish", "--repo", "o/r", "--tag", "v1.0.0"])
        self.assertEqual(arguments.command, "publish")
        self.assertEqual(arguments.repo, "o/r")
        self.assertEqual(arguments.tag, "v1.0.0")
        self.assertEqual(arguments.directory, "dist")
        self.assertEqual(arguments.workflow, ".github/workflows/release.yml")
        self.assertEqual(arguments.signer_workflow, ".github/workflows/release.yml")
        self.assertFalse(arguments.verified_tag_recovery)
