"""The scheduled report of the sandbox release cycle, run against the stand-in gh."""

import datetime
from pathlib import Path

from tests.test_release_pr_workflow import REPO, StepCase, forge

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "sandbox-cycle.yml"
SANDBOX = "jellysin/release-helper-sandbox"
TITLE = "Sandbox release cycle fails for main"
HEAD, OTHER = "a" * 40, "b" * 40
RUNS = f"repos/{SANDBOX}/actions/workflows/cycle.yml/runs?per_page=30"
ISSUES = f"repos/{REPO}/issues?state=open&per_page=100"


def ago(hours):
    moment = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def cycle(run_id, tested, conclusion="success", status="completed"):
    """One sandbox cycle run and the reads that name the commit it tested."""
    run = {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://example.invalid/runs/{run_id}",
    }
    reads = {
        f"repos/{SANDBOX}/actions/runs/{run_id}/jobs": {"jobs": [{"id": run_id * 10}]},
        f"repos/{SANDBOX}/check-runs/{run_id * 10}/annotations": [
            {"title": "", "message": "Process completed with exit code 1."},
            {"title": "release-helper", "message": tested},
        ],
    }
    return run, reads


def state(cycles, committed_hours_ago=0.1, issues=()):
    responses = {
        f"repos/{REPO}/commits/main": {"sha": HEAD, "commit": {"committer": {"date": ago(committed_hours_ago)}}},
        RUNS: {"workflow_runs": [run for run, _ in cycles]},
        ISSUES: list(issues),
    }
    for _, reads in cycles:
        responses.update(reads)
    return forge(responses=responses, issue_writes=[])


OPEN_ISSUE = {"number": 5, "title": TITLE}


class SandboxCycleReportTests(StepCase):
    step = "Open, update or close the issue for the newest sandbox cycle of main"
    workflow = WORKFLOW

    def writes(self, current):
        return [(arguments[1], arguments[2]) for arguments in current["issue_writes"]]

    def test_a_successful_cycle_for_main_closes_the_open_issue(self):
        current = state([cycle(3, HEAD), cycle(2, OTHER, "failure")], issues=[OPEN_ISSUE])
        lines = self.passed(current)
        self.assertEqual(
            self.writes(current), [("POST", f"repos/{REPO}/issues/5/comments"), ("PATCH", f"repos/{REPO}/issues/5")]
        )
        self.assertIn("state=closed", current["issue_writes"][1])
        self.assertEqual(lines[-1], "closed #5")

    def test_a_failed_cycle_for_main_opens_the_issue(self):
        current = state([cycle(4, HEAD, "failure"), cycle(3, HEAD)])
        lines = self.passed(current)
        self.assertEqual(self.writes(current), [("POST", f"repos/{REPO}/issues")])
        self.assertIn(f"title={TITLE}", current["issue_writes"][0])
        self.assertIn("concluded failure: https://example.invalid/runs/4", lines[-1])

    def test_a_failed_cycle_updates_an_issue_that_is_already_open(self):
        current = state([cycle(4, HEAD, "cancelled")], issues=[OPEN_ISSUE])
        self.passed(current)
        self.assertEqual(self.writes(current), [("PATCH", f"repos/{REPO}/issues/5")])

    def test_main_without_a_cycle_after_two_hours_opens_the_issue(self):
        current = state([cycle(2, OTHER)], committed_hours_ago=2.1)
        lines = self.passed(current)
        self.assertEqual(self.writes(current), [("POST", f"repos/{REPO}/issues")])
        self.assertIn(f"No sandbox release cycle tested main {HEAD} within two hours", lines[-1])

    def test_main_without_a_cycle_inside_two_hours_or_with_one_running_writes_nothing(self):
        for cycles, hours in (([cycle(2, OTHER, "failure")], 1.9), ([cycle(5, HEAD, None, "in_progress")], 5)):
            with self.subTest(cycles=cycles, hours=hours):
                current = state(cycles, committed_hours_ago=hours)
                self.passed(current)
                self.assertEqual(current["issue_writes"], [])

    def test_a_failed_request_fails_without_echoing_it(self):
        self.failed(state([]) | {"fail": "every"}, "Sandbox cycle request failed")
