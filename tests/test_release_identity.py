"""Draft discovery uses authenticated pagination and pins every operation to its ID."""

import copy
import unittest
from unittest.mock import Mock, patch

from jellysin_tooling.common import ValidationError
from jellysin_tooling.release import (
    _ReleaseContext,
    find_release,
    local_artifacts,
    publish,
    read_release,
    release_identity,
    remove_draft_starters,
    upload_missing,
)
from tests.fixtures import COMMIT, FIRST, FakeGitHub, Workspace

REPO = FIRST["repository"]
TAG = "v1.0.0"


def remote_release(identity=1):
    return {
        "id": identity,
        "url": f"https://api.github.com/repos/{REPO}/releases/{identity}",
        "tag_name": TAG,
        "prerelease": False,
        "draft": True,
    }


class ReleaseLookupTests(unittest.TestCase):
    def test_lookup_reaches_second_page_and_checks_uniqueness_after_first_page_match(self):
        expected = remote_release()
        for pages in ([100 * [{"tag_name": "v0.1.0"}], [expected]], [[expected] + 99 * [{"tag_name": "v0.1.0"}], []]):
            with self.subTest(pages=len(pages)):
                client = Mock(token="fixture", api=Mock(side_effect=pages))
                self.assertEqual(expected, find_release(client, REPO, TAG))
                self.assertEqual(
                    [f"repos/{REPO}/releases?per_page=100&page={page}" for page in (1, 2)],
                    [call.args[0] for call in client.api.call_args_list],
                )

    def test_missing_duplicate_or_over_bound_lookup_fails_closed(self):
        cases = (
            ([[]], "No visible"),
            ([[remote_release()] + 99 * [{"tag_name": "v0.1.0"}], [remote_release(2)]], "Multiple"),
            ([[remote_release(), remote_release()]], "Multiple"),
            (10 * [100 * [{"tag_name": "v0.1.0"}]], "1000-entry"),
        )
        for pages, message in cases:
            with self.subTest(message=message):
                client = Mock(token="fixture", api=Mock(side_effect=pages))
                with self.assertRaisesRegex(ValueError, message):
                    find_release(client, REPO, TAG)
                self.assertLessEqual(client.api.call_count, 10)

    def test_malformed_pages_entries_and_unauthenticated_lookup_are_rejected(self):
        for response in ({}, None, [None], [{}], [{"tag_name": False}], 101 * [{"tag_name": "v0.1.0"}]):
            with self.subTest(response=type(response).__name__), self.assertRaises(ValidationError):
                find_release(Mock(token="fixture", api=Mock(return_value=response)), REPO, TAG)
        for token in (None, "", " ", False):
            client = Mock(token=token)
            with self.subTest(token=token), self.assertRaises(ValidationError):
                find_release(client, REPO, TAG)
            client.api.assert_not_called()

    def test_matched_release_requires_strict_identity_repository_and_state(self):
        invalid = [None, {}]
        invalid.extend({**remote_release(), "id": identity} for identity in (None, True, 0, -1, "1"))
        invalid.extend(
            {**remote_release(), key: value}
            for key, value in (
                ("tag_name", "v2.0.0"),
                ("prerelease", True),
                ("prerelease", 0),
                ("draft", None),
                ("draft", 1),
                ("url", "https://api.github.com/repos/other/repo/releases/1"),
            )
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                release_identity(value, REPO, TAG)
        with self.assertRaisesRegex(ValidationError, "identity changed"):
            read_release(Mock(api=Mock(return_value=remote_release(2))), REPO, TAG, 1)
        with self.assertRaisesRegex(ValidationError, "ID"):
            find_release(Mock(token="fixture", api=Mock(return_value=[{**remote_release(), "id": False}])), REPO, TAG)


class ReleaseMutationIdentityTests(Workspace):
    def test_actual_draft_tag_endpoint_404_does_not_hide_draft_from_authenticated_list(self):
        release, output = self.plugin()
        client = FakeGitHub()
        expected = client.add(FIRST, release, output, draft=True)
        with self.assertRaisesRegex(ValidationError, "404"):
            client.api(f"repos/{REPO}/releases/tags/{TAG}")
        self.assertEqual(expected, find_release(client, REPO, TAG))
        self.assertEqual(expected, read_release(client, REPO, TAG, expected["id"]))

    def test_deleted_pinned_draft_is_never_reselected_as_same_tag_replacement(self):
        release, output = self.plugin()
        client = FakeGitHub()
        original = client.add(FIRST, release, output, draft=True)
        original["assets"].pop()
        endpoint = f"repos/{REPO}/releases/{original['id']}"
        real_api = client.api
        reads = 0

        def api(path):
            nonlocal reads
            if path == endpoint:
                reads += 1
                if reads == 2:
                    replacement = copy.deepcopy(original)
                    replacement.update(id=2, url=f"https://api.github.com/repos/{REPO}/releases/2")
                    client.releases[REPO] = [replacement]
            return real_api(path)

        client.api = api
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            self.assertRaisesRegex(ValidationError, "404"),
        ):
            publish(client, output, REPO, TAG, ".github/workflows/release.yml", Mock(), runner)
        runner.assert_not_called()
        self.assertEqual(1, sum("releases?" in path for path in client.calls))
        self.assertEqual(2, reads)

    def test_uploads_and_publication_use_only_pinned_numeric_endpoints(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        missing = remote["assets"].pop()
        writes = []

        def runner(arguments):
            writes.append(arguments)
            if arguments[3] == "POST":
                self.assertEqual(
                    f"https://uploads.github.com/repos/{REPO}/releases/{remote['id']}/assets?name={missing['name']}",
                    arguments[4],
                )
                self.assertIn(f"Content-Length: {missing['size']}", arguments)
                self.assertIn("Content-Type: application/octet-stream", arguments)
                remote["assets"].append(missing)
            else:
                self.assertEqual(f"repos/{REPO}/releases/{remote['id']}", arguments[4])
                self.assertIn("make_latest=true", arguments)
                client.publish(remote)

        with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
            publish(client, output, REPO, TAG, ".github/workflows/release.yml", Mock(), runner)
        self.assertEqual(["POST", "PATCH"], [arguments[3] for arguments in writes])
        self.assertFalse(any("/releases/tags/" in path for path in client.calls))

    def test_concurrently_completed_asset_is_verified_without_duplicate_upload(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        files = local_artifacts(output, release)
        runner = Mock()
        context = _ReleaseContext(REPO, TAG, remote["id"], runner=runner)
        upload_missing(client, output, files, ["release.json"], context)
        runner.assert_not_called()
        remote["draft"] = False
        with self.assertRaisesRegex(ValidationError, "no longer a draft"):
            upload_missing(client, output, files, ["release.json"], context)
        runner.assert_not_called()

    def test_published_draft_and_changed_tag_stop_remaining_mutations(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        starter = copy.deepcopy(remote["assets"][0])
        starter.update(state="starter", size=0)
        remote["assets"][0] = starter
        remote["draft"] = False
        runner = Mock()
        with self.assertRaisesRegex(ValidationError, "no longer a draft"):
            remove_draft_starters(client, [starter], REPO, TAG, remote["id"], runner)
        remote["draft"] = True
        remote["assets"][0].update(state="uploaded", size=len(client.assets[REPO, starter["id"]]))
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.release.remote_tag", side_effect=[COMMIT, "b" * 40]),
            self.assertRaisesRegex(ValidationError, "tag changed"),
        ):
            publish(client, output, REPO, TAG, ".github/workflows/release.yml", Mock(), runner)
        runner.assert_not_called()
