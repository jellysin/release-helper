"""No credential forwarding, unsafe hosts, unbounded reads, or uncontrolled retries."""

import io
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch

from jellysin_tooling.common import ValidationError
from jellysin_tooling.github import (
    GitHub,
    NoRedirect,
    command,
    command_bytes,
    read_response,
    safe_url,
    verify_attestation,
)
from tests.fixtures import COMMIT, FIRST


class NetworkTests(unittest.TestCase):
    def test_download_url_policy(self):
        for url in (
            "http://github.com/x",
            "https://github.com.evil.test/x",
            "https://me@github.com/x",
            "https://github.com:8080/x",
            "https://github.com/x#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                safe_url(url, {"github.com"})

    def test_reads_stop_at_size_and_time_limits(self):
        with self.assertRaises(ValidationError):
            read_response(io.BytesIO(b"1234"), 3, float("inf"))
        with self.assertRaises(ValidationError):
            read_response(io.BytesIO(b"1234"), 9, -1)
        self.assertEqual(b"1234", read_response(io.BytesIO(b"1234"), 4, float("inf")))

    def test_token_is_only_sent_to_api_and_removed_after_asset_redirect(self):
        client = GitHub("fixture-token")
        error = urllib.error.HTTPError(
            "", 302, "", {"Location": "https://release-assets.githubusercontent.com/asset"}, None
        )
        response = io.BytesIO(b"asset")
        client.opener = Mock()
        client.opener.open.side_effect = [error, response]
        self.assertEqual(b"asset", client.download_asset(FIRST["repository"], 1))
        first, second = [call.args[0] for call in client.opener.open.call_args_list]
        self.assertEqual("Bearer fixture-token", first.get_header("Authorization"))
        self.assertIsNone(second.get_header("Authorization"))

    def test_redirect_to_unapproved_host_never_receives_request(self):
        client = GitHub("fixture-token")
        client.opener = Mock()
        error = urllib.error.HTTPError("", 302, "", {"Location": "https://evil.test/"}, None)
        client.opener.open.side_effect = error
        with self.assertRaises(ValidationError):
            client.download("https://github.com/x")
        self.assertEqual(1, client.opener.open.call_count)
        self.assertTrue(error.fp.closed)

    def test_api_redirect_and_http_error_are_redacted(self):
        client = GitHub("fixture-token")
        client.opener = Mock()
        for code in (302, 404, 429, 503):
            client.opener.open.side_effect = urllib.error.HTTPError("sensitive-url", code, "private-body", {}, None)
            with self.subTest(code=code), self.assertRaises(ValidationError) as caught:
                client.api("repos/jellysin/repo")
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn("sensitive", str(caught.exception))

    def test_download_redirect_limit_and_transport_failure(self):
        client = GitHub()
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            "", 302, "", {"Location": "https://github.com/again"}, None
        )
        with self.assertRaisesRegex(ValidationError, "redirect"):
            client.download("https://github.com/x")
        self.assertEqual(5, client.opener.open.call_count)
        client.opener.open.side_effect = urllib.error.URLError("secret")
        with self.assertRaisesRegex(ValidationError, "failed"):
            client.download("https://github.com/x")

    def test_api_json_response_and_path_constraints(self):
        client = GitHub()
        with patch.object(client, "get", return_value=b'{"ok":true}'):
            self.assertEqual({"ok": True}, client.api("repos/jellysin/repo"))
            with self.assertRaises(ValidationError):
                client.api("users/me")
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_subprocess_failure_timeout_and_output_limit_are_redacted(self):
        for value in (OSError("secret"), subprocess.TimeoutExpired("gh", 1), Mock(returncode=1, stdout=b"secret")):
            with (
                self.subTest(value=value),
                patch("subprocess.run") as run,
                self.assertRaises(ValidationError) as caught,
            ):
                if isinstance(value, Exception):
                    run.side_effect = value
                else:
                    run.return_value = value
                command(["gh", "version"])
            self.assertNotIn("secret", str(caught.exception))
        with patch("subprocess.run", return_value=Mock(returncode=0, stdout=b"ok\n")):
            self.assertEqual("ok", command(["gh", "version"]))
        with (
            patch("subprocess.run", return_value=Mock(returncode=0, stdout=b"long")),
            patch("jellysin_tooling.github.MAX_JSON", 1),
            self.assertRaises(ValidationError),
        ):
            command(["gh", "version"])

    def test_command_bytes_returns_raw_bytes_and_enforces_limit(self):
        with patch("subprocess.run", return_value=Mock(returncode=0, stdout=b"raw-bytes")):
            self.assertEqual(b"raw-bytes", command_bytes(["git", "archive"]))
        with (
            patch("subprocess.run", return_value=Mock(returncode=0, stdout=b"exceeds")),
            self.assertRaises(ValidationError),
        ):
            command_bytes(["git", "archive"], limit=4)

    def test_attestation_requires_workflow_tag_commit_and_hosted_runner(self):
        with patch("jellysin_tooling.github.command") as run:
            verify_attestation("fixture.zip", FIRST["repository"], ".github/workflows/release.yml", COMMIT, "v1.0.0")
        args = run.call_args.args[0]
        self.assertEqual(COMMIT, args[args.index("--source-digest") + 1])
        self.assertEqual("refs/tags/v1.0.0", args[args.index("--source-ref") + 1])
        self.assertEqual(
            FIRST["repository"] + "/.github/workflows/release.yml", args[args.index("--signer-workflow") + 1]
        )
        self.assertIn("--deny-self-hosted-runners", args)
