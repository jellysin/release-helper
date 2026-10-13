"""The figures README.md and CONTRIBUTING.md state, each held by a test that fails when it drifts.

README.md:69 states 32 files and 90-second downloads; README.md:244 states 20 seconds per network step
and 60 seconds per answer of the server check; CONTRIBUTING.md:46 states that Renovate widens
requires-python instead of pinning it.
"""

import io
import unittest
from unittest.mock import MagicMock, patch

from jellysin_tooling import servercheck
from jellysin_tooling.catalog import release_assets
from jellysin_tooling.common import MAX_FILES, ValidationError, metadata, read_json
from jellysin_tooling.github import GitHub, read_response
from tests.action_steps import ROOT
from tests.fixtures import FIRST

REPOSITORY, TAG = FIRST["repository"], "v1.0.0"


def asset(index):
    name = f"file-{index}.txt"
    return {
        "name": name,
        "state": "uploaded",
        "size": 1,
        "browser_download_url": f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{name}",
    }


class Clock:
    """time.monotonic that returns the given readings in order, then repeats the last."""

    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


class FileCountTests(unittest.TestCase):
    def test_the_bound_is_32_files(self):
        self.assertEqual(MAX_FILES, 32)

    def test_a_package_holds_the_assembly_and_at_most_31_package_files(self):
        files = [f"file-{index}.json" for index in range(MAX_FILES)]
        metadata({**FIRST, "packageFiles": files[:31]})
        with self.assertRaisesRegex(ValidationError, "Too many package files"):
            metadata({**FIRST, "packageFiles": files})

    def test_a_release_holds_at_most_32_assets(self):
        self.assertEqual(len(release_assets({"assets": [asset(i) for i in range(32)]}, REPOSITORY, TAG)), 32)
        with self.assertRaisesRegex(ValidationError, "Invalid release assets"):
            release_assets({"assets": [asset(i) for i in range(33)]}, REPOSITORY, TAG)


class DownloadDeadlineTests(unittest.TestCase):
    def test_a_read_just_inside_the_deadline_continues_and_one_at_it_stops(self):
        with patch("jellysin_tooling.github.time.monotonic", Clock(89.999)):
            self.assertEqual(read_response(io.BytesIO(b"data"), 100, 90), b"data")
        with (
            patch("jellysin_tooling.github.time.monotonic", Clock(90)),
            self.assertRaisesRegex(ValidationError, "Download exceeded deadline"),
        ):
            read_response(io.BytesIO(b"data"), 100, 90)

    def test_a_download_gets_90_seconds_and_each_request_20(self):
        client = GitHub(token="")
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = io.BytesIO(b"bytes")
        with patch("jellysin_tooling.github.time.monotonic", Clock(1000, 1089.999)):
            self.assertEqual(client.get("https://github.com/a/b/releases/download/v1/x", binary=True), b"bytes")
        self.assertEqual(client.opener.open.call_args.kwargs["timeout"], 20)
        with (
            patch("jellysin_tooling.github.time.monotonic", Clock(1000, 1090)),
            self.assertRaisesRegex(ValidationError, "Download exceeded deadline"),
        ):
            client.get("https://github.com/a/b/releases/download/v1/x", binary=True)


class ServerTimeoutTests(unittest.TestCase):
    def test_each_server_request_gets_20_seconds_and_its_answer_60(self):
        response = MagicMock(status=200)
        connection = MagicMock()
        connection.getresponse.return_value = response
        with (
            patch("jellysin_tooling.servercheck.http.client.HTTPConnection", return_value=connection) as opened,
            patch("jellysin_tooling.servercheck.time.monotonic", return_value=500.0),
            patch("jellysin_tooling.servercheck.read_response", return_value=b"{}") as read,
        ):
            servercheck.Server(8096).call("GET", "/System/Info/Public")
        self.assertEqual(opened.call_args.args, ("127.0.0.1", 8096))
        self.assertEqual(opened.call_args.kwargs, {"timeout": 20})
        self.assertEqual(read.call_args.args[2], 560.0)


class RenovateRuleTests(unittest.TestCase):
    def test_renovate_widens_requires_python_while_it_pins_everything_else(self):
        config = read_json(ROOT / "renovate.json")
        self.assertEqual(config["rangeStrategy"], "pin")
        rules = [
            rule
            for rule in config["packageRules"]
            if rule.get("matchManagers") == ["pep621"] and rule.get("matchDepTypes") == ["requires-python"]
        ]
        self.assertEqual(len(rules), 1, "one rule must select the pep621 requires-python dependency")
        self.assertEqual(rules[0].get("rangeStrategy"), "widen")
        self.assertEqual(set(rules[0]) - {"description"}, {"matchManagers", "matchDepTypes", "rangeStrategy"})
