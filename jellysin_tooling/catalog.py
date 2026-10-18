"""Validate approved releases and reconcile the native Jellyfin manifest atomically."""

import io
import re
import tempfile
import zipfile
from pathlib import Path
from uuid import UUID

from . import inventory, spdx
from .common import (
    MAX_ASSET,
    MAX_FILES,
    MAX_RELEASES,
    SHA,
    SIGNER_WORKFLOWS,
    atomic_write,
    decode_json,
    digest,
    filename,
    image_url,
    json_bytes,
    metadata,
    release_notes,
    repository,
    require,
    semver,
    target_abi,
    text,
    timestamp,
)
from .github import verify_attestation

PLUGIN_FIELDS = frozenset({"guid", "name", "description", "overview", "owner", "category", "versions"})
LINK = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def allowlist(value):
    require(isinstance(value, dict) and value.get("schemaVersion") == 1, "Invalid allowlist schema")
    plugins = value.get("plugins")
    require(isinstance(plugins, list) and 0 < len(plugins) <= 32, "Invalid plugin allowlist")
    identities = set()
    repos = set()
    for plugin in plugins:
        require(
            isinstance(plugin, dict) and plugin.keys() == {"repository", "guid", "signerWorkflow"},
            "Invalid allowlist entry",
        )
        repository(plugin["repository"])
        require(str(UUID(plugin["guid"])) == plugin["guid"], "Invalid allowlisted GUID")
        require(plugin["signerWorkflow"] in SIGNER_WORKFLOWS, "Unapproved release workflow")
        require(plugin["guid"] not in identities and plugin["repository"] not in repos, "Duplicate plugin identity")
        identities.add(plugin["guid"])
        repos.add(plugin["repository"])
    return plugins


def validate_release(value, approved, tag):
    require(isinstance(value, dict) and value.get("schemaVersion") == 1, "Invalid release schema")
    info = metadata(value.get("plugin"))
    require(
        info["repository"] == approved["repository"] and info["guid"] == approved["guid"], "Release identity mismatch"
    )
    version = semver(value.get("semver"))
    require(
        value.get("tag") == tag == "v" + version and value.get("version") == version + ".0", "Release version mismatch"
    )
    require(isinstance(value.get("commit"), str) and SHA.fullmatch(value["commit"]), "Invalid source commit")
    require(timestamp(value.get("timestamp")) == value["timestamp"], "Noncanonical release timestamp")
    if "dependencyInventory" in value:
        inventory.require_floor(inventory.validate(value["dependencyInventory"]), target_abi(info["targetAbi"]))
    if "changelog" in value:
        release_notes(value["changelog"])
    asset = value.get("archive")
    require(
        isinstance(asset, dict) and asset.keys() == {"name", "url", "sha256", "md5", "size"}, "Invalid archive metadata"
    )
    name = filename(asset["name"])
    require(name == f"{approved['repository'].split('/')[1]}-{version}.zip", "Unexpected archive name")
    expected = f"https://github.com/{approved['repository']}/releases/download/{tag}/{name}"
    require(asset["url"] == expected, "Archive URL is outside approved release")
    require(type(asset["size"]) is int and 0 < asset["size"] <= MAX_ASSET, "Invalid archive size")
    for algorithm, length in (("sha256", 64), ("md5", 32)):
        value_hash = asset[algorithm]
        require(
            isinstance(value_hash, str)
            and len(value_hash) == length
            and all(c in "0123456789abcdef" for c in value_hash),
            "Invalid checksum",
        )
    return value


def validate_archive(data, release):
    asset = release["archive"]
    require(
        len(data) == asset["size"] and digest(data) == asset["sha256"] and digest(data, "md5") == asset["md5"],
        "Archive checksum mismatch",
    )
    expected = sorted([release["plugin"]["assemblyFile"], *release["plugin"].get("packageFiles", [])])
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            require(
                len(entries) <= MAX_FILES and sorted(item.filename for item in entries) == expected,
                "Unexpected archive contents",
            )
            require(sum(item.file_size for item in entries) <= MAX_ASSET, "Expanded archive too large")
            for entry in entries:
                require(
                    entry.compress_type == zipfile.ZIP_STORED and entry.external_attr >> 16 == 0o100644,
                    "Unexpected archive mode or compression",
                )
                require(not entry.flag_bits & 1 and entry.file_size > 0, "Encrypted or empty archive member")
            return {entry.filename: archive.read(entry) for entry in entries}  # Validate CRC; never extract.
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("Invalid ZIP archive") from exc


