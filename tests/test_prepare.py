"""Preparation creates the tag and one draft, repeats without writing, and refuses a conflict before any write."""

import json
import os
import subprocess
from functools import partial
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling import prepare
from jellysin_tooling.cli import parser, run
from jellysin_tooling.common import MAX_BODY, ValidationError
from jellysin_tooling.github import command
from tests.fixtures import COMMIT, FIRST, Workspace

REPO = FIRST["repository"]
TAG = "v1.2.3"
OTHER = "b" * 40
MAIN = "c" * 40
REFS = f"repos/{REPO}/git/refs"
RELEASES = f"repos/{REPO}/releases"
CHANGELOG = (
    "# Changelog\n\n"
    f"## [1.2.3](https://github.com/{REPO}/compare/v1.2.2...v1.2.3) (2026-10-11)\n\n"
    "### Features\n\n* prepare drafts\n\n"
    "## 1.2.2\n\n* older\n"
)
SECTION = "### Features\n\n* prepare drafts"


def release(identity=1, tag=TAG, **changes):
    remote = {
        "id": identity,
        "url": f"https://api.github.com/repos/{REPO}/releases/{identity}",
        "tag_name": tag,
        "draft": True,
        "prerelease": False,
        "immutable": False,
    }
    return {**remote, **changes}


class Remote:
    """GitHub state shared by the read client and the write runner."""

    def __init__(self, tags=None, releases=()):
        self.token = "synthetic-fixture-token"
        self.tags = dict(tags or {})
        self.releases = list(releases)
        self.listing = None
        self.reads = 0
        self.writes = []
        self.payloads = []
        self.refused = set()
        self.lost = set()
        self.lagging = False
        self.answer = {}

    def api(self, path):
        self.reads += 1
        prefix = f"repos/{REPO}/"
        assert path.startswith(prefix), path
        kind, _, name = path[len(prefix) :].rpartition("/")
        if kind == "git/matching-refs/tags":
            if self.listing is not None:
                return self.listing
            return [{"ref": f"refs/tags/{tag}"} for tag in sorted(self.tags) if tag.startswith(name)]
        if kind == "git/ref/tags":
            return {"object": {"type": "commit", "sha": self.tags[name]}}
        assert path == f"{RELEASES}?per_page=100&page=1", path
        return [] if self.lagging else list(self.releases)

    def run(self, arguments):
        endpoint = arguments[4]
        self.writes.append(arguments)
        if endpoint in self.refused:
            raise ValidationError("gh command failed (1)")
        if endpoint in self.lost:
            return
        if endpoint == REFS:
            fields = dict(argument.split("=", 1) for argument in arguments if "=" in argument)
            self.tags[fields["ref"].removeprefix("refs/tags/")] = fields["sha"]
            return
        assert endpoint == RELEASES, endpoint
        payload = json.loads(Path(arguments[6]).read_text(encoding="utf-8"))
        self.payloads.append(payload)
        self.releases.append(release(len(self.releases) + 1, payload["tag_name"]))
        return json.dumps({**self.releases[-1], **self.answer})

    def endpoints(self):
        return [arguments[4] for arguments in self.writes]


class Checkout:
    """The two git reads preparation performs, answered from fixed history."""

    def __init__(self):
        self.files = {f"{COMMIT}:version.txt": "1.2.3", f"{COMMIT}:CHANGELOG.md": CHANGELOG}
        self.ancestors = {(COMMIT, "refs/remotes/origin/main")}
        self.calls = []

    def __call__(self, arguments):
        self.calls.append(arguments)
        if arguments[:3] == ["git", "merge-base", "--is-ancestor"]:
            if tuple(arguments[3:]) not in self.ancestors:
                raise ValidationError("git command failed (1)")
            return ""
        assert arguments[:2] == ["git", "show"] and len(arguments) == 3, arguments
        if arguments[2] not in self.files:
            raise ValidationError("git command failed (128)")
        return self.files[arguments[2]]


