"""Each composite action passes its inputs to tools.py as the documented options, and refuses what it must.

Recorded runs check the argument lists without running the tool; real runs check refusals that need no
network, Docker or GitHub.
"""

import unittest

from jellysin_tooling.common import json_bytes
from tests.action_steps import ROOT, SKIP_REASON, ActionRunner, bash
from tests.fixtures import approved

TOOLS = f"{ROOT}/tools.py"
REPOSITORY = "jellysin/plugin-lastfm"


@unittest.skipIf(bash() is None, SKIP_REASON)
class RecordedInputTests(unittest.TestCase):
    def setUp(self):
        self.runner = ActionRunner(record=True)
        self.addCleanup(self.runner.close)

    def argv(self, name, inputs, environment=None):
        """Run the action once and return the one python call it made and the token that call saw."""
        self.runner.records.unlink(missing_ok=True)
        result = self.runner.run(name, inputs, environment)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        (call,) = self.runner.calls()
        return call["argv"], call["GH_TOKEN"]

    def test_package_passes_all_eleven_inputs(self):
        inputs = {
            "publish-directory": "out/publish",
            "metadata-path": "meta/plugin.json",
            "version-path": "meta/version.txt",
            "output-directory": "artifacts",
            "tag": "v1.2.3",
            "nuget-lock-path": "src/packages.lock.json",
            "nuget-assets-path": "src/obj/project.assets.json",
            "runtime-dependencies-path": "runtime.json",
            "global-json-path": "sdk/global.json",
            "changelog-path": "CHANGELOG.md",
        }
        argv, _ = self.argv("package", inputs)
        self.assertEqual(
            argv,
            [TOOLS, "package", "--publish-directory", "out/publish", "--metadata-path", "meta/plugin.json"]
            + ["--version-path", "meta/version.txt", "--output-directory", "artifacts", "--tag", "v1.2.3"]
            + ["--nuget-lock-path", "src/packages.lock.json", "--nuget-assets-path", "src/obj/project.assets.json"]
            + ["--runtime-dependencies-path", "runtime.json", "--global-json-path", "sdk/global.json"]
            + ["--changelog-path", "CHANGELOG.md"],
        )

    def test_package_with_only_the_required_input_uses_the_defaults_and_omits_empty_options(self):
        argv, _ = self.argv("package", {"publish-directory": "publish"})
        self.assertEqual(
            argv,
            [TOOLS, "package", "--publish-directory", "publish", "--metadata-path", "plugin.json"]
            + ["--version-path", "version.txt", "--output-directory", "dist", "--global-json-path", "global.json"],
        )

    def test_an_input_with_shell_syntax_stays_one_argument(self):
        tag = 'v1.0.0; touch injected $(touch injected) "'
        argv, _ = self.argv("package", {"publish-directory": "publish", "tag": tag})
        self.assertEqual(argv[argv.index("--tag") + 1], tag)
        self.assertFalse((self.runner.work / "injected").exists())

    def test_publish_passes_its_inputs_and_the_token_only_through_the_environment(self):
        environment = {"GITHUB_REPOSITORY": REPOSITORY}
        inputs = {"directory": "artifacts", "tag": "v1.2.3", "token": "synthetic-token"}
        argv, token = self.argv("publish", inputs, environment)
        self.assertEqual(argv, [TOOLS, "publish", "--directory", "artifacts", "--tag", "v1.2.3", "--repo", REPOSITORY])
        self.assertEqual(token, "synthetic-token")
        self.assertNotIn("synthetic-token", argv)

    def test_publish_adds_recovery_only_for_true(self):
        environment = {"GITHUB_REPOSITORY": REPOSITORY}
        argv, _ = self.argv("publish", {"tag": "v1.2.3", "verified-tag-recovery": "true"}, environment)
        self.assertEqual(argv[-1], "--verified-tag-recovery")
        self.assertEqual(argv[2:4], ["--directory", "dist"])

    def test_publish_refuses_any_other_recovery_value_before_running_the_tool(self):
        for value in ("True", "yes", "1", "", "true "):
            with self.subTest(value=value):
                result = self.runner.run("publish", {"tag": "v1.2.3", "verified-tag-recovery": value})
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr.decode(), "Invalid verified-tag-recovery input\n")
        self.assertEqual(self.runner.calls(), [])

    def test_catalog_passes_its_files_and_the_token(self):
        argv, token = self.argv("catalog", {"token": "synthetic-token"})
        self.assertEqual(argv, [TOOLS, "catalog", "--manifest", "manifest.json", "--allowlist", "plugins.json"])
        self.assertEqual(token, "synthetic-token")
        argv, _ = self.argv(
            "catalog", {"manifest": "repo/manifest.json", "allowlist": "repo/plugins.json", "token": "t"}
        )
        self.assertEqual(argv[2:], ["--manifest", "repo/manifest.json", "--allowlist", "repo/plugins.json"])

    def test_prepare_passes_its_five_inputs(self):
        environment = {"GITHUB_REPOSITORY": REPOSITORY}
        inputs = {"tag": "v1.2.3", "commit": "a" * 40, "token": "synthetic-token"}
        argv, token = self.argv("prepare", inputs, environment)
        self.assertEqual(
            argv,
            [TOOLS, "prepare", "--repo", REPOSITORY, "--tag", "v1.2.3", "--commit", "a" * 40]
            + ["--version-path", "version.txt"],
        )
        self.assertEqual(token, "synthetic-token")
        argv, _ = self.argv(
            "prepare", {**inputs, "version-path": "v.txt", "changelog-path": "CHANGELOG.md"}, environment
        )
        self.assertEqual(argv[-4:], ["--version-path", "v.txt", "--changelog-path", "CHANGELOG.md"])

    def test_server_check_passes_the_image_only_when_given(self):
        argv, _ = self.argv("server-check", {"manifest": "manifest.json"})
        self.assertEqual(argv, [TOOLS, "server-check", "--manifest", "manifest.json"])
        image = "jellyfin/jellyfin:12.2@sha256:" + "b" * 64
        argv, _ = self.argv("server-check", {"manifest": "manifest.json", "image": image})
        self.assertEqual(argv[-2:], ["--image", image])