def manifest_entry(release):
    info = release["plugin"]
    return {
        "version": release["version"],
        "targetAbi": info["targetAbi"],
        "sourceUrl": release["archive"]["url"],
        "checksum": release["archive"]["md5"],
        "timestamp": release["timestamp"],
        # A release without attested notes keeps the release page link that earlier entries carry.
        "changelog": release.get("changelog", f"https://github.com/{info['repository']}/releases/tag/{release['tag']}"),
    }


def validate_manifest(value):
    require(isinstance(value, list) and len(value) <= 32, "Manifest must be a bounded array")
    identities = set()
    for plugin in value:
        require(isinstance(plugin, dict) and isinstance(plugin.get("guid"), str), "Invalid manifest plugin")
        require(PLUGIN_FIELDS <= plugin.keys() <= PLUGIN_FIELDS | {"imageUrl"}, "Unexpected manifest fields")
        require(str(UUID(plugin["guid"])) == plugin["guid"], "Invalid manifest GUID")
        for key in ("name", "description", "overview", "owner", "category"):
            text(plugin[key], key)
        require(plugin["guid"] not in identities, "Duplicate manifest plugin")
        identities.add(plugin["guid"])
        versions = plugin.get("versions")
        require(isinstance(versions, list) and 0 < len(versions) <= MAX_RELEASES, "Invalid version list")
        seen = set()
        for version in versions:
            validate_version(version)
            key = version["version"]
            require(key not in seen, "Duplicate catalog version")
            seen.add(key)
        if "imageUrl" in plugin:
            sources = {"/".join(version["sourceUrl"].split("/")[3:5]) for version in versions}
            require(len(sources) == 1, "Image URL needs one source repository")
            image_url(plugin["imageUrl"], sources.pop())
    return value


def validate_version(value):
    require(
        isinstance(value, dict)
        and value.keys() == {"version", "targetAbi", "sourceUrl", "checksum", "timestamp", "changelog"},
        "Invalid catalog version",
    )
    version = text(value["version"], "version", 24)
    require(version.endswith(".0"), "Expected four-part plugin version")
    stable = semver(version[:-2])
    target_abi(value["targetAbi"])
    checksum = value["checksum"]
    require(
        isinstance(checksum, str) and len(checksum) == 32 and all(c in "0123456789abcdef" for c in checksum),
        "Invalid MD5 checksum",
    )
    require(timestamp(value["timestamp"]) == value["timestamp"], "Noncanonical catalog timestamp")
    source = text(value["sourceUrl"], "source URL", 512)
    require(source.startswith("https://github.com/"), "Catalog source must be an approved GitHub release")
    parts = source.removeprefix("https://github.com/").split("/")
    require(len(parts) == 6 and parts[2:5] == ["releases", "download", "v" + stable], "Invalid catalog release URL")
    repo = repository("/".join(parts[:2]))
    require(filename(parts[5]) == f"{parts[1]}-{stable}.zip", "Unexpected catalog archive")
    notes = value["changelog"]
    if notes != f"https://github.com/{repo}/releases/tag/v{stable}":
        # Notes are text; a lone link may only name the release that the entry describes.
        require(isinstance(notes, str) and not LINK.match(notes), "Unexpected changelog URL")
        release_notes(notes)
    return value


def merge_manifest(current, releases):
    validate_manifest(current)
    merged = decode_json(json_bytes(current))
    by_id = {entry["guid"]: entry for entry in merged}
    for release in releases:
        info = release["plugin"]
        plugin = by_id.get(info["guid"])
        if plugin is None:
            plugin = {key: info[key] for key in sorted((PLUGIN_FIELDS - {"versions"}) | ({"imageUrl"} & info.keys()))}
            plugin["versions"] = []
            merged.append(plugin)
            by_id[info["guid"]] = plugin
        entry = manifest_entry(release)
        existing = next((version for version in plugin["versions"] if version["version"] == entry["version"]), None)
        if existing is not None:
            require(existing == entry, "Published catalog entries are immutable")
        else:
            plugin["versions"].append(entry)
        plugin["versions"].sort(key=lambda item: tuple(int(part) for part in item["version"].split(".")), reverse=True)
    merged.sort(key=lambda item: item["guid"])
    return validate_manifest(merged)


