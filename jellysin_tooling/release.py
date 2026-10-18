"""Exact-tag, resumable publication: upload missing bytes, never replace assets."""

import os
from pathlib import Path
from urllib.parse import quote

from . import spdx
from .catalog import release_assets, validate_archive, validate_release
from .common import MAX_ASSET, SHA, SIGNER_WORKFLOWS, decode_json, digest, read_json, repository, require, semver
from .github import command, verify_attestation


def exact_tag(tag, cwd=None):
    require(isinstance(tag, str) and tag.startswith("v"), "Release tag must start with v")
    semver(tag[1:])
    head = command(["git", "rev-parse", "HEAD"], cwd)
    require(SHA.fullmatch(head), "Invalid checkout commit")
    tagged = command(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], cwd)
    require(head == tagged, "Checkout does not match exact release tag")
    command(["git", "diff", "--exit-code", "HEAD", "--"], cwd)
    return head


def remote_tag(client, repo, tag):
    obj = client.api(f"repos/{repo}/git/ref/tags/{tag}").get("object", {})
    for _depth in range(5):
        require(isinstance(obj.get("sha"), str) and SHA.fullmatch(obj["sha"]), "Invalid remote tag object")
        if obj.get("type") == "commit":
            return obj["sha"]
        require(obj.get("type") == "tag", "Remote tag does not identify a commit")
        obj = client.api(f"repos/{repo}/git/tags/{obj['sha']}").get("object", {})
    raise ValueError("Annotated tag nesting exceeds limit")


def release_identity(remote, repo, tag, release_id=None):
    require(isinstance(remote, dict), "Invalid GitHub release response")
    identity = remote.get("id")
    require(type(identity) is int and identity > 0, "Invalid GitHub release ID")
    require(release_id is None or identity == release_id, "Release identity changed")
    require(remote.get("tag_name") == tag, "Release tag changed")
    require(remote.get("prerelease") is False and type(remote.get("draft")) is bool, "Unexpected release state")
    require(
        remote.get("url") == f"https://api.github.com/repos/{repo}/releases/{identity}",
        "Release repository identity changed",
    )
    return identity


def matching_releases(client, repo, tag):
    """Return the releases, draft or published, that carry the exact tag: none or one."""
    require(isinstance(client.token, str) and bool(client.token.strip()), "Authenticated release lookup is required")
    matches = []
    for page in range(1, 11):
        releases = client.api(f"repos/{repo}/releases?per_page=100&page={page}")
        require(isinstance(releases, list) and len(releases) <= 100, "Invalid GitHub releases response")
        for remote in releases:
            require(
                isinstance(remote, dict) and isinstance(remote.get("tag_name"), str), "Invalid GitHub release entry"
            )
            if remote["tag_name"] == tag:
                release_identity(remote, repo, tag)
                matches.append(remote)
                require(len(matches) <= 1, "Multiple releases match the exact tag")
        if len(releases) < 100:
            return matches
    raise ValueError("Release history exceeds 1000-entry scan limit")


def find_release(client, repo, tag):
    matches = matching_releases(client, repo, tag)
    require(len(matches) == 1, "No visible release matches the exact tag")
    return matches[0]


def read_release(client, repo, tag, release_id):
    remote = client.api(f"repos/{repo}/releases/{release_id}")
    release_identity(remote, repo, tag, release_id)
    return remote


def publication_context(repo, tag, commit, verified_tag_recovery):
    if verified_tag_recovery:
        require(os.environ.get("GITHUB_ACTIONS") == "true", "Recovery requires a GitHub Actions workflow")
        require(
            os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Recovery requires manual workflow dispatch"
        )
        require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Recovery must run from main")
        require(os.environ.get("GITHUB_REPOSITORY") == repo, "Recovery repository differs")
        expected = f"{repo}/.github/workflows/recover-release.yml@refs/heads/main"
        require(os.environ.get("GITHUB_WORKFLOW_REF") == expected, "Unapproved recovery workflow")
        workflow_commit = os.environ.get("GITHUB_SHA", "")
        require(SHA.fullmatch(workflow_commit), "Invalid recovery workflow commit")
        require(os.environ.get("GITHUB_WORKFLOW_SHA") == workflow_commit, "Recovery workflow identity differs")
        command(["git", "merge-base", "--is-ancestor", commit, workflow_commit])
        command(["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"])
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        require(os.environ.get("GITHUB_REF") == "refs/tags/" + tag, "Publication must run on the tag ref")
        require(
            os.environ.get("GITHUB_REPOSITORY") == repo and os.environ.get("GITHUB_SHA") == commit,
            "Workflow source differs from release",
        )


def local_artifacts(directory, release):
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
    members = validate_archive(files[release["archive"]["name"]], release)
    spdx.validate(decode_json(files["sbom.spdx.json"]), release, members)
    checksums = "".join(
        f"{digest(data)}  {name}\n" for name, data in sorted(files.items()) if name != "checksums.txt"
    ).encode()
    require(files["checksums.txt"] == checksums, "Local release checksum manifest mismatch")
    return files


