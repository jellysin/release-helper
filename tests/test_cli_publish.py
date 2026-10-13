"""The publish command refuses bad input through tools.py main, before it reads git or the forge."""

from unittest.mock import patch

from tests.fixtures import FIRST, Workspace
from tests.test_tool_name import message

SHA = "a" * 40


class PublishCommandTests(Workspace):
    def publish(self, *extra, repo=FIRST["repository"], tag="v1.0.0", git=()):
        arguments = ["publish", "--directory", str(self.root), "--repo", repo, "--tag", tag, *extra]
        with (
            patch("jellysin_tooling.cli.GitHub") as client,
            patch("jellysin_tooling.release.command", side_effect=list(git)) as runner,
        ):
            result = message(arguments)
        return result, client, runner

    def assert_refused_before_any_call(self, outcome, reason):
        (status, stderr), client, runner = outcome
        self.assertEqual((status, stderr), (1, f"jellysin-release-helper: {reason}\n"))
        client.return_value.api.assert_not_called()
        runner.assert_not_called()

    def test_bad_repository_workflow_and_tag_are_refused_before_git_and_the_forge(self):
        cases = (
            ({"repo": "jellysin"}, (), "Invalid repository"),
            ({"repo": "jellysin/plugin lastfm"}, (), "Invalid repository"),
            ({}, ("--workflow", ".github/workflows/other.yml"), "Unapproved publisher workflow"),
            ({"tag": "1.0.0"}, (), "Release tag must start with v"),
            ({"tag": "v1.0"}, (), "Expected stable x.y.z version"),
            ({"tag": "v1.0.0-rc.1"}, (), "Expected stable x.y.z version"),
            ({"tag": "v65535.0.0"}, (), "Version exceeds assembly limit"),
        )
        for options, extra, reason in cases:
            with self.subTest(options=options, extra=extra):
                self.assert_refused_before_any_call(self.publish(*extra, **options), reason)

    def test_the_highest_assembly_version_reaches_the_exact_tag_check(self):
        (status, stderr), _, runner = self.publish(tag="v65534.0.0", git=[SHA, "b" * 40])
        self.assertEqual((status, stderr), (1, "jellysin-release-helper: Checkout does not match exact release tag\n"))
        self.assertEqual(runner.call_args_list[0].args[0], ["git", "rev-parse", "HEAD"])

    def test_verified_tag_recovery_is_refused_outside_github_actions(self):
        (status, stderr), client, _ = self.publish("--verified-tag-recovery", git=[SHA, SHA, ""])
        self.assertEqual(
            (status, stderr), (1, "jellysin-release-helper: Recovery requires a GitHub Actions workflow\n")
        )
        client.return_value.api.assert_not_called()
