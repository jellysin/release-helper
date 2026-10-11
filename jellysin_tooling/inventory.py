"""Read the production NuGet graph without loading assemblies or running packages."""

import base64
import re

from .common import digest, json_bytes, read_json, require, semver

MAX_PACKAGES = 512
HOST_FAMILY = "jellyfin."
HOST_REFERENCE = "jellyfin.controller"
PACKAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?")


def mapping(value, label):
    require(isinstance(value, dict), "Invalid " + label)
    return value


def package_id(value):
    require(isinstance(value, str) and PACKAGE_ID.fullmatch(value), "Invalid NuGet package ID")
    return value


def package_version(value):
    require(isinstance(value, str) and len(value) <= 100 and VERSION.fullmatch(value), "Invalid NuGet version")
    return value


def sha512(value):
    require(isinstance(value, str) and len(value) <= 100, "Invalid NuGet content hash")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ValueError("Invalid NuGet content hash") from error
    require(len(decoded) == 64, "Invalid NuGet content hash")
    return decoded.hex()


def runtime_requirements(value):
    require(
        isinstance(value, dict) and value.keys() == {"schemaVersion", "targetFramework", "runtimeDependencies"},
        "Invalid runtime dependency document",
    )
    require(
        type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, "Unsupported runtime dependency schema"
    )
    framework = value["targetFramework"]
    require(isinstance(framework, str) and re.fullmatch(r"net[0-9]{1,2}\.[0-9]", framework), "Invalid framework")
    entries = value["runtimeDependencies"]
    require(isinstance(entries, list) and len(entries) <= MAX_PACKAGES, "Invalid runtime dependency count")
    result = {}
    for entry in entries:
        require(
            isinstance(entry, dict) and entry.keys() == {"id", "version", "providedBy"}, "Invalid runtime dependency"
        )
        name = package_id(entry["id"])
        require(name.casefold() not in result and entry["providedBy"] == "jellyfin", "Invalid runtime dependency owner")
        result[name.casefold()] = package_version(entry["version"])
    return framework, result


def locked_packages(lock, framework):
    require(
        isinstance(lock, dict) and type(lock.get("version")) is int and lock["version"] == 1,
        "Unsupported NuGet lock schema",
    )
    frameworks = lock.get("dependencies")
    require(isinstance(frameworks, dict) and framework in frameworks, "Production framework absent from NuGet lock")
    packages = frameworks[framework]
    require(isinstance(packages, dict) and 0 < len(packages) <= MAX_PACKAGES, "Invalid NuGet lock package count")
    result = {}
    for name, entry in packages.items():
        package_id(name)
        require(name.casefold() not in result, "Duplicate NuGet package identity")
        require(
            isinstance(entry, dict) and entry.get("type") in ("Direct", "Transitive"), "Unsupported lock dependency"
        )
        dependencies = entry.get("dependencies", {})
        require(isinstance(dependencies, dict) and len(dependencies) <= MAX_PACKAGES, "Invalid NuGet dependency edges")
        result[name.casefold()] = {
            "id": name,
            "version": package_version(entry.get("resolved")),
            "sha512": sha512(entry.get("contentHash")),
            "dependencies": sorted((package_id(dep) for dep in dependencies), key=str.casefold),
        }
    for entry in result.values():
        require(all(dep.casefold() in result for dep in entry["dependencies"]), "Unresolved NuGet dependency edge")
        entry["dependencies"] = [result[dep.casefold()]["id"] for dep in entry["dependencies"]]
    return result


def verify_restored_graph(assets, framework, packages):
    require(isinstance(assets, dict) and assets.get("version") in (3, 4), "Unsupported NuGet assets schema")
    targets = mapping(assets.get("targets"), "restore targets").get(framework)
    libraries = assets.get("libraries")
    require(isinstance(targets, dict) and isinstance(libraries, dict), "Production restore graph absent")
    expected = {f"{entry['id']}/{entry['version']}".casefold(): entry for entry in packages.values()}
    actual = {key.casefold(): value for key, value in targets.items()}
    contents = {key.casefold(): value for key, value in libraries.items()}
    require(len(contents) == len(libraries), "Duplicate restored library identity")
    require(len(actual) == len(targets) and actual.keys() == expected.keys(), "Lock and restore package graphs differ")
    for key, entry in expected.items():
        target = mapping(actual[key], "restore target")
        library = mapping(contents.get(key), "restore library")
        require(target.get("type") == library.get("type") == "package", "Unexpected restored project dependency")
        require(sha512(library.get("sha512")) == entry["sha512"], "Lock and restored package hashes differ")
        dependencies = target.get("dependencies", {})
        require(isinstance(dependencies, dict), "Invalid restored dependency edges")
        require(
            {name.casefold() for name in dependencies} == {name.casefold() for name in entry["dependencies"]},
            "Lock and restored dependency edges differ",
        )


