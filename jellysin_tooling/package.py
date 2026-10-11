"""Produce reproducible flat plugin archives and SPDX file inventories."""

import io
import zipfile
from pathlib import Path

from . import inventory as dependency_inventory
from . import spdx
from .common import (
    MAX_ASSET,
    SHA,
    atomic_write,
    digest,
    json_bytes,
    metadata,
    release_notes,
    require,
    semver,
    target_abi,
    timestamp,
)


def package_files(publish_directory, info):
    root = Path(publish_directory).resolve(strict=True)
    files = {}
    total = 0
    for name in sorted([info["assemblyFile"], *info.get("packageFiles", [])]):
        source = root / name
        require(not source.is_symlink(), "Symbolic links must not be packaged")
        require(source.resolve(strict=True).parent == root and source.is_file(), "Unsafe package path")
        with source.open("rb") as stream:
            contents = stream.read(MAX_ASSET + 1)
        total += len(contents)
        require(0 < len(contents) and total <= MAX_ASSET, "Empty or oversized plugin package")
        files[name] = contents
    return files


def archive_bytes(files):
    buffer = io.BytesIO()
    # Stored entries eliminate zlib/runtime differences. DLLs are small; reproducibility wins.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, contents in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, contents)
    require(len(buffer.getvalue()) <= MAX_ASSET, "ZIP exceeds asset limit")
    return buffer.getvalue()


def build(publish_directory, info, version, commit, created, output_directory, inventory=None, notes=None):
    info = metadata(info)
    if inventory is not None:
        inventory = dependency_inventory.validate(inventory)
        dependency_inventory.require_floor(inventory, target_abi(info["targetAbi"]))
    version = semver(version)
    require(isinstance(commit, str) and SHA.fullmatch(commit), "Expected full lowercase commit SHA")
    created = timestamp(created)
    files = package_files(publish_directory, info)
    archive = archive_bytes(files)
    name = f"{info['repository'].split('/')[1]}-{version}.zip"
    release = {
        "schemaVersion": 1,
        "plugin": info,
        "semver": version,
        "version": version + ".0",
        "tag": "v" + version,
        "commit": commit,
        "timestamp": created,
        "archive": {
            "name": name,
            "size": len(archive),
            "sha256": digest(archive),
            "md5": digest(archive, "md5"),
            "url": f"https://github.com/{info['repository']}/releases/download/v{version}/{name}",
        },
    }
    if inventory is not None:
        release["dependencyInventory"] = inventory
    if notes is not None:
        release["changelog"] = release_notes(notes)
    artifacts = {
        name: archive,
        "release.json": json_bytes(release),
        "sbom.spdx.json": json_bytes(spdx.document(info, version, commit, created, files, inventory)),
    }
    artifacts["checksums.txt"] = "".join(
        f"{digest(data)}  {filename}\n" for filename, data in sorted(artifacts.items())
    ).encode()
    for filename, contents in artifacts.items():
        target = Path(output_directory) / filename
        if target.exists():
            require(target.read_bytes() == contents, "Refusing to replace different release bytes")
        else:
            atomic_write(target, contents)
    return release
