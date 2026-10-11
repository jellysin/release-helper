"""Run the reusable workflow's inline programs as the workflow does, against a stand-in gh."""

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from tests.stub_gh import PENDING, SECRET, TAGGED

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-please.yml"
STUB = Path(__file__).with_name("stub_gh.py")
LABELS = "Mark merged release PRs whose tag names their merge commit"
DISPATCH = "Explicitly dispatch release PR checks"
INTERPRETER = "Refuse an interpreter other than the pinned one"
CHECKOUT = "      - uses: actions/checkout@"
SETUP = "      - uses: actions/setup-python@"
ACTION = "      - uses: googleapis/release-please-action@"
START = "          python3 - <<'PY'\n"
REPO = "jellysin/plugin-lastfm"
MERGE, OTHER, ANNOTATION = "a" * 40, "b" * 40, "c" * 40
BRANCH = "release-please--branches--main--components--plugin"
RELEASE_PR = {"headBranchName": "release-please--branches--main", "baseBranchName": "main"}


def source(step):
    """Return the inline program of the named step, exactly as the workflow file carries it."""
    tail = WORKFLOW.read_text(encoding="utf-8").split(f"      - name: {step}\n", 1)[1]
    return dedent(tail.split(START, 1)[1].split("          PY\n", 1)[0])


def pull(number=7, version="1.2.3", **changes):
    merged = {"title": f"chore(main): release {version}", "merged": True, "merge_commit_sha": MERGE}
    return str(number), {**merged, "head": {"ref": BRANCH}, "base": {"ref": "main"}, "labels": [PENDING], **changes}


def lightweight(sha=MERGE):
    return {"type": "commit", "sha": sha}


def forge(pulls=(), tags=None, **changes):
    state = {"repo": REPO, "pulls": dict(pulls), "tags": dict(tags or {}), "annotations": {}, "issues": []}
    return {**state, "listing": None, "fail": None, "calls": [], "writes": [], **changes}


