"""Real draft assets have temporary browser URLs and stable authenticated API IDs."""

import copy
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from jellysin_tooling.catalog import collect, release_assets
from jellysin_tooling.common import MAX_ASSET, ValidationError, digest
from jellysin_tooling.release import (
    _ReleaseContext,
    draft_starters,
    local_artifacts,
    publish,
    reconcile_assets,
)
from tests.fixtures import COMMIT, FIRST, SECOND, FakeGitHub, Workspace, approved


class DraftAssetTests(Workspace):
    def test_both_plugins_resume_three_missing_assets_without_changing_existing_checksum(self):
        for info, version in ((FIRST, "1.0.0"), (SECOND, "2.3.4")):
            with self.subTest(repository=info["repository"]):
                release, output = self.plugin(info, version)
                client = FakeGitHub()
                remote = client.add(info, release, output, draft=True)
                assets = {asset["name"]: copy.deepcopy(asset) for asset in remote["assets"]}
                checksum = assets["checksums.txt"]
                remote["assets"] = [copy.deepcopy(checksum)]
                original = client.assets[info["repository"], checksum["id"]]
                self.assertIn("/untagged-", checksum["browser_download_url"])
                writes = []

                def runner(arguments, writes=writes, remote=remote, assets=assets, client=client):
                    writes.append(arguments)
                    if arguments[3] == "POST":
                        name = parse_qs(urlsplit(arguments[4]).query)["name"][0]
                        self.assertNotEqual("checksums.txt", name)
                        remote["assets"].append(copy.deepcopy(assets[name]))
                    else:
                        self.assertEqual("PATCH", arguments[3])
                        client.publish(remote)

                with patch("jellysin_tooling.release.exact_tag", return_value=COMMIT):
                    for _ in range(2):
                        publish(
                            client,
                            output,
                            info["repository"],
                            release["tag"],
                            ".github/workflows/release.yml",
                            Mock(),
                            runner,
                        )
                self.assertEqual(["POST", "POST", "POST", "PATCH"], [arguments[3] for arguments in writes])
                preserved = next(asset for asset in remote["assets"] if asset["name"] == "checksums.txt")
                self.assertEqual(checksum["id"], preserved["id"])
                self.assertEqual(checksum["url"], preserved["url"])
                self.assertEqual(original, client.assets[info["repository"], checksum["id"]])
                self.assertEqual(digest(original), digest((output / "checksums.txt").read_bytes()))
                manifest = collect(client, [approved(info)], [], Mock())
                self.assertEqual(release["archive"]["url"], manifest[0]["versions"][0]["sourceUrl"])

    def test_draft_validation_requires_positive_asset_id_and_exact_owned_api_url(self):
        release, output = self.plugin()
        remote = FakeGitHub().add(FIRST, release, output, draft=True)
        mutations = [("id", value) for value in (None, False, 0, -1, "1")]
        mutations.extend(
            ("url", value)
            for value in (
                None,
                "https://api.github.com/repos/other/repo/releases/assets/1",
                f"https://api.github.com/repos/{FIRST['repository']}/releases/assets/999",
                f"http://api.github.com/repos/{FIRST['repository']}/releases/assets/1",
                f"https://evil.test/repos/{FIRST['repository']}/releases/assets/1",
                f"https://api.github.com/repos/{FIRST['repository']}/releases/assets/1?redirect=elsewhere",
            )
        )
        for key, value in mutations:
            changed = copy.deepcopy(remote)
            changed["assets"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                release_assets(changed, FIRST["repository"], release["tag"], authenticated_draft=True)

    def test_unused_browser_url_is_never_downloaded_in_authenticated_draft_mode(self):
        release, output = self.plugin()
        remote = FakeGitHub().add(FIRST, release, output, draft=True)
        files = local_artifacts(output, release)
        by_id = {asset["id"]: files[asset["name"]] for asset in remote["assets"]}
        for asset in remote["assets"]:
            asset["browser_download_url"] = "https://untrusted.test/unused"
        client = Mock(download_asset=Mock(side_effect=lambda _repo, identity: by_id[identity]))
        context = _ReleaseContext(FIRST["repository"], release["tag"], remote["id"])
        self.assertEqual([], reconcile_assets(client, remote, files, context))
        self.assertEqual(4, client.download_asset.call_count)
        self.assertTrue(all(call.args[0] == FIRST["repository"] for call in client.download_asset.call_args_list))
        client.download.assert_not_called()

    def test_public_catalog_mode_never_accepts_temporary_draft_browser_urls(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        with self.assertRaisesRegex(ValidationError, "Unapproved release asset URL"):
            release_assets(remote, FIRST["repository"], release["tag"])
        remote.update(draft=False, immutable=True)
        with self.assertRaisesRegex(ValidationError, "Unapproved release asset URL"):
            collect(client, [approved()], [], Mock())
        for state in (False, None, 1):
            with self.subTest(state=state), self.assertRaisesRegex(ValidationError, "requires a draft"):
                release_assets(
                    {"draft": state, "assets": []}, FIRST["repository"], release["tag"], authenticated_draft=True
                )

    def test_draft_mode_preserves_asset_shape_name_state_size_and_count_bounds(self):
        release, output = self.plugin()
        remote = FakeGitHub().add(FIRST, release, output, draft=True)
        changes = [None, [None], 33 * [remote["assets"][0]], 2 * [remote["assets"][0]]]
        changes.extend(
            [{**remote["assets"][0], key: value}]
            for key, value in (
                ("name", "../secret"),
                ("state", "starter"),
                ("size", 0),
                ("size", -1),
                ("size", True),
                ("size", MAX_ASSET + 1),
            )
        )
        for assets in changes:
            with self.subTest(assets=assets), self.assertRaises(ValidationError):
                release_assets(
                    {**remote, "assets": assets}, FIRST["repository"], release["tag"], authenticated_draft=True
                )

    def test_starter_recovery_checks_owned_api_identity_before_any_mutation(self):
        release, output = self.plugin()
        client = FakeGitHub()
        remote = client.add(FIRST, release, output, draft=True)
        starter = remote["assets"][0]
        starter.update(state="starter", size=0)
        files = local_artifacts(output, release)
        _, pending = draft_starters(remote, files, FIRST["repository"], release["tag"])
        self.assertEqual([starter], pending)
        starter["url"] = "https://api.github.com/repos/other/repository/releases/assets/1"
        runner = Mock()
        with (
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            self.assertRaisesRegex(ValidationError, "draft asset API URL"),
        ):
            publish(
                client, output, FIRST["repository"], release["tag"], ".github/workflows/release.yml", Mock(), runner
            )
        runner.assert_not_called()
