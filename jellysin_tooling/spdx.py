"""Deterministic SPDX 2.3 file and dependency inventory with semantic validation."""

from urllib.parse import quote

from .common import TOOL_NAME, digest, require


def file_entries(files):
    return [
        {
            "SPDXID": f"SPDXRef-File-{index}",
            "fileName": name,
            "checksums": [
                {"algorithm": algorithm.upper(), "checksumValue": digest(data, algorithm)}
                for algorithm in ("sha1", "sha256")
            ],
            "licenseConcluded": "NOASSERTION",
            "copyrightText": "NOASSERTION",
        }
        for index, (name, data) in enumerate(sorted(files.items()))
    ]


def dependency_entries(inventory):
    if inventory is None:
        return [], []
    identities = {entry["id"]: f"SPDXRef-NuGet-{index}" for index, entry in enumerate(inventory["packages"])}
    packages = []
    relationships = []
    for entry in inventory["packages"]:
        identity = identities[entry["id"]]
        packages.append(
            {
                "SPDXID": identity,
                "name": entry["id"],
                "versionInfo": entry["version"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseDeclared": "NOASSERTION",
                "checksums": [{"algorithm": "SHA512", "checksumValue": entry["sha512"]}],
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:nuget/{quote(entry['id'], safe='')}@{quote(entry['version'], safe='')}",
                    }
                ],
                "comment": "Host-provided runtime dependency; not bundled."
                if entry["hostProvided"]
                else "Restored production build dependency; not bundled.",
            }
        )
        relationships.append(relationship(identity, "BUILD_DEPENDENCY_OF", "SPDXRef-Plugin"))
        if entry["hostProvided"]:
            relationships.append(relationship(identity, "RUNTIME_DEPENDENCY_OF", "SPDXRef-Plugin"))
        relationships.extend(relationship(identity, "DEPENDS_ON", identities[name]) for name in entry["dependencies"])
    return packages, relationships


def relationship(source, kind, target):
    return {"spdxElementId": source, "relationshipType": kind, "relatedSpdxElement": target}


def document(info, version, commit, created, files, inventory=None):
    entries = file_entries(files)
    dependencies, dependency_relationships = dependency_entries(inventory)
    root = {
        "SPDXID": "SPDXRef-Plugin",
        "name": info["name"],
        "versionInfo": version,
        "downloadLocation": f"https://github.com/{info['repository']}/releases/tag/v{version}",
        "filesAnalyzed": True,
        "licenseDeclared": "EUPL-1.2",
        "packageVerificationCode": {
            "packageVerificationCodeValue": digest(
                "".join(sorted(digest(data, "sha1") for data in files.values())).encode(), "sha1"
            )
        },
    }
    result = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{info['name']} {version}",
        "documentNamespace": f"https://github.com/{info['repository']}/sbom/{version}/{commit}",
        "creationInfo": {"created": created, "creators": [f"Tool: {TOOL_NAME}-1"]},
        "packages": [root, *dependencies],
        "files": entries,
        "relationships": [
            relationship("SPDXRef-DOCUMENT", "DESCRIBES", "SPDXRef-Plugin"),
            *[relationship("SPDXRef-Plugin", "CONTAINS", item["SPDXID"]) for item in entries],
            *dependency_relationships,
        ],
    }
    if inventory is not None:
        result["comment"] = (
            f"Production restore inventory for {inventory['targetFramework']}; .NET SDK {inventory['sdkVersion']}; "
            f"SHA256 of canonical NuGet lock JSON: {inventory['lockSha256']}. "
            "External host/framework files are not included or inspected; package hashes identify NuGet archives."
        )
    return result


def source_document(repo, version, commit, created, files, license_declared="NOASSERTION"):
    entries = file_entries(files)
    repo_name = repo.split("/")[1]
    root = {
        "SPDXID": "SPDXRef-Source",
        "name": repo_name,
        "versionInfo": version,
        "downloadLocation": f"https://github.com/{repo}/releases/tag/v{version}",
        "filesAnalyzed": True,
        "licenseDeclared": license_declared,
        "packageVerificationCode": {
            "packageVerificationCodeValue": digest(
                "".join(sorted(digest(data, "sha1") for data in files.values())).encode(), "sha1"
            )
        },
    }
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{repo_name} {version}",
        "documentNamespace": f"https://github.com/{repo}/sbom/{version}/{commit}",
        "creationInfo": {"created": created, "creators": [f"Tool: {TOOL_NAME}-1"]},
        "packages": [root],
        "files": entries,
        "relationships": [
            relationship("SPDXRef-DOCUMENT", "DESCRIBES", "SPDXRef-Source"),
            *[relationship("SPDXRef-Source", "CONTAINS", item["SPDXID"]) for item in entries],
        ],
    }


def validate_source(value, release, files, license_declared="NOASSERTION"):
    expected = source_document(
        release["repository"],
        release["semver"],
        release["commit"],
        release["timestamp"],
        files,
        license_declared=license_declared,
    )
    require(value == expected, "SBOM differs from release identity, files or checksums")


def validate(value, release, files):
    if release.get("kind") == "source":
        validate_source(value, release, files)
        return
    expected = document(
        release["plugin"],
        release["semver"],
        release["commit"],
        release["timestamp"],
        files,
        release.get("dependencyInventory"),
    )
    require(value == expected, "SBOM differs from release identity, files, checksums or dependency graph")