class PrepareCase(Workspace):
    def setUp(self):
        super().setUp()
        self.git = Checkout()
        replaced = patch("jellysin_tooling.prepare.command", self.git)
        replaced.start()
        self.addCleanup(replaced.stop)

    def prepare(self, remote, tag=TAG, commit=COMMIT, **options):
        return prepare.prepare(remote, REPO, tag, commit, runner=remote.run, **options)

    def refused(self, remote, message, **options):
        tags, releases = dict(remote.tags), list(remote.releases)
        with self.assertRaisesRegex(ValidationError, message):
            self.prepare(remote, **options)
        self.assertEqual(([], tags, releases), (remote.writes, remote.tags, remote.releases))


class PreparationTests(PrepareCase):
    def test_creates_the_tag_then_one_draft_that_names_no_target(self):
        remote = Remote()
        draft = self.prepare(remote, changelog_path="CHANGELOG.md")
        self.assertEqual({TAG: COMMIT}, remote.tags)
        self.assertEqual(release(), draft)
        tag_write, draft_write = remote.writes
        self.assertEqual(
            ["gh", "api", "--method", "POST", REFS, "--raw-field", f"ref=refs/tags/{TAG}"]
            + ["--raw-field", f"sha={COMMIT}", "--silent"],
            tag_write,
        )
        self.assertEqual(["gh", "api", "--method", "POST", RELEASES, "--input"], draft_write[:6])
        self.assertEqual(7, len(draft_write), "the answer of the write is read, not silenced")
        self.assertFalse(Path(draft_write[6]).exists(), "request file outlives the call")
        expected = {"tag_name": TAG, "name": TAG, "draft": True, "prerelease": False, "body": SECTION}
        self.assertEqual([expected], remote.payloads)

    def test_draft_text_is_empty_without_a_changelog(self):
        remote = Remote()
        self.prepare(remote)
        self.assertEqual("", remote.payloads[0]["body"])

    def test_repeat_on_a_matching_draft_writes_nothing(self):
        existing = release(7, body="edited by a maintainer")
        remote = Remote({TAG: COMMIT}, [release(3, "v1.2.2", draft=False, immutable=True), existing])
        self.assertEqual(existing, self.prepare(remote, changelog_path="CHANGELOG.md"))
        self.assertEqual([], remote.writes)
        fresh = Remote()
        self.prepare(fresh)
        self.prepare(fresh)
        self.assertEqual([REFS, RELEASES], fresh.endpoints())

    def test_recovery_completes_only_the_missing_half(self):
        tagged = Remote({TAG: COMMIT})
        self.prepare(tagged)
        self.assertEqual([RELEASES], tagged.endpoints())
        drafted = Remote(releases=[release()])
        self.prepare(drafted)
        self.assertEqual(([REFS], {TAG: COMMIT}, [release()]), (drafted.endpoints(), drafted.tags, drafted.releases))

    def test_rerun_after_a_failed_draft_creation_keeps_the_tag_and_adds_the_draft(self):
        remote = Remote()
        remote.refused.add(RELEASES)
        with self.assertRaisesRegex(ValidationError, "gh command failed"):
            self.prepare(remote)
        self.assertEqual(({TAG: COMMIT}, []), (remote.tags, remote.releases))
        remote.refused.clear()
        self.prepare(remote)
        self.assertEqual([REFS, RELEASES, RELEASES], remote.endpoints())
        self.assertEqual([release()], remote.releases)

    def test_refused_tag_creation_creates_no_draft(self):
        remote = Remote()
        remote.refused.add(REFS)
        with self.assertRaises(ValidationError):
            self.prepare(remote)
        self.assertEqual(([REFS], []), (remote.endpoints(), remote.releases))

    def test_longer_tags_with_the_same_prefix_are_not_the_tag(self):
        neighbours = {"v1.2.30": OTHER, "v1.2.31": OTHER}
        remote = Remote(neighbours, [release(1, "v1.2.30", draft=False, immutable=True)])
        self.prepare(remote)
        self.assertEqual({**neighbours, TAG: COMMIT}, remote.tags)
        self.assertEqual([REFS, RELEASES], remote.endpoints())


