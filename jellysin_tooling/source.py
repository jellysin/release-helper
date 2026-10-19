"""Deterministic source archive packaging, metadata, and verification."""

import gzip
import io
import tarfile
from pathlib import Path

from . import spdx
from .common import (
    MAX_ASSET,
    SHA,
    ValidationError,
    atomic_write,
    decode_json,
    digest,
    filename,
    json_bytes,
    repository,
    require,
    semver,
    timestamp,
)
from .github import command_bytes

# A source archive holds the whole tracked tree, not the few files of a plugin ZIP (MAX_FILES).
MAX_SOURCE_MEMBERS = 2048


def archive_bytes(commit, prefix, cwd=None):
    require(isinstance(commit, str) and SHA.fullmatch(commit), "Expected full lowercase commit SHA")
    require(isinstance(prefix, str) and prefix and not prefix.startswith("/"), "Invalid archive prefix")
    tar_data = command_bytes(["git", "archive", "--format=tar", f"--prefix={prefix}/", commit], cwd=cwd)
    require(len(tar_data) <= MAX_ASSET, "Tar archive exceeds asset limit")
    compressed = gzip.compress(tar_data, compresslevel=9, mtime=0)
    require(len(compressed) <= MAX_ASSET, "Archive exceeds asset limit")
    return compressed


def members(data, prefix=None):
    require(isinstance(data, bytes) and len(data) <= MAX_ASSET, "Invalid archive data")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            entries = tar.getmembers()
            require(len(entries) <= MAX_SOURCE_MEMBERS, "Too many archive members")
            require(sum(entry.size for entry in entries) <= MAX_ASSET, "Archive contents exceed asset limit")
            if prefix is None:
                require(len(entries) > 0, "Empty archive")
                prefix = entries[0].name.rstrip("/").split("/")[0]
            require(prefix and not prefix.startswith("/") and prefix != "..", "Invalid archive prefix")
            prefix_slash = f"{prefix}/"
            files = {}
            for entry in entries:
                require(entry.isfile() or entry.isdir(), "Unsupported archive member type")
                name = entry.name
                require(not name.startswith("/") and not Path(name).is_absolute(), "Unsafe archive path")
                require(".." not in name.split("/") and "\\" not in name, "Unsafe archive path")
                if entry.isdir() and (name == prefix or name == prefix.rstrip("/")):
                    continue
                require(name.startswith(prefix_slash), "Archive member outside prefix")
                if entry.isfile():
                    rel_name = name[len(prefix_slash) :]
                    require(rel_name and not rel_name.startswith("/"), "Invalid member relative name")
                    stream = tar.extractfile(entry)
                    require(stream is not None, "Failed to read archive member")
                    files[rel_name] = stream.read()
            return files
    except (tarfile.TarError, EOFError, gzip.BadGzipFile) as exc:
        raise ValidationError("Invalid source archive") from exc


def build(repo, tag, commit, created, output_directory, cwd=None):
    repository(repo)
    require(isinstance(tag, str) and tag.startswith("v"), "Release tag must start with v")
    version = semver(tag[1:])
    require(tag == "v" + version, "Tag does not match semver")
    require(isinstance(commit, str) and SHA.fullmatch(commit), "Expected full lowercase commit SHA")
    created = timestamp(created)
    repo_name = repo.split("/")[1]
    prefix = f"{repo_name}-{version}"
    name = f"{prefix}.tar.gz"
    archive = archive_bytes(commit, prefix, cwd=cwd)
    archive_files = members(archive, prefix)
    release = {
        "schemaVersion": 1,
        "kind": "source",
        "repository": repo,
        "semver": version,
        "tag": tag,
        "commit": commit,
        "timestamp": created,
        "archive": {
            "name": name,
            "size": len(archive),
            "sha256": digest(archive),
            "url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
        },
    }
    artifacts = {
        name: archive,
        "release.json": json_bytes(release),
        "sbom.spdx.json": json_bytes(spdx.source_document(repo, version, commit, created, archive_files)),
    }
    artifacts["checksums.txt"] = "".join(
        f"{digest(data)}  {artifact_name}\n" for artifact_name, data in sorted(artifacts.items())
    ).encode()
    for artifact_name, contents in artifacts.items():
        target = Path(output_directory) / artifact_name
        if target.exists():
            require(target.read_bytes() == contents, "Refusing to replace different release bytes")
        else:
            atomic_write(target, contents)
    return release


def validate_release(value, repo, tag):
    require(isinstance(value, dict), "Invalid release schema")
    required = {"schemaVersion", "kind", "repository", "semver", "tag", "commit", "timestamp", "archive"}
    require(value.keys() == required, "Invalid release schema")
    require(value["schemaVersion"] == 1, "Invalid release schema")
    require(value["kind"] == "source", "Invalid release kind")
    repository(repo)
    require(value["repository"] == repo, "Release repository mismatch")
    version = semver(value["semver"])
    require(value["tag"] == tag == "v" + version, "Release version mismatch")
    require(isinstance(value["commit"], str) and SHA.fullmatch(value["commit"]), "Invalid source commit")
    require(timestamp(value["timestamp"]) == value["timestamp"], "Noncanonical release timestamp")
    asset = value["archive"]
    require(isinstance(asset, dict) and asset.keys() == {"name", "size", "sha256", "url"}, "Invalid archive metadata")
    name = filename(asset["name"])
    repo_name = repo.split("/")[1]
    require(name == f"{repo_name}-{version}.tar.gz", "Unexpected archive name")
    expected_url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    require(asset["url"] == expected_url, "Archive URL is outside approved release")
    require(type(asset["size"]) is int and 0 < asset["size"] <= MAX_ASSET, "Invalid archive size")
    value_hash = asset["sha256"]
    require(
        isinstance(value_hash, str) and len(value_hash) == 64 and all(c in "0123456789abcdef" for c in value_hash),
        "Invalid checksum",
    )
    return value


def validate_local(files, release, cwd=None):
    asset = release["archive"]
    name = asset["name"]
    require(name in files, "Missing archive in local files")
    data = files[name]
    require(
        len(data) == asset["size"] and digest(data) == asset["sha256"],
        "Archive checksum mismatch",
    )
    repo_name = release["repository"].split("/")[1]
    prefix = f"{repo_name}-{release['semver']}"
    regenerated = archive_bytes(release["commit"], prefix, cwd=cwd)
    require(data == regenerated, "Local archive differs from checkout archive")
    tar_members = members(data, prefix)
    require("sbom.spdx.json" in files, "Missing SBOM in local files")
    spdx.validate_source(decode_json(files["sbom.spdx.json"]), release, tar_members)
    require("checksums.txt" in files, "Missing checksums in local files")
    checksums = "".join(
        f"{digest(bytes_data)}  {filename}\n"
        for filename, bytes_data in sorted(files.items())
        if filename != "checksums.txt"
    ).encode()
    require(files["checksums.txt"] == checksums, "Local release checksum manifest mismatch")


def local_artifacts(directory, release, cwd=None):
    root = Path(directory).resolve(strict=True)
    names = {release["archive"]["name"], "release.json", "sbom.spdx.json", "checksums.txt"}
    files = {}
    for name in names:
        path = root / name
        require(
            path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_ASSET,
            "Missing or unsafe release artifact",
        )
        files[name] = path.read_bytes()
    validate_local(files, release, cwd=cwd)
    return files