@unittest.skipIf(bash() is None, SKIP_REASON)
class RefusalTests(unittest.TestCase):
    """Real runs of the tool through the action step, on inputs it refuses before any network call."""

    def setUp(self):
        self.runner = ActionRunner()
        self.addCleanup(self.runner.close)

    def assert_refused(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stdout.decode())
        self.assertIn(f"jellysin-release-helper: {reason}", result.stderr.decode())

    def test_server_check_refuses_an_unreadable_manifest_through_the_action(self):
        (self.runner.work / "manifest.json").write_text("[1]\n", encoding="utf-8")
        self.assert_refused(self.runner.run("server-check", {"manifest": "manifest.json"}), "Invalid manifest plugin")

    def test_check_refuses_a_directory_without_the_repository_documents(self):
        (self.runner.work / "empty").mkdir()
        result = self.runner.run("check", {"directory": "empty"})
        self.assert_refused(result, "Missing required repository document: LICENSE")
        self.assert_refused(self.runner.run("check", {"directory": "missing"}), "Missing required repository document")

    def test_check_reads_the_directory_it_is_given(self):
        repository = self.runner.work / "repository"
        repository.mkdir()
        for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md"):
            (repository / name).write_text("fixture\n", encoding="utf-8")
        (repository / "renovate.json").write_text("{}\n", encoding="utf-8")
        (repository / "plugins.json").write_bytes(json_bytes({"schemaVersion": 1, "plugins": [approved()]}))
        (repository / "manifest.json").write_text("[]\n", encoding="utf-8")
        for directory in ("repository", "repository/"):
            with self.subTest(directory=directory):
                result = self.runner.run("check", {"directory": directory, "catalog": "true"})
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_package_refuses_a_missing_publish_directory(self):
        result = self.runner.run("package", {"publish-directory": "missing"})
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.decode().startswith("jellysin-release-helper: "), result.stderr.decode())


class WorkflowWiringTests(unittest.TestCase):
    def test_ci_runs_the_package_and_check_actions_on_the_committed_fixtures(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "        uses: ./actions/package\n        with:\n          publish-directory: tests/data/plugin/publish\n",
            ci,
        )
        self.assertIn(
            '          for file in release.json checksums.txt sbom.spdx.json; do test -s "$DIRECTORY/$file"; done\n', ci
        )
        self.assertIn(
            "        uses: ./actions/check\n        with:\n          directory: tests/data/repository\n"
            "          catalog: 'true'\n",
            ci,
        )
        for path in ("plugin/plugin.json", "plugin/version.txt", "repository/manifest.json", "repository/plugins.json"):
            self.assertTrue((ROOT / "tests" / "data" / path).is_file(), path)