class ConflictTests(PrepareCase):
    def test_published_immutable_or_ambiguous_releases_stop_before_any_write(self):
        cases = (
            ([release(draft=False)], "published release already uses"),
            ([release(draft=False, immutable=True)], "published release already uses"),
            ([release(immutable=True)], "immutable release already uses"),
            ([release(prerelease=True)], "Unexpected release state"),
            ([release(1), release(2)], "Multiple releases"),
        )
        for releases, message in cases:
            for tags in ({}, {TAG: COMMIT}):
                with self.subTest(message=message, tagged=bool(tags)):
                    self.refused(Remote(tags, releases), message)

    def test_tag_on_another_commit_stops_before_any_write(self):
        for releases in ([], [release()]):
            with self.subTest(drafts=len(releases)):
                self.refused(Remote({TAG: OTHER}, releases), "Tag already names another commit")

    def test_full_or_malformed_tag_listing_fails_closed(self):
        full = [{"ref": f"refs/tags/{TAG}{index}"} for index in range(prepare.TAG_PAGE)]
        for listing in (full, {}, "", [None], [{}], [{"ref": 1}]):
            with self.subTest(listing=str(listing)[:30]):
                remote = Remote()
                remote.listing = listing
                self.refused(remote, "tag listing")
        remote = Remote()
        remote.listing = full[:-1]
        self.prepare_unverified(remote)

    def prepare_unverified(self, remote):
        # The fixed listing hides the created tag, so only the read-back can object.
        with self.assertRaisesRegex(ValidationError, "Remote tag differs after preparation"):
            self.prepare(remote)
        self.assertEqual([REFS, RELEASES], remote.endpoints())

    def test_read_back_refuses_a_write_that_did_not_take_effect(self):
        for lost, message in ((REFS, "Remote tag differs after"), (RELEASES, "Draft release is absent after")):
            with self.subTest(lost=lost):
                remote = Remote()
                remote.lost.add(lost)
                with self.assertRaisesRegex(ValidationError, message):
                    self.prepare(remote)

    def test_created_draft_is_read_from_the_answer_of_the_write_not_from_a_lagging_listing(self):
        # Measured on run 38149612374: the listing did not show the draft one second after its creation.
        remote = Remote()
        remote.lagging = True
        self.assertEqual(release(), self.prepare(remote))
        self.assertEqual(([REFS, RELEASES], [release()]), (remote.endpoints(), remote.releases))

    def test_answer_of_the_write_must_be_the_unpublished_draft_of_this_tag(self):
        cases = [
            ({"draft": False}, "Created release is not a draft"),
            ({"immutable": True}, "Created release is not a draft"),
            ({"tag_name": "v1.2.4"}, "Release tag changed"),
            ({"id": 0}, "Invalid GitHub release ID"),
            ({"prerelease": True}, "Unexpected release state"),
            ({"url": f"https://api.github.com/repos/{FIRST['repository']}-fork/releases/1"}, "repository identity"),
        ]
        for answer, message in cases:
            with self.subTest(answer=answer):
                remote = Remote()
                remote.answer = answer
                with self.assertRaisesRegex(ValidationError, message):
                    self.prepare(remote)

    def test_answer_that_is_no_release_object_is_refused(self):
        def answering(remote, answer, arguments):
            remote.run(arguments)
            return answer

        for answer in ("[]", "{", '"text"'):
            with self.subTest(answer=answer):
                remote = Remote()
                with self.assertRaises(ValidationError):
                    prepare.prepare(remote, REPO, TAG, COMMIT, runner=partial(answering, remote, answer))

    def test_read_back_refuses_a_tag_that_another_writer_moved(self):
        remote = Remote()
        create = remote.run

        def raced(arguments):
            answer = create(arguments)
            remote.tags[TAG] = OTHER
            return answer

        with self.assertRaisesRegex(ValidationError, "Remote tag differs after"):
            prepare.prepare(remote, REPO, TAG, COMMIT, runner=raced)