def release_assets(release, repo, tag, *, authenticated_draft=False):
    require(not authenticated_draft or release.get("draft") is True, "Authenticated draft validation requires a draft")
    assets = release.get("assets")
    require(isinstance(assets, list) and len(assets) <= MAX_FILES, "Invalid release assets")
    result = {}
    for asset in assets:
        require(isinstance(asset, dict), "Invalid release asset")
        name = filename(asset.get("name"))
        require(name not in result, "Duplicate release asset")
        require(
            asset.get("state") == "uploaded" and type(asset.get("size")) is int and 0 < asset["size"] <= MAX_ASSET,
            "Incomplete or oversized asset",
        )
        if authenticated_draft:
            identity = asset.get("id")
            require(type(identity) is int and identity > 0, "Invalid draft asset ID")
            require(
                asset.get("url") == f"https://api.github.com/repos/{repo}/releases/assets/{identity}",
                "Unapproved draft asset API URL",
            )
        else:
            require(
                asset.get("browser_download_url") == f"https://github.com/{repo}/releases/download/{tag}/{name}",
                "Unapproved release asset URL",
            )
        result[name] = asset
    return result


def verified_release(client, approved, remote, verifier=verify_attestation):
    repo = approved["repository"]
    tag = remote["tag_name"]
    assets = release_assets(remote, repo, tag)
    require(
        {"release.json", "checksums.txt", "sbom.spdx.json"} <= assets.keys(),
        "Release is missing metadata, checksums, or SBOM",
    )
    metadata_bytes = client.download(assets["release.json"]["browser_download_url"])
    release = validate_release(decode_json(metadata_bytes), approved, tag)
    require(release["archive"]["name"] in assets, "Release is missing plugin ZIP")
    with tempfile.TemporaryDirectory(prefix="jellysin-release-") as temporary:
        files = {"release.json": metadata_bytes}
        for name in (release["archive"]["name"], "checksums.txt", "sbom.spdx.json"):
            files[name] = client.download(assets[name]["browser_download_url"])
        for name, data in files.items():
            path = Path(temporary) / name
            path.write_bytes(data)
            verifier(path, repo, approved["signerWorkflow"], release["commit"], tag)
        members = validate_archive(files[release["archive"]["name"]], release)
        expected = "".join(
            f"{digest(data)}  {name}\n" for name, data in sorted(files.items()) if name != "checksums.txt"
        ).encode()
        require(files["checksums.txt"] == expected, "Release checksum manifest mismatch")
        document = decode_json(files["sbom.spdx.json"])
        spdx.validate(document, release, members)
    return release


def collect(client, approved_plugins, current, verifier=verify_attestation):
    validate_manifest(current)
    known = {(plugin["guid"], version["version"]) for plugin in current for version in plugin["versions"]}
    pending = []
    for approved in approved_plugins:
        exhausted = False
        for page in range(1, 11):
            releases = client.api(f"repos/{approved['repository']}/releases?per_page=100&page={page}")
            require(isinstance(releases, list) and len(releases) <= 100, "Invalid GitHub releases response")
            for remote in releases:
                if remote.get("draft") or remote.get("prerelease"):
                    continue
                tag = remote.get("tag_name", "")
                if not isinstance(tag, str) or not tag.startswith("v"):
                    continue
                version = semver(tag[1:]) + ".0"
                require(remote.get("immutable") is True, "Source release must have GitHub immutability enabled")
                if (approved["guid"], version) in known:
                    continue
                if len(pending) == 20:
                    return merge_manifest(current, pending)
                pending.append(verified_release(client, approved, remote, verifier))
                known.add((approved["guid"], version))
            if len(releases) < 100:
                exhausted = True
                break
        require(exhausted, "Release history exceeds 1000-entry scan limit")
    return merge_manifest(current, pending)


def update(path, current, merged):
    require(validate_manifest(current) == current, "Invalid existing catalog")
    # One replacement after every download, signature, checksum and merge succeeds.
    if json_bytes(current) != json_bytes(merged):
        atomic_write(path, json_bytes(merged))
