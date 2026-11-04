"""Exact-tag, resumable publication: upload missing bytes, never replace assets."""

import base64
import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from . import source, spdx
from .catalog import release_assets, validate_archive, validate_release
from .common import (
    MAX_ASSET,
    MAX_BUNDLE,
    MAX_FILES,
    SHA,
    SIGNER_WORKFLOWS,
    ValidationError,
    decode_json,
    digest,
    read_json,
    repository,
    require,
    semver,
)
from .github import command, verify_attestation


@dataclass(frozen=True)
class _ReleaseContext:
    """Keep the release identity and approved signing runner together across publication retries."""

    repo: str
    tag: str
    release_id: int
    signer_workflow: str = ".github/workflows/release.yml"
    runner: Callable = command


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


def reconcile_assets(client, remote, files, context):
    """Check artifact equality, then verify every retained bundle's digest and exact signer identity."""
    require(len(files) <= MAX_FILES, "Too many local release artifacts")
    assets = release_assets(remote, context.repo, context.tag, authenticated_draft=remote.get("draft") is True)
    require(assets.keys() <= files.keys(), "Unexpected assets on release")
    missing = []
    bundles = {}
    for name, data in sorted(files.items()):
        if name not in assets:
            require(remote.get("draft") is True, "Published release is incomplete; do not mutate it")
            missing.append(name)
            continue
        bundle = name.endswith(".sigstore.json")
        require(assets[name]["size"] == len(data) or bundle, "Existing asset differs; use a new release version")
        if bundle:
            require(assets[name]["size"] <= MAX_BUNDLE, f"Signature bundle exceeds size limit: {name}")
        remote_bytes = client.download_asset(context.repo, assets[name]["id"])
        require(len(remote_bytes) == assets[name]["size"], "Existing asset differs; use a new release version")
        if bundle:
            bundles[name] = remote_bytes
        else:
            require(remote_bytes == data, "Existing asset differs; use a new release version")
    for name, remote_bytes in bundles.items():
        asset_name = name.removesuffix(".sigstore.json")
        require(asset_name in files, f"Unexpected signature bundle: {name}")
        if asset_name in assets:
            signed = assets[asset_name]
            require(
                signed["size"] == len(files[asset_name])
                and client.download_asset(context.repo, signed["id"]) == files[asset_name],
                "Existing asset differs; use a new release version",
            )
        verify_uploaded_bundle(remote_bytes, name, files[asset_name], context)
        files[name] = remote_bytes
    return missing


def verify_uploaded_bundle(bundle_bytes, bundle_name, asset_bytes, context):
    """Verify the retained bundle against the local artifact and exact approved workflow's tag identity."""
    require(context.signer_workflow in SIGNER_WORKFLOWS, "Unapproved signer workflow")
    validate_bundle_bytes(bundle_bytes, bundle_name, asset_bytes)
    try:
        with tempfile.TemporaryDirectory(prefix="jellysin-bundle-") as temporary:
            bundle_path, asset_path = Path(temporary) / "bundle.sigstore.json", Path(temporary) / "artifact"
            bundle_path.write_bytes(bundle_bytes)
            asset_path.write_bytes(asset_bytes)
            context.runner(
                [
                    "cosign",
                    "verify-blob",
                    "--bundle",
                    str(bundle_path),
                    "--certificate-identity",
                    f"https://github.com/{context.repo}/{context.signer_workflow}@refs/tags/{context.tag}",
                    "--certificate-oidc-issuer",
                    "https://token.actions.githubusercontent.com",
                    str(asset_path),
                ],
                timeout=120,
            )
    except (OSError, ValidationError) as exc:
        raise ValidationError(f"cosign verification unavailable or failed for uploaded bundle: {bundle_name}") from exc


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


