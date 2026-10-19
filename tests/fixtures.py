import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling.common import ValidationError
from jellysin_tooling.package import build

FIRST = {
    "guid": "2034650d-a290-4a16-b195-89fb44cfb932",
    "name": "JellySin Last.fm",
    "description": "Music integration",
    "overview": "Scrobbling and discovery",
    "owner": "JellySin",
    "category": "Music",
    "targetAbi": "12.0.0.0",
    "assemblyFile": "JellySin.Plugin.Lastfm.dll",
    "repository": "jellysin/plugin-lastfm",
}
SECOND = {
    **FIRST,
    "guid": "c951d02c-b9c5-4e6a-b39c-f336e5d18891",
    "name": "JellySin Cinema",
    "category": "Movies",
    "assemblyFile": "JellySin.Plugin.Cinema.dll",
    "repository": "jellysin/cinema",
}
COMMIT = "a" * 40
CREATED = "2026-09-08T12:00:00Z"


def make_bundle(
    asset_bytes,
    media_type="application/vnd.dev.sigstore.bundle.v0.3+json",
    algorithm="SHA2_256",
    digest=None,
    padding="",
):
    if digest is None:
        digest = base64.b64encode(hashlib.sha256(asset_bytes).digest()).decode("ascii")
    bundle = {
        "mediaType": media_type,
        "verificationMaterial": {
            "certificate": "synthetic-certificate",
            "padding": padding,
        },
        "messageSignature": {
            "messageDigest": {
                "algorithm": algorithm,
                "digest": digest,
            },
            "signature": "synthetic-signature",
        },
    }
    return json.dumps(bundle, indent=2).encode("utf-8")


def write_bundles(directory, files=None):
    root = Path(directory)
    if files is None:
        files = {
            f.name: f.read_bytes() for f in root.iterdir() if f.is_file() and not f.name.endswith(".sigstore.json")
        }
    bundles = {}
    for name, data in files.items():
        bundle_name = f"{name}.sigstore.json"
        bundle_bytes = make_bundle(data)
        (root / bundle_name).write_bytes(bundle_bytes)
        bundles[bundle_name] = bundle_bytes
    return bundles


def approved(info=FIRST):
    return {"guid": info["guid"], "repository": info["repository"], "signerWorkflow": ".github/workflows/release.yml"}


class Workspace(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GITHUB_ACTIONS": "false", "GITHUB_OUTPUT": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def plugin(self, info=FIRST, version="1.0.0", contents=b"MZplugin fixture"):
        source = self.root / info["name"] / version / "publish"
        source.mkdir(parents=True, exist_ok=True)
        (source / info["assemblyFile"]).write_bytes(contents)
        output = source.parent / "dist"
        release = build(source, copy.deepcopy(info), version, COMMIT, CREATED, output)
        return release, output


class FakeGitHub:
    def __init__(self):
        self.token = "synthetic-fixture-token"
        self.releases = {}
        self.downloads = {}
        self.assets = {}
        self.calls = []

    def add(self, info, release, output, draft=False):
        repo = info["repository"]
        identity = sum(len(values) for values in self.releases.values()) + 1
        remote = {
            "id": identity,
            "url": f"https://api.github.com/repos/{repo}/releases/{identity}",
            "tag_name": release["tag"],
            "draft": draft,
            "prerelease": False,
            "immutable": not draft,
            "assets": [],
        }
        for index, file in enumerate(sorted(output.iterdir()), 1):
            download_tag = f"untagged-fixture-{identity}" if draft else release["tag"]
            url = f"https://github.com/{repo}/releases/download/{download_tag}/{file.name}"
            contents = file.read_bytes()
            remote["assets"].append(
                {
                    "id": index,
                    "url": f"https://api.github.com/repos/{repo}/releases/assets/{index}",
                    "name": file.name,
                    "state": "uploaded",
                    "size": len(contents),
                    "browser_download_url": url,
                }
            )
            self.downloads[url] = contents
            self.assets[repo, index] = contents
        self.releases.setdefault(repo, []).append(remote)
        return remote

    def publish(self, remote):
        repo = remote["url"].removeprefix("https://api.github.com/repos/").rsplit("/releases/", 1)[0]
        for asset in remote["assets"]:
            url = f"https://github.com/{repo}/releases/download/{remote['tag_name']}/{asset['name']}"
            self.downloads[url] = self.assets[repo, asset["id"]]
            asset["browser_download_url"] = url
        remote.update(draft=False, immutable=True)

    def api(self, path):
        self.calls.append(path)
        for repo, releases in self.releases.items():
            if path.startswith(f"repos/{repo}/releases?"):
                page = int(path.rsplit("page=", 1)[1])
                return releases[(page - 1) * 100 : page * 100]
            if path.startswith(f"repos/{repo}/releases/tags/"):
                tag = path.rsplit("/", 1)[1]
                matches = [release for release in releases if release["tag_name"] == tag and not release["draft"]]
                if not matches:
                    self.not_found()
                return matches[0]
            if path.startswith(f"repos/{repo}/releases/"):
                identity = int(path.rsplit("/", 1)[1])
                matches = [release for release in releases if release["id"] == identity]
                if not matches:
                    self.not_found()
                return matches[0]
            if path.startswith(f"repos/{repo}/git/ref/"):
                return {"object": {"type": "commit", "sha": COMMIT}}
        raise AssertionError("Unexpected API call")

    @staticmethod
    def not_found():
        raise ValidationError("GitHub request failed with HTTP 404")

    def download(self, url):
        self.calls.append(url)
        return self.downloads[url]

    def download_asset(self, repo, asset_id):
        return self.assets[repo, asset_id]
