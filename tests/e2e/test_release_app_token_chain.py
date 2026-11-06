"""Follow the release App token from the caller's secret to the release-please step that opens PRs.

Each workflow file is checked alone in tests/test_release_pr_workflow.py; this test reads the
caller, the reusable workflow and the README caller example together, as GitHub composes them.
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SECRET = "RELEASE_APP_PRIVATE_KEY"
TOKEN = "${{ steps.app-token.outputs.token }}"
MINT = "actions/create-github-app-token@"
RELEASE_PLEASE = "googleapis/release-please-action@"


def load(path):
    """Parse one workflow; PyYAML reads the bare key on as True."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["on"] = document.pop(True)
    return document


def readme_caller_example():
    """Return the YAML block of the README that shows a caller of release-please.yml."""
    blocks = re.findall(r"```yaml\n(.*?)```", (ROOT / "README.md").read_text(encoding="utf-8"), re.DOTALL)
    examples = [block for block in blocks if "release-please.yml@" in block]
    assert len(examples) == 1, f"expected one README caller example, found {len(examples)}"
    return yaml.safe_load("job:\n" + examples[0])["job"]


class ReleaseAppTokenChainTests(unittest.TestCase):
    def setUp(self):
        self.caller = load(WORKFLOWS / "release-automation.yml")
        self.called = load(WORKFLOWS / "release-please.yml")
        (self.job,) = self.called["jobs"].values()

    def test_every_secret_the_reusable_workflow_requires_reaches_it_from_each_caller(self):
        declared = self.called["on"]["workflow_call"]["secrets"]
        required = {name for name, spec in declared.items() if spec.get("required")}
        self.assertEqual({SECRET}, required)
        callers = {"release-automation.yml": self.caller["jobs"]["release"], "README.md": readme_caller_example()}
        for name, job in callers.items():
            with self.subTest(caller=name):
                self.assertTrue(job["uses"].split("@")[0].endswith(".github/workflows/release-please.yml"))
                self.assertEqual({SECRET: f"${{{{ secrets.{SECRET} }}}}"}, job["secrets"])

    def test_minted_token_reaches_release_please_and_no_other_step(self):
        steps = self.job["steps"]
        mints = [index for index, step in enumerate(steps) if step.get("uses", "").startswith(MINT)]
        self.assertEqual(1, len(mints))
        mint = steps[mints[0]]
        self.assertEqual("app-token", mint["id"])
        self.assertNotIn("if", mint)
        self.assertNotIn("continue-on-error", mint)
        self.assertEqual(f"${{{{ secrets.{SECRET} }}}}", mint["with"]["private-key"])
        self.assertEqual("${{ vars.RELEASE_APP_CLIENT_ID }}", mint["with"]["client-id"])
        holders = [index for index, step in enumerate(steps) if "steps.app-token.outputs.token" in yaml.safe_dump(step)]
        self.assertEqual(1, len(holders), "only release-please may hold the App token")
        release = steps[holders[0]]
        self.assertTrue(release["uses"].startswith(RELEASE_PLEASE))
        self.assertEqual(TOKEN, release["with"]["token"])
        self.assertGreater(holders[0], mints[0])

    def test_no_step_dispatches_the_release_pr_checks_by_hand(self):
        """The App token's pull_request events start CI; only prepare-release.yml is still dispatched."""
        dispatched = re.findall(r"'gh', 'workflow', 'run', '([^']+)'", yaml.safe_dump(self.job["steps"], width=10**6))
        self.assertEqual(["prepare-release.yml"], dispatched)
        self.assertNotIn("steps.release.outputs.prs", yaml.safe_dump(self.job["steps"], width=10**6))


if __name__ == "__main__":
    unittest.main()