def upload_missing(client, directory, files, missing, context):
    """Reconcile the exact draft before each missing upload, using the approved bundle signer on retries."""
    for name in missing:
        current = read_release(client, context.repo, context.tag, context.release_id)
        require(current.get("draft") is True, "Release is no longer a draft")
        if name not in reconcile_assets(client, current, files, context):
            continue
        endpoint = (
            f"https://uploads.github.com/repos/{context.repo}/releases/{context.release_id}/assets"
            f"?name={quote(name, safe='')}"
        )
        context.runner(
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


def plugin_artifacts(client, directory, repo, tag, commit):
    release = read_json(Path(directory) / "release.json")
    approved = {"repository": repo, "guid": release.get("plugin", {}).get("guid")}
    validate_release(release, approved, tag)
    require(release["commit"] == commit == remote_tag(client, repo, tag), "Local, remote and metadata commits differ")
    return local_artifacts(directory, release)


def source_artifacts(client, directory, repo, tag, commit):
    release = read_json(Path(directory) / "release.json")
    source.validate_release(release, repo, tag)
    require(release["commit"] == commit == remote_tag(client, repo, tag), "Local, remote and metadata commits differ")
    return source.local_artifacts(directory, release)


def validate_bundle(bundle_path, bundle_name, asset_bytes):
    """Read a bounded regular bundle file and apply the shared structural and artifact digest checks."""
    require(bundle_path.exists() or bundle_path.is_symlink(), f"Missing signature bundle: {bundle_name}")
    require(not bundle_path.is_symlink(), f"Symlink signature bundle refused: {bundle_name}")
    require(bundle_path.is_file(), f"Missing or unsafe signature bundle: {bundle_name}")
    require(bundle_path.stat().st_size <= MAX_BUNDLE, f"Signature bundle exceeds size limit: {bundle_name}")
    bundle_bytes = bundle_path.read_bytes()
    return validate_bundle_bytes(bundle_bytes, bundle_name, asset_bytes)


def validate_bundle_bytes(bundle_bytes, bundle_name, asset_bytes):
    """Validate local or downloaded Sigstore bundle bytes against the exact artifact's SHA-256 digest."""
    require(len(bundle_bytes) <= MAX_BUNDLE, f"Signature bundle exceeds size limit: {bundle_name}")
    try:
        bundle = decode_json(bundle_bytes, limit=MAX_BUNDLE)
    except ValidationError as exc:
        raise ValidationError(f"Invalid JSON in signature bundle: {bundle_name}") from exc
    require(isinstance(bundle, dict), f"Signature bundle must be a JSON object: {bundle_name}")
    require(
        bundle.get("mediaType") == "application/vnd.dev.sigstore.bundle.v0.3+json",
        f"Invalid signature bundle mediaType: {bundle_name}",
    )
    message_sig = bundle.get("messageSignature")
    require(isinstance(message_sig, dict), f"Invalid signature bundle messageSignature: {bundle_name}")
    message_digest = message_sig.get("messageDigest")
    require(isinstance(message_digest, dict), f"Invalid signature bundle messageDigest: {bundle_name}")
    require(
        message_digest.get("algorithm") == "SHA2_256",
        f"Invalid signature bundle algorithm: {bundle_name}",
    )
    expected_digest = base64.b64encode(hashlib.sha256(asset_bytes).digest()).decode("ascii")
    require(
        message_digest.get("digest") == expected_digest,
        f"Signature bundle digest mismatch: {bundle_name}",
    )
    return bundle_bytes


def validate_bundles(directory, files):
    root = Path(directory).resolve(strict=True)
    suffix = ".sigstore.json"
    entries = []
    for item in root.iterdir():
        entries.append(item)
        require(len(entries) <= MAX_FILES, "Release directory holds too many files")
    for item in sorted(entries):
        if item.name.endswith(suffix):
            asset_name = item.name[: -len(suffix)]
            require(asset_name in files, f"Unexpected signature bundle: {item.name}")
    bundles = {}
    for asset_name in sorted(files):
        bundle_name = f"{asset_name}{suffix}"
        bundle_path = root / bundle_name
        bundles[bundle_name] = validate_bundle(bundle_path, bundle_name, files[asset_name])
    return bundles


def attested_artifacts(directory, files, repo, tag, signer_workflow, commit, verifier):
    """Verify each artifact's attestation and collect bundles for the approved signed-build workflow."""
    for name in sorted(files):
        verifier(Path(directory) / name, repo, signer_workflow, commit, tag)
    if signer_workflow == ".github/workflows/build-signed.yml":
        files = {**files, **validate_bundles(directory, files)}
    return files


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
    kind="plugin",
):
    """Verify exact-tag artifacts and retained bundles before resuming uploads and immutable publication."""
    require(kind in ("plugin", "source"), f"Unknown release kind: {kind}")
    repository(repo)
    require(workflow == ".github/workflows/release.yml", "Unapproved publisher workflow")
    require(signer_workflow in SIGNER_WORKFLOWS, "Unapproved signer workflow")
    commit = exact_tag(tag)
    publication_context(repo, tag, commit, verified_tag_recovery)
    if kind == "plugin":
        files = plugin_artifacts(client, directory, repo, tag, commit)
    else:
        files = source_artifacts(client, directory, repo, tag, commit)
    files = attested_artifacts(directory, files, repo, tag, signer_workflow, commit, verifier)
    found = find_release(client, repo, tag)
    release_id = found["id"]
    context = _ReleaseContext(repo, tag, release_id, signer_workflow, runner)
    remote = read_release(client, repo, tag, release_id)
    remote, pending = draft_starters(remote, files, repo, tag)
    missing = reconcile_assets(client, remote, files, context)
    remove_draft_starters(client, pending, repo, tag, release_id, runner)
    upload_missing(client, directory, files, missing, context)
    refreshed = read_release(client, repo, tag, release_id)
    require(
        not reconcile_assets(client, refreshed, files, context),
        "Release upload is incomplete",
    )
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
    return decode_json(files["release.json"])
