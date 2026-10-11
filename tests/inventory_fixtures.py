"""Small independently declared package/restore graphs for inventory boundary tests."""

import base64

from jellysin_tooling.common import json_bytes


def restore_files(root, version="12.0.0"):
    hash_a = base64.b64encode(b"a" * 64).decode()
    hash_b = base64.b64encode(b"b" * 64).decode()
    lock = {
        "version": 1,
        "dependencies": {
            "net10.0": {
                "Jellyfin.Controller": {
                    "type": "Direct",
                    "resolved": version,
                    "contentHash": hash_a,
                    "dependencies": {"Jellyfin.Data": version},
                },
                "Jellyfin.Data": {"type": "Transitive", "resolved": version, "contentHash": hash_b},
            }
        },
    }
    assets = {
        "version": 4,
        "targets": {
            "net10.0": {
                f"Jellyfin.Controller/{version}": {"type": "package", "dependencies": {"Jellyfin.Data": version}},
                f"Jellyfin.Data/{version}": {"type": "package"},
            }
        },
        "libraries": {
            f"Jellyfin.Controller/{version}": {"type": "package", "sha512": hash_a},
            f"Jellyfin.Data/{version}": {"type": "package", "sha512": hash_b},
        },
        "packageFolders": {"/first/checkout/cache/": {}},
    }
    runtime = {
        "schemaVersion": 1,
        "targetFramework": "net10.0",
        "runtimeDependencies": [{"id": "Jellyfin.Controller", "version": version, "providedBy": "jellyfin"}],
    }
    documents = [lock, assets, runtime, {"sdk": {"version": "10.0.400"}}]
    paths = [
        root / name
        for name in ("packages.lock.json", "project.assets.json", "runtime-dependencies.json", "global.json")
    ]
    for path, document in zip(paths, documents, strict=True):
        path.write_bytes(json_bytes(document))
    return paths, documents