@unittest.skipIf(os.name == "nt", "the stand-in gh is a POSIX shell script")
class StepCase(unittest.TestCase):
    step = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jellysin-workflow-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        gh = self.directory / "gh"
        gh.write_text(f'#!/bin/sh\n"{sys.executable}" "{STUB}" "$@"\n', encoding="utf-8")
        gh.chmod(0o755)

    def execute(self, state, **environment):
        """Run the step's program on standard input; the stand-in is the only command on PATH."""
        path = self.directory / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        environment = {
            "PATH": str(self.directory),
            "GH_STUB_STATE": str(path),
            "GITHUB_REPOSITORY": REPO,
            **environment,
        }
        done = subprocess.run(
            [sys.executable, "-"],
            input=source(self.step),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        state.update(json.loads(path.read_text(encoding="utf-8")))
        return done

    def passed(self, state, **environment):
        done = self.execute(state, **environment)
        self.assertEqual((0, ""), (done.returncode, done.stderr))
        return done.stdout.splitlines()

    def failed(self, state, message, **environment):
        done = self.execute(state, **environment)
        self.assertNotEqual(0, done.returncode)
        self.assertIn(message, done.stderr)
        self.assertNotIn(SECRET, done.stdout + done.stderr.replace("stub gh", ""))
        return done


class ReleasePrWorkflowTests(StepCase):
    step = DISPATCH

    def dispatch(self, prs, **changes):
        state = forge(**changes)
        # A stale caller environment must not enable the removed publication path.
        stale = {"RELEASE_TAG": "v1.0.0", "PUBLISH_RELEASE": "true", "PUBLICATION_WORKFLOW": "release.yml"}
        return state, self.execute(state, RELEASE_PRS=json.dumps(prs), **stale)

    def test_no_pull_requests_never_dispatches_publication(self):
        state, done = self.dispatch([])
        self.assertEqual((0, []), (done.returncode, state["calls"]))

    def test_dispatches_only_ci_for_returned_release_pr_branches(self):
        branches = ["release-please--branches--main", "release-please--branches--main--components--second"]
        state, done = self.dispatch([{"headBranchName": branch, "baseBranchName": "main"} for branch in branches])
        self.assertEqual(0, done.returncode)
        expected = [["workflow", "run", "ci.yml", "--repo", REPO, "--ref", branch] for branch in branches]
        self.assertEqual(expected, state["calls"])

    def test_accepts_at_most_twenty_release_prs(self):
        state, done = self.dispatch([RELEASE_PR] * 20)
        self.assertEqual((0, 20), (done.returncode, len(state["calls"])))
        state, done = self.dispatch([RELEASE_PR] * 21)
        self.assertIn("Unexpected release PR output", done.stderr)
        self.assertEqual((1, []), (done.returncode, state["calls"]))

    def test_rejects_invalid_output_and_unexpected_branches(self):
        cases = [
            {},
            [{"headBranchName": "main", "baseBranchName": "main"}],
            [{"headBranchName": "release-please--branches--main", "baseBranchName": "other"}],
        ]
        for prs in cases:
            with self.subTest(prs=prs):
                state, done = self.dispatch(prs)
                self.assertIn("Unexpected release PR", done.stderr)
                self.assertEqual((1, []), (done.returncode, state["calls"]))

    def test_dispatch_failure_stops_the_remaining_batch(self):
        state, done = self.dispatch([RELEASE_PR, RELEASE_PR], fail="every")
        self.assertIn("CalledProcessError", done.stderr)
        self.assertEqual((1, 1), (done.returncode, len(state["calls"])))


class ReleaseLabelTests(StepCase):
    step = LABELS

    def labels(self, state, number=7):
        return state["pulls"][str(number)]["labels"]

    def test_tag_on_the_merge_commit_moves_the_label_adding_before_removing(self):
        state = forge([pull()], {"v1.2.3": lightweight()})
        said = self.passed(state)
        self.assertEqual([TAGGED], self.labels(state))
        add = ["--method", "POST", f"repos/{REPO}/issues/7/labels", "--raw-field", f"labels[]={TAGGED}"]
        remove = ["--method", "DELETE", f"repos/{REPO}/issues/7/labels/autorelease%3A%20pending"]
        self.assertEqual([add, remove], state["writes"])
        self.assertEqual(["#7: v1.2.3 names its merge commit, marked tagged"], said)

    def test_annotated_tag_is_followed_to_its_commit(self):
        annotated = {"v1.2.3": {"type": "tag", "sha": ANNOTATION}}
        state = forge([pull()], annotated, annotations={ANNOTATION: lightweight()})
        self.passed(state)
        self.assertEqual([TAGGED], self.labels(state))
        state = forge([pull()], annotated, annotations={ANNOTATION: lightweight(OTHER)})
        self.passed(state)
        self.assertEqual(([PENDING], []), (self.labels(state), state["writes"]))

    def test_tag_on_another_commit_absent_or_only_similar_keeps_the_label(self):
        cases = {
            "another commit": {"v1.2.3": lightweight(OTHER)},
            "absent": {},
            "longer name": {"v1.2.30": lightweight(), "v1.2.3-rc": lightweight()},
            "other version": {"v1.2.4": lightweight()},
            "names a tree": {"v1.2.3": {"type": "tree", "sha": MERGE}},
            "nested annotation": {"v1.2.3": {"type": "tag", "sha": ANNOTATION}},
        }
        for name, tags in cases.items():
            with self.subTest(name=name):
                state = forge([pull()], tags, annotations={ANNOTATION: {"type": "tag", "sha": MERGE}})
                said = self.passed(state)
                self.assertEqual(([PENDING], []), (self.labels(state), state["writes"]))
                self.assertEqual(["#7: label stays, no version tag names its merge commit"], said)

    def test_only_merged_release_pull_requests_into_main_are_touched(self):
        cases = {
            "closed unmerged": {"merged": False},
            "no merge commit": {"merge_commit_sha": None},
            "other base": {"base": {"ref": "release"}},
            "other branch": {"head": {"ref": "feat/release-please--branches--main"}},
            "other title": {"title": "chore(main): release 1.2.3 and more"},
            "component title": {"title": "chore(main): release plugin 1.2.3"},
            "prerelease title": {"title": "chore(main): release 1.2.3-rc.1"},
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                state = forge([pull(**changes)], {"v1.2.3": lightweight(), "v1.2.3-rc.1": lightweight()})
                self.passed(state)
                self.assertEqual(([PENDING], []), (self.labels(state), state["writes"]))
        untagged = forge([pull(merge_commit_sha=None)])
        self.passed(untagged)
        self.assertEqual(([PENDING], []), (self.labels(untagged), untagged["writes"]))

    def test_each_pull_request_is_judged_alone_and_plain_issues_are_skipped(self):
        pulls = [pull(7), pull(9, "1.3.0", merge_commit_sha=OTHER), pull(11, "1.4.0")]
        state = forge(pulls, {"v1.2.3": lightweight(), "v1.3.0": lightweight()}, issues=[{"number": 5}])
        said = self.passed(state)
        self.assertEqual([[TAGGED], [PENDING], [PENDING]], [self.labels(state, number) for number in (7, 9, 11)])
        self.assertEqual(3, len(said))
        self.assertNotIn(["api", f"repos/{REPO}/pulls/5"], state["calls"])

    def test_nothing_pending_reads_one_listing_and_writes_nothing(self):
        state = forge([pull(labels=[TAGGED])], {"v1.2.3": lightweight()})
        self.assertEqual([], self.passed(state))
        self.assertEqual(1, len(state["calls"]))

    def test_full_pending_page_fails_before_any_write_and_nineteen_are_read(self):
        other = {"title": "docs: not a release"}
        full = forge([pull(number, **other) for number in range(1, 21)])
        self.failed(full, "Unexpected pending release PR listing")
        self.assertEqual(1, len(full["calls"]))
        nineteen = forge([pull(number, **other) for number in range(1, 20)])
        self.assertEqual(19, len(self.passed(nineteen)))
        self.assertEqual(20, len(nineteen["calls"]))

    def test_malformed_listings_fail_before_any_write(self):
        for listing in ({}, [{"ref": "refs/tags/v1.2.3", "object": lightweight()}] * 30):
            with self.subTest(listing=type(listing).__name__):
                state = forge([pull()], {"v1.2.3": lightweight()}, listing=listing)
                self.failed(state, "Unexpected tag listing")
                self.assertEqual([], state["writes"])
        odd = forge([pull()], {"v1.2.3": lightweight()}, issues=[{"number": "7/labels", "pull_request": {}}])
        self.failed(odd, "Unexpected pending release PR listing")
        self.assertEqual(([], 1), (odd["writes"], len(odd["calls"])))

    def test_failed_request_stops_the_step_without_echoing_the_response(self):
        state = forge([pull()], {"v1.2.3": lightweight()}, fail="every")
        self.failed(state, "Release label request failed")
        self.assertEqual(([], 1), (state["writes"], len(state["calls"])))

    def test_failed_addition_keeps_the_pull_request_pending(self):
        tags = {"v1.2.3": lightweight(), "v1.3.0": lightweight()}
        state = forge([pull(), pull(9, "1.3.0")], tags, fail="POST")
        self.failed(state, "Release label request failed")
        self.assertEqual([[PENDING], [PENDING]], [self.labels(state, number) for number in (7, 9)])
        self.assertEqual([], state["writes"])

    def test_failed_removal_stops_and_the_next_run_completes_it(self):
        state = forge([pull()], {"v1.2.3": lightweight()}, fail="DELETE")
        self.failed(state, "Release label request failed")
        self.assertEqual([PENDING, TAGGED], self.labels(state))
        state.update(fail=None, calls=[], writes=[])
        self.passed(state)
        self.assertEqual([TAGGED, TAGGED], self.labels(state))

    def test_oversized_answer_is_refused(self):
        state = forge([pull(title="chore(main): release 1.2.3" + " " * 1_000_000)], {"v1.2.3": lightweight()})
        self.failed(state, "Release label request failed")
        self.assertEqual([], state["writes"])


class WorkflowShapeTests(unittest.TestCase):
    def test_labels_are_judged_before_release_please_runs_and_checks_are_dispatched_after(self):
        contents = WORKFLOW.read_text(encoding="utf-8")
        positions = [contents.index(f"      - name: {LABELS}\n"), contents.index(ACTION)]
        positions.append(contents.index(f"      - name: {DISPATCH}\n"))
        self.assertEqual(sorted(positions), positions)
        self.assertEqual(3, contents.count(START))
        self.assertEqual(1, contents.count("skip-github-release: true"))

    def test_every_external_process_has_a_deadline(self):
        for step in (LABELS, DISPATCH):
            with self.subTest(step=step):
                calls = [line for line in source(step).splitlines() if "subprocess.run(" in line]
                self.assertEqual(1, len(calls))
                self.assertIn("timeout=60)", calls[0])


class InterpreterPinTests(unittest.TestCase):
    def judge(self, pinned):
        """Run the guard as the workflow does, beside the pin file its checkout step leaves."""
        with tempfile.TemporaryDirectory(prefix="jellysin-workflow-") as directory:
            if pinned is not None:
                target = Path(directory) / ".release-helper" / ".python-version"
                target.parent.mkdir()
                target.write_text(f"{pinned}\n", encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-"],
                input=source(INTERPRETER),
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

    def test_the_pinned_interpreter_passes(self):
        done = self.judge(platform.python_version())
        self.assertEqual((0, ""), (done.returncode, done.stderr))
        self.assertEqual(f"python3 is the pinned {platform.python_version()}\n", done.stdout)

    def test_any_other_interpreter_is_refused(self):
        major, minor, patch = platform.python_version_tuple()
        # The runner image's own interpreter, the next patch release, and a prefix of the pin.
        for pinned in ("3.12.3", f"{major}.{minor}.{int(patch) + 1}", f"{major}.{minor}"):
            with self.subTest(pinned=pinned):
                done = self.judge(pinned)
                self.assertEqual(1, done.returncode)
                self.assertEqual(f"python3 is {platform.python_version()}, the pin is {pinned}\n", done.stderr)

    def test_a_missing_pin_file_is_refused(self):
        done = self.judge(None)
        self.assertNotEqual(0, done.returncode)
        self.assertIn("FileNotFoundError", done.stderr)

    def test_the_pin_is_read_from_the_workflow_commit_before_any_program_runs(self):
        contents = WORKFLOW.read_text(encoding="utf-8")
        positions = [contents.index(CHECKOUT), contents.index(SETUP)]
        positions += [contents.index(f"      - name: {INTERPRETER}\n"), contents.index(START)]
        positions.append(contents.index(f"      - name: {LABELS}\n"))
        self.assertEqual(sorted(positions), positions)
        self.assertEqual((1, 1), (contents.count(CHECKOUT), contents.count(SETUP)))
        checkout = contents[positions[0] : positions[1]].splitlines()[2:]
        self.assertEqual(
            [
                "repository: ${{ job.workflow_repository }}",
                "ref: ${{ job.workflow_sha }}",
                "sparse-checkout: .python-version",
                "sparse-checkout-cone-mode: false",
                "path: .release-helper",
                "persist-credentials: false",
            ],
            [line.strip() for line in checkout],
        )
        setup = contents[positions[1] : positions[2]].splitlines()[2:]
        self.assertEqual(["python-version-file: .release-helper/.python-version"], [line.strip() for line in setup])
