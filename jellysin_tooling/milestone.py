"""Milestone version alignment and release-please PR gating."""

import os
import re
from pathlib import Path

from .common import TOOL_NAME, ValidationError, read_json, require, semver

VERSION_MILESTONE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?=\s|$)")


def manifest_version(directory):
    manifest_path = Path(directory) / ".release-please-manifest.json"
    require(manifest_path.is_file(), "Missing .release-please-manifest.json")
    data = read_json(manifest_path)
    require(isinstance(data, dict), "Invalid release-please manifest")
    require("." in data, "Release-please manifest missing root version")
    version = data["."]
    require(isinstance(version, str), "Expected stable x.y.z version")
    semver(version)
    parts = [int(p) for p in version.split(".")]
    next_v = f"{parts[0]}.{parts[1] + 1}.0"
    return version, next_v, parts[2]


def parse_version_milestone(title):
    match = VERSION_MILESTONE.match(title)
    if not match:
        return None
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    require(major <= 65534 and minor <= 65534 and patch <= 65534, "Version exceeds assembly limit")
    return f"{major}.{minor}.{patch}"


def validate_milestone(item):
    require(isinstance(item, dict), "Invalid milestone object")
    require(isinstance(item.get("title"), str), "Invalid milestone title")
    require(isinstance(item.get("state"), str), "Invalid milestone state")


def validate_pull_request(pr):
    require(isinstance(pr, dict), "Invalid pull request object")
    base = pr.get("base")
    require(isinstance(base, dict) and isinstance(base.get("ref"), str), "Invalid pull request base")
    require(base["ref"] == "main", "Release pull request base must be main")
    labels = pr.get("labels")
    require(isinstance(labels, list), "Invalid pull request labels")
    names = set()
    for label in labels:
        require(isinstance(label, dict) and isinstance(label.get("name"), str), "Invalid pull request label")
        names.add(label["name"])
    return names


def fetch_milestones(client, repo, state):
    data = client.api(f"repos/{repo}/milestones?state={state}&per_page=100")
    require(isinstance(data, list), "Unexpected milestone listing")
    require(len(data) < 100, f"Milestone listing is unbounded: state={state}")
    for item in data:
        validate_milestone(item)
    return data


def fetch_release_pr_labels(client, repo, branch):
    owner = repo.split("/", 1)[0]
    data = client.api(f"repos/{repo}/pulls?state=open&head={owner}:{branch}&per_page=5")
    require(isinstance(data, list), "Unexpected pull request listing")
    require(len(data) == 1, "Expected exactly one open release pull request")
    return validate_pull_request(data[0])


def check_normal_mode(open_milestones, version, next_v):
    version_milestones = []
    for item in open_milestones:
        v = parse_version_milestone(item["title"])
        if v is not None:
            version_milestones.append((v, item["title"]))
    require(len(version_milestones) <= 1, "More than one open version milestone")
    if not version_milestones:
        print(f"{TOOL_NAME}: notice: zero open version milestones")
        return
    milestone_v, title = version_milestones[0]
    if milestone_v != next_v:
        raise ValidationError(f"Milestone '{title}' drifts from manifest {version}: the next milestone is {next_v}")


def check_release_mode(client, repo, branch, open_milestones, version, next_v, patch):
    open_v_milestones = []
    for item in open_milestones:
        v = parse_version_milestone(item["title"])
        if v is not None:
            if v not in (version, next_v):
                raise ValidationError(
                    f"Milestone '{item['title']}' drifts from manifest {version}: the next milestone is {next_v}"
                )
            open_v_milestones.append((v, item["title"]))
    versions = [v for v, _ in open_v_milestones]
    require(len(versions) == len(set(versions)), "More than one open version milestone")

    labels = fetch_release_pr_labels(client, repo, branch)
    if "release-ready" in labels:
        return

    waits_message = (
        f"Release {version} waits: close milestone {version} or add label release-ready, "
        f"then re-run CI (gh workflow run ci.yml --ref {branch})"
    )
    if patch != 0 or any(v == version for v, _ in open_v_milestones):
        raise ValidationError(waits_message)

    closed_milestones = fetch_milestones(client, repo, "closed")
    closed_has_v = any(parse_version_milestone(item["title"]) == version for item in closed_milestones)
    if not closed_has_v:
        raise ValidationError(waits_message)


def check(client, directory, repo, branch=None):
    # A tag run verifies an already merged release; milestone planning cannot hold back its artifacts.
    if os.environ.get("GITHUB_REF_TYPE", "").strip() == "tag":
        print(f"{TOOL_NAME}: notice: tag run, milestone check skipped")
        return
    if branch is None:
        head_ref = os.environ.get("GITHUB_HEAD_REF", "").strip()
        branch = head_ref if head_ref else os.environ.get("GITHUB_REF_NAME", "").strip()
    version, next_v, patch = manifest_version(directory)
    open_milestones = fetch_milestones(client, repo, "open")
    if branch.startswith("release-please--branches--"):
        check_release_mode(client, repo, branch, open_milestones, version, next_v, patch)
    else:
        check_normal_mode(open_milestones, version, next_v)