def reconcile_assets(client, remote, files, repo, tag):
    assets = release_assets(remote, repo, tag, authenticated_draft=remote.get("draft") is True)
    require(assets.keys() <= files.keys(), "Unexpected assets on release")
    missing = []
    for name, data in sorted(files.items()):
        if name not in assets:
            require(remote.get("draft") is True, "Published release is incomplete; do not mutate it")
            missing.append(name)
            continue
        require(assets[name]["size"] == len(data), "Existing asset differs; use a new release version")
        remote_bytes = client.download_asset(repo, assets[name]["id"])
        require(remote_bytes == data, "Existing asset differs; use a new release version")
    return missing


def draft_starters(remote, files, repo, tag):
    assets = remote.get("assets")
    require(isinstance(assets, list) and len(assets) <= 32, "Invalid release assets")
    pending = []
    uploaded = []
    for asset in assets:
        require(isinstance(asset, dict), "Invalid release asset")
        if asset.get("state") != "starter":
            uploaded.append(asset)
            continue
        require(remote.get("draft") is True, "Never remove a published release asset")
        require(
            asset.get("name") in files and type(asset.get("size")) is int and asset["size"] == 0,
            "Unexpected incomplete draft asset",
        )
        require(type(asset.get("id")) is int and asset["id"] > 0, "Invalid incomplete asset ID")
        release_assets(
            {**remote, "assets": [{**asset, "state": "uploaded", "size": 1}]}, repo, tag, authenticated_draft=True
        )
        pending.append(asset)
    require(len({asset["name"] for asset in assets}) == len(assets), "Duplicate release asset")
    return {**remote, "assets": uploaded}, pending


def remove_draft_starters(client, pending, repo, tag, release_id, runner):
    for asset in pending:
        current = read_release(client, repo, tag, release_id)
        require(current.get("draft") is True, "Release is no longer a draft")
        matches = [entry for entry in current.get("assets", []) if entry.get("id") == asset["id"]]
        require(len(matches) == 1 and matches[0] == asset, "Incomplete draft asset changed before recovery")
        # GitHub documents zero-byte starter assets after failed uploads as safe to delete.
        runner(["gh", "api", "--method", "DELETE", f"repos/{repo}/releases/assets/{asset['id']}"])


def upload_missing(client, directory, files, missing, repo, tag, release_id, runner):
    for name in missing:
        current = read_release(client, repo, tag, release_id)
        require(current.get("draft") is True, "Release is no longer a draft")
        if name not in reconcile_assets(client, current, files, repo, tag):
            continue
        endpoint = f"https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={quote(name, safe='')}"
        runner(
            [
                "gh",
                "api",
                "--method",
                "POST",
                endpoint,
                "--input",
                str(Path(directory) / name),
                "--header",
                "Content-Type: application/octet-stream",
                "--header",
                f"Content-Length: {len(files[name])}",
                "--silent",
            ]
        )


def publish(
    client,
    directory,
    repo,
    tag,
    workflow,
    verifier=verify_attestation,
    runner=command,
    *,
    signer_workflow=".github/workflows/release.yml",
    verified_tag_recovery=False,
):
    repository(repo)
    require(workflow == ".github/workflows/release.yml", "Unapproved publisher workflow")
    require(signer_workflow in SIGNER_WORKFLOWS, "Unapproved signer workflow")
    commit = exact_tag(tag)
    publication_context(repo, tag, commit, verified_tag_recovery)
    release = read_json(Path(directory) / "release.json")
    approved = {"repository": repo, "guid": release.get("plugin", {}).get("guid")}
    validate_release(release, approved, tag)
    require(release["commit"] == commit == remote_tag(client, repo, tag), "Local, remote and metadata commits differ")
    files = local_artifacts(directory, release)
    for name in sorted(files):
        verifier(Path(directory) / name, repo, signer_workflow, commit, tag)
    found = find_release(client, repo, tag)
    release_id = found["id"]
    remote = read_release(client, repo, tag, release_id)
    remote, pending = draft_starters(remote, files, repo, tag)
    missing = reconcile_assets(client, remote, files, repo, tag)
    remove_draft_starters(client, pending, repo, tag, release_id, runner)
    upload_missing(client, directory, files, missing, repo, tag, release_id, runner)
    refreshed = read_release(client, repo, tag, release_id)
    require(not reconcile_assets(client, refreshed, files, repo, tag), "Release upload is incomplete")
    if refreshed.get("draft") is True:
        require(remote_tag(client, repo, tag) == commit, "Remote tag changed before publication")
        runner(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/releases/{release_id}",
                "--field",
                "draft=false",
                "--raw-field",
                "make_latest=true",
                "--silent",
            ]
        )
    published = read_release(client, repo, tag, release_id)
    require(
        published.get("draft") is False and published.get("immutable") is True,
        "Published release must be immutable; enable repository immutable releases before publishing",
    )
    return release
