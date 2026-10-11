"""Create the exact tag and one unpublished draft that publication reconciles later; never publish."""

import os
import tempfile
from pathlib import Path

from . import changelog
from .common import MAX_BODY, SHA, filename, json_bytes, release_notes, repository, require, semver
from .github import command
from .release import matching_releases, remote_tag

WORKFLOW = ".github/workflows/prepare-release.yml"
# The listing documents no page size; a full default page may hide the exact tag behind longer names.
TAG_PAGE = 30


def tracked(commit, path):
    """Read a repository file as the approved commit records it, not as the checkout holds it."""
    parts = path.split("/") if isinstance(path, str) else []
    require(0 < len(parts) <= 8, "Unsafe repository path")
    for part in parts:
        filename(part)
    return command(["git", "show", f"{commit}:{path}"])


def approved_version(tag, commit, version_path):
    require(isinstance(tag, str) and tag.startswith("v"), "Release tag must start with v")
    version = semver(tag[1:])
    require(isinstance(commit, str) and SHA.fullmatch(commit), "Expected full lowercase commit SHA")
    command(["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"])
    require(tracked(commit, version_path) == version, "Tag does not match the version file of the approved commit")
    return version


def dispatch_context(repo, commit):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Preparation requires manual workflow dispatch")
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Preparation must run from main")
    require(os.environ.get("GITHUB_REPOSITORY") == repo, "Preparation repository differs")
    expected = f"{repo}/{WORKFLOW}@refs/heads/main"
    require(os.environ.get("GITHUB_WORKFLOW_REF") == expected, "Unapproved preparation workflow")
    workflow_commit = os.environ.get("GITHUB_SHA", "")
    require(SHA.fullmatch(workflow_commit), "Invalid preparation workflow commit")
    command(["git", "merge-base", "--is-ancestor", commit, workflow_commit])


def release_body(commit, changelog_path, version):
    if not changelog_path:
        return ""
    lines = changelog.section(tracked(commit, changelog_path), version)
    require(lines is not None, "Changelog has no section for the prepared version")
    return release_notes("\n".join(lines), MAX_BODY)


def tagged_commit(client, repo, tag):
    """Return the commit the remote tag names, or None when the listing shows no such tag."""
    refs = client.api(f"repos/{repo}/git/matching-refs/tags/{tag}")
    require(isinstance(refs, list) and len(refs) < TAG_PAGE, "Invalid or ambiguous tag listing")
    require(all(isinstance(ref, dict) and isinstance(ref.get("ref"), str) for ref in refs), "Invalid tag listing entry")
    if all(ref["ref"] != "refs/tags/" + tag for ref in refs):
        return None
    return remote_tag(client, repo, tag)


def unpublished(client, repo, tag):
    """Return the draft that already carries the tag, or None; refuse every published state."""
    matches = matching_releases(client, repo, tag)
    for remote in matches:
        require(remote["draft"] is True, "A published release already uses this tag")
        require(remote.get("immutable") is not True, "An immutable release already uses this tag")
    return matches[0] if matches else None


def create_draft(repo, tag, body, runner):
    # No target_commitish: the tag exists by now, and the built-in token is refused for a named
    # commit whose workflow files differ from the default branch.
    payload = {"tag_name": tag, "name": tag, "draft": True, "prerelease": False, "body": body}
    with tempfile.TemporaryDirectory(prefix="jellysin-prepare-") as temporary:
        path = Path(temporary) / "release.json"
        path.write_bytes(json_bytes(payload))
        runner(["gh", "api", "--method", "POST", f"repos/{repo}/releases", "--input", str(path), "--silent"])


def prepare(client, repo, tag, commit, version_path="version.txt", changelog_path=None, runner=command):
    repository(repo)
    version = approved_version(tag, commit, version_path)
    dispatch_context(repo, commit)
    body = release_body(commit, changelog_path, version)
    # Read both remote states before the first write: a conflict must leave nothing behind.
    draft = unpublished(client, repo, tag)
    existing = tagged_commit(client, repo, tag)
    require(existing in (None, commit), "Tag already names another commit")
    if existing is None:
        reference = ["--raw-field", f"ref=refs/tags/{tag}", "--raw-field", f"sha={commit}"]
        runner(["gh", "api", "--method", "POST", f"repos/{repo}/git/refs", *reference, "--silent"])
    if draft is None:
        create_draft(repo, tag, body, runner)
    require(tagged_commit(client, repo, tag) == commit, "Remote tag differs after preparation")
    draft = unpublished(client, repo, tag)
    require(draft is not None, "Draft release is absent after preparation")
    return draft
