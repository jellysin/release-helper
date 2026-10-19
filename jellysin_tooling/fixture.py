"""Run a local release directory through the catalog's release checks; never writes a manifest."""

from itertools import islice
from pathlib import Path

from .catalog import verified_release
from .common import MAX_ASSET, MAX_FILES, decode_json, filename, require

NOT_ATTESTED = "attestation NOT checked"


class FixtureClient:
    """Serves the fixture files at the URLs verified_release downloads; no network."""

    def __init__(self, files):
        self.files = files

    def download(self, url):
        return self.files[url.rsplit("/", 1)[1]]


def read_directory(directory):
    entries = sorted(islice(Path(directory).iterdir(), MAX_FILES + 1))
    require(len(entries) <= MAX_FILES, "Fixture has too many files")
    files = {}
    for path in entries:
        name = filename(path.name)
        require(path.is_file() and not path.is_symlink(), "Fixture entry is not a regular file")
        with path.open("rb") as stream:
            data = stream.read(MAX_ASSET + 1)
        require(0 < len(data) <= MAX_ASSET, "Incomplete or oversized asset")
        files[name] = data
    return files


def skip_attestation(_path, _repo, _workflow, _commit, _tag):
    """A fixture has no attestation: the check is skipped and every result says so."""


def check(directory, approved_plugins):
    """Validate one fixture release exactly as catalog does, except for its attestation."""
    files = read_directory(directory)
    require("release.json" in files, "Release is missing metadata, checksums, or SBOM")
    claimed = decode_json(files["release.json"])
    require(isinstance(claimed, dict) and isinstance(claimed.get("plugin"), dict), "Invalid release schema")
    guid = claimed["plugin"].get("guid")
    approved = next((plugin for plugin in approved_plugins if plugin["guid"] == guid), None)
    require(approved is not None, "Fixture plugin is not in the allowlist")
    tag = claimed.get("tag")
    require(isinstance(tag, str), "Release version mismatch")
    base = f"https://github.com/{approved['repository']}/releases/download/{tag}/"
    remote = {
        "tag_name": tag,
        "assets": [
            {"name": name, "state": "uploaded", "size": len(data), "browser_download_url": base + name}
            for name, data in files.items()
        ],
    }
    release = verified_release(FixtureClient(files), approved, remote, skip_attestation)
    return f"{approved['repository']} {release['tag']}: fixture passed the catalog checks; {NOT_ATTESTED}"