class ApprovalTests(PrepareCase):
    def unread(self, message, **options):
        remote = Remote()
        self.refused(remote, message, **options)
        self.assertEqual(0, remote.reads)

    def test_tag_and_commit_are_validated_before_git_sees_them(self):
        for tag in ("1.2.3", "v1.2", "v1.2.3-rc1", "v01.2.3", "v1.2.3\n", None):
            with self.subTest(tag=tag):
                self.unread("Release tag must start with v|Expected stable x.y.z version", tag=tag)
        for commit in (COMMIT[:39], COMMIT.upper(), "--output=x", "main", None):
            with self.subTest(commit=commit):
                self.unread("Expected full lowercase commit SHA", commit=commit)
        self.assertEqual([], self.git.calls)

    def test_commit_must_be_on_main_and_record_the_tagged_version(self):
        self.git.files[f"{OTHER}:version.txt"] = "1.2.3"
        self.unread("git command failed", commit=OTHER)
        self.unread("Tag does not match the version file", tag="v1.2.4")
        self.git.files[f"{COMMIT}:version.txt"] = "1.2.3-rc1"
        self.unread("Tag does not match the version file")

    def test_repository_paths_cannot_leave_the_tree_or_become_options(self):
        deep = "/".join(9 * ["a"])
        for path in ("../version.txt", "/etc/version.txt", "a//version.txt", "-version.txt", deep, "", None, 7):
            with self.subTest(path=path):
                self.git.calls.clear()
                self.unread("Unsafe repository path|(Unsafe|Invalid) filename", version_path=path)
                self.assertFalse([call for call in self.git.calls if call[1] == "show"])
        self.unread("Unsafe repository path|(Unsafe|Invalid) filename", changelog_path="../CHANGELOG.md")
        self.git.files[f"{COMMIT}:src/plugin/version.txt"] = "1.2.3"
        self.prepare(Remote(), version_path="src/plugin/version.txt")

    def test_changelog_section_must_exist_and_stay_bounded_plain_text(self):
        key = f"{COMMIT}:CHANGELOG.md"
        self.git.files[key] = CHANGELOG.replace("[1.2.3]", "[1.2.4]")
        self.unread("Changelog has no section for the prepared version", changelog_path="CHANGELOG.md")
        self.git.files[key] = CHANGELOG.replace("* prepare drafts", "* tab\there")
        self.unread("Control character", changelog_path="CHANGELOG.md")
        self.git.files[key] = CHANGELOG.replace("* prepare drafts", "x" * MAX_BODY)
        self.unread("Invalid release notes", changelog_path="CHANGELOG.md")
        self.git.files[key] = CHANGELOG.replace("* prepare drafts", "x" * (MAX_BODY - len(SECTION) + 16))
        remote = Remote()
        self.prepare(remote, changelog_path="CHANGELOG.md")
        self.assertEqual(MAX_BODY, len(remote.payloads[0]["body"]))
        self.unread("git command failed", changelog_path="absent.md")

    def test_repository_name_is_validated_first(self):
        remote = Remote()
        with self.assertRaises(ValidationError):
            prepare.prepare(remote, "jellysin/../other", TAG, COMMIT, runner=remote.run)
        self.assertEqual((0, [], []), (remote.reads, remote.writes, self.git.calls))


class DispatchTests(PrepareCase):
    CONTEXT = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/prepare-release.yml@refs/heads/main",
        "GITHUB_SHA": MAIN,
    }

    def setUp(self):
        super().setUp()
        self.git.ancestors.add((COMMIT, MAIN))

    def test_manual_dispatch_of_the_approved_workflow_on_main_prepares(self):
        remote = Remote()
        with patch.dict(os.environ, self.CONTEXT):
            self.prepare(remote)
        self.assertEqual([REFS, RELEASES], remote.endpoints())
        self.assertIn(["git", "merge-base", "--is-ancestor", COMMIT, MAIN], self.git.calls)

    def test_any_other_context_is_refused_before_a_remote_read(self):
        workflows = f"{REPO}/.github/workflows"
        cases = (
            ("GITHUB_EVENT_NAME", "push", "manual workflow dispatch"),
            ("GITHUB_EVENT_NAME", None, "manual workflow dispatch"),
            ("GITHUB_REF", f"refs/tags/{TAG}", "must run from main"),
            ("GITHUB_REF", "refs/heads/feature", "must run from main"),
            ("GITHUB_REPOSITORY", "jellysin/cinema", "repository differs"),
            ("GITHUB_WORKFLOW_REF", f"{workflows}/release.yml@refs/heads/main", "Unapproved preparation workflow"),
            ("GITHUB_WORKFLOW_REF", f"{workflows}/prepare-release.yml@refs/heads/feature", "Unapproved preparation"),
            ("GITHUB_WORKFLOW_REF", "jellysin/cinema/.github/workflows/prepare-release.yml@refs/heads/main", "Unapp"),
            ("GITHUB_SHA", "main", "Invalid preparation workflow commit"),
            ("GITHUB_SHA", None, "Invalid preparation workflow commit"),
            ("GITHUB_SHA", OTHER, "git command failed"),
        )
        for name, value, message in cases:
            context = {key: text for key, text in {**self.CONTEXT, name: value}.items() if text is not None}
            with self.subTest(name=name, value=value), patch.dict(os.environ, context):
                os.environ.pop(name, None) if value is None else None
                remote = Remote()
                self.refused(remote, message)
                self.assertEqual(0, remote.reads)


