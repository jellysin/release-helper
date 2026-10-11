"""Command line interface shared by local checks and composite Actions."""

import argparse
import os
import sys
from pathlib import Path

from . import catalog, changelog, inventory, package, policy, prepare, release, servercheck
from .common import TOOL_NAME, ValidationError, metadata, read_json, require
from .github import GitHub, command


def parser():
    root = argparse.ArgumentParser(description="JellySin deterministic releases and verified catalog updates")
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("package")
    build.add_argument("--publish-directory", required=True)
    build.add_argument("--metadata-path", default="plugin.json")
    build.add_argument("--version-path", default="version.txt")
    build.add_argument("--output-directory", default="dist")
    build.add_argument("--tag")
    build.add_argument("--nuget-lock-path")
    build.add_argument("--nuget-assets-path")
    build.add_argument("--runtime-dependencies-path")
    build.add_argument("--global-json-path", default="global.json")
    build.add_argument("--changelog-path")
    publish = commands.add_parser("publish")
    publish.add_argument("--directory", default="dist")
    publish.add_argument("--repo", required=True)
    publish.add_argument("--tag", required=True)
    publish.add_argument("--workflow", default=".github/workflows/release.yml")
    publish.add_argument("--verified-tag-recovery", action="store_true")
    draft = commands.add_parser("prepare")
    draft.add_argument("--repo", required=True)
    draft.add_argument("--tag", required=True)
    draft.add_argument("--commit", required=True)
    draft.add_argument("--version-path", default="version.txt")
    draft.add_argument("--changelog-path")
    update = commands.add_parser("catalog")
    update.add_argument("--manifest", default="manifest.json")
    update.add_argument("--allowlist", default="plugins.json")
    validate = commands.add_parser("validate-catalog")
    validate.add_argument("--manifest", default="manifest.json")
    validate.add_argument("--allowlist", default="plugins.json")
    server = commands.add_parser("server-check")
    source = server.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--emit-fixture", nargs=2, metavar=("KIND", "PATH"))
    server.add_argument("--image")
    check = commands.add_parser("check-policy")
    check.add_argument("--directory", default=".")
    return root


def release_changelog(arguments, version, repo):
    if not arguments.changelog_path:
        return None
    notes = changelog.notes(arguments.changelog_path, version, repo)
    # An untagged packaging check may run before the release pull request writes the section.
    require(notes is not None or not arguments.tag, "Changelog has no section for the tagged version")
    return notes


def run_package(arguments):
    commit = release.exact_tag(arguments.tag) if arguments.tag else command(["git", "rev-parse", "HEAD"])
    created = command(["git", "show", "-s", "--format=%cI", commit])
    version = Path(arguments.version_path).read_text(encoding="utf-8").strip()
    require(not arguments.tag or arguments.tag == "v" + version, "Tag does not match version.txt")
    inventory_paths = (arguments.nuget_lock_path, arguments.nuget_assets_path, arguments.runtime_dependencies_path)
    require(all(inventory_paths) or not any(inventory_paths), "Provide all three dependency inventory inputs")
    dependencies = (
        inventory.from_restore(*inventory_paths, arguments.global_json_path) if all(inventory_paths) else None
    )
    info = metadata(read_json(arguments.metadata_path))
    result = package.build(
        arguments.publish_directory,
        info,
        version,
        commit,
        created,
        arguments.output_directory,
        dependencies,
        release_changelog(arguments, version, info["repository"]),
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"archive={Path(arguments.output_directory).resolve() / result['archive']['name']}\n")
            stream.write(f"directory={Path(arguments.output_directory).resolve()}\n")


def run_server_check(arguments):
    if arguments.emit_fixture:
        require(arguments.image is None, "A fixture is written without a server image")
        servercheck.emit_fixture(*arguments.emit_fixture)
        return
    servercheck.check(arguments.manifest, arguments.image)


def run(arguments):
    if arguments.command == "check-policy":
        policy.check_repository(arguments.directory)
        return
    if arguments.command == "package":
        run_package(arguments)
        return
    if arguments.command == "server-check":
        run_server_check(arguments)
        return
    if arguments.command == "prepare":
        prepare.prepare(
            GitHub(),
            arguments.repo,
            arguments.tag,
            arguments.commit,
            arguments.version_path,
            arguments.changelog_path,
        )
        return
    if arguments.command == "publish":
        release.publish(
            GitHub(),
            arguments.directory,
            arguments.repo,
            arguments.tag,
            arguments.workflow,
            verified_tag_recovery=arguments.verified_tag_recovery,
        )
        return
    current = catalog.validate_manifest(read_json(arguments.manifest))
    approved = catalog.allowlist(read_json(arguments.allowlist))
    require(
        {plugin["guid"] for plugin in current} <= {plugin["guid"] for plugin in approved},
        "Manifest contains an unapproved plugin",
    )
    repositories = {plugin["guid"]: plugin["repository"] for plugin in approved}
    for plugin in current:
        prefix = f"https://github.com/{repositories[plugin['guid']]}/releases/download/"
        require(
            all(version["sourceUrl"].startswith(prefix) for version in plugin["versions"]),
            "Catalog source differs from approved repository",
        )
    if arguments.command == "catalog":
        merged = catalog.collect(GitHub(), approved, current)
        catalog.update(arguments.manifest, current, merged)


def main():
    try:
        run(parser().parse_args())
    except ValidationError as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Validations contain controlled labels, never raw remote values or credential data.
        print(f"{TOOL_NAME}: {type(exc).__name__}: operation failed validation or I/O", file=sys.stderr)
        raise SystemExit(1) from None