def from_restore(lock_path, assets_path, runtime_path, global_json_path):
    framework, runtime = runtime_requirements(read_json(runtime_path))
    lock = read_json(lock_path)
    packages = locked_packages(lock, framework)
    verify_restored_graph(read_json(assets_path), framework, packages)
    require(runtime.keys() <= packages.keys(), "Runtime dependency absent from production lock")
    # A direct reference to a server package that the runtime list omits would escape the floor rule.
    direct = {name.casefold() for name, entry in lock["dependencies"][framework].items() if entry["type"] == "Direct"}
    require(
        all(key in runtime for key in direct if key.startswith(HOST_FAMILY)),
        "Direct Jellyfin reference is not declared host-provided",
    )
    for key, entry in packages.items():
        require(key not in runtime or runtime[key] == entry["version"], "Runtime dependency version differs from lock")
        entry["hostProvided"] = key in runtime
    settings = mapping(read_json(global_json_path), "SDK document")
    sdk = mapping(settings.get("sdk"), "SDK settings").get("version")
    result = {
        "schemaVersion": 1,
        "targetFramework": framework,
        "sdkVersion": semver(sdk),
        "lockSha256": digest(json_bytes(lock)),
        "packages": sorted(packages.values(), key=lambda entry: entry["id"].casefold()),
    }
    return validate(result)


def validate(value):
    require(
        isinstance(value, dict)
        and value.keys() == {"schemaVersion", "targetFramework", "sdkVersion", "lockSha256", "packages"},
        "Invalid dependency inventory",
    )
    require(
        type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, "Unsupported dependency inventory schema"
    )
    require(
        isinstance(value["targetFramework"], str) and re.fullmatch(r"net[0-9]{1,2}\.[0-9]", value["targetFramework"]),
        "Invalid dependency framework",
    )
    semver(value["sdkVersion"])
    require(
        isinstance(value["lockSha256"], str) and re.fullmatch(r"[a-f0-9]{64}", value["lockSha256"]),
        "Invalid lock fingerprint",
    )
    packages = value["packages"]
    require(isinstance(packages, list) and 0 < len(packages) <= MAX_PACKAGES, "Invalid dependency inventory count")
    identities = {}
    for entry in packages:
        require(
            isinstance(entry, dict) and entry.keys() == {"id", "version", "sha512", "dependencies", "hostProvided"},
            "Invalid inventory package",
        )
        name = package_id(entry["id"])
        require(name.casefold() not in identities, "Duplicate inventory package")
        identities[name.casefold()] = name
        package_version(entry["version"])
        require(
            isinstance(entry["sha512"], str) and re.fullmatch(r"[a-f0-9]{128}", entry["sha512"]),
            "Invalid inventory package hash",
        )
        require(type(entry["hostProvided"]) is bool, "Invalid inventory package owner")
    validate_edges(packages, identities)
    return value


def require_floor(inventory, floor):
    """Hold every host-provided Jellyfin package at the release the target ABI names."""
    versions = {
        entry["id"].casefold(): entry["version"]
        for entry in inventory["packages"]
        if entry["hostProvided"] and entry["id"].casefold().startswith(HOST_FAMILY)
    }
    # A comparison with nothing to compare would pass on a renamed or undeclared package.
    require(HOST_REFERENCE in versions, "Jellyfin.Controller is not a declared host dependency")
    # An older server disables a plugin built against a newer release; a floor above the reference is untested.
    require(set(versions.values()) == {floor}, "Jellyfin reference differs from the target ABI floor")
    return inventory


def validate_edges(packages, identities):
    require(packages == sorted(packages, key=lambda entry: entry["id"].casefold()), "Noncanonical package order")
    for entry in packages:
        edges = entry["dependencies"]
        require(isinstance(edges, list) and len(edges) <= MAX_PACKAGES, "Invalid inventory dependency edges")
        for name in edges:
            package_id(name)
            require(identities.get(name.casefold()) == name, "Unresolved inventory dependency edge")
        require(edges == sorted(set(edges), key=str.casefold), "Noncanonical dependency edges")