class GitHistoryTests(Workspace):
    """Read a real history: object syntax, ancestry and file content come from git itself."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "version.txt").write_text("1.2.3\n", encoding="utf-8")
        (self.root / "version.txt").write_text("1.2.3\n", encoding="utf-8")
        (self.root / "CHANGELOG.md").write_bytes(CHANGELOG.replace("\n", "\r\n").encode())
        self.approved = self.commit("release")
        self.git("update-ref", "refs/remotes/origin/main", self.approved)
        (self.root / "version.txt").write_text("1.2.4\n", encoding="utf-8")
        self.unmerged = self.commit("unmerged")
        replaced = patch("jellysin_tooling.prepare.command", partial(command, cwd=self.root))
        replaced.start()
        self.addCleanup(replaced.stop)

    def git(self, *arguments):
        identity = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid"}
        environment = {
            **identity,
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "PATH": os.environ["PATH"],
        }
        result = subprocess.run(
            ["git", *arguments], cwd=self.root, env=environment, capture_output=True, check=True, timeout=30
        )
        return result.stdout.decode().strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_version_and_changelog_come_from_the_approved_commit_not_the_checkout(self):
        self.assertEqual("1.2.4", (self.root / "version.txt").read_text(encoding="utf-8").strip())
        remote = Remote()
        prepare.prepare(remote, REPO, TAG, self.approved, "version.txt", "CHANGELOG.md", runner=remote.run)
        self.assertEqual({TAG: self.approved}, remote.tags)
        self.assertEqual(SECTION, remote.payloads[0]["body"])
        self.assertEqual("1.2.3", prepare.approved_version(TAG, self.approved, "docs/version.txt"))

    def test_commit_outside_main_and_absent_files_are_refused(self):
        cases = (
            (self.unmerged, "v1.2.4", "version.txt"),
            (self.approved, TAG, "absent.txt"),
            (OTHER, TAG, "version.txt"),
        )
        for commit, tag, path in cases:
            remote = Remote()
            with self.subTest(path=path, tag=tag), self.assertRaisesRegex(ValidationError, "git command failed"):
                prepare.prepare(remote, REPO, tag, commit, path, runner=remote.run)
            self.assertEqual((0, []), (remote.reads, remote.writes))


class InterfaceTests(Workspace):
    def test_command_passes_every_option_and_the_real_runner(self):
        base = ["prepare", "--repo", REPO, "--tag", TAG, "--commit", COMMIT]
        cases = (
            ([], ("version.txt", None)),
            (
                ["--version-path", "src/version.txt", "--changelog-path", "CHANGELOG.md"],
                ("src/version.txt", "CHANGELOG.md"),
            ),
        )
        for options, expected in cases:
            with (
                self.subTest(options=len(options)),
                patch("jellysin_tooling.cli.GitHub") as client,
                patch("jellysin_tooling.cli.prepare.prepare") as prepared,
            ):
                run(parser().parse_args(base + options))
                prepared.assert_called_once_with(client.return_value, REPO, TAG, COMMIT, *expected)
        self.assertIs(command, prepare.prepare.__defaults__[-1])

    def test_action_passes_inputs_through_the_environment_only(self):
        action = Path(__file__).resolve().parents[1] / "actions" / "prepare" / "action.yml"
        script = action.read_text(encoding="utf-8").split("      run: |\n", 1)[1]
        self.assertNotIn("${{", script)
        self.assertIn('python "$TOOL_ROOT/tools.py" prepare "${args[@]}"', script)
        self.assertIn('--repo "$GITHUB_REPOSITORY" --tag "$RELEASE_TAG" --commit "$RELEASE_COMMIT"', script)
