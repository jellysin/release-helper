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
APP_KEY_INPUT = "RELEASE_APP_PRIVATE_KEY"
MINTED_OUTPUT = "${{ steps.app-token.outputs.token }}"
EXPECTED_AUTH_EXPR = "${{ steps.app-token.outputs.token || github.token }}"
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
    if len(examples) != 1:
        raise AssertionError(f"expected one README caller example, found {len(examples)}")
    return yaml.safe_load("job:\n" + examples[0])["job"]


def simulate_release_auth_choice(workflow_doc, app_key_present: bool):
    """Simulate release auth resolution from workflow text for key present or absent."""
    job = next(iter(workflow_doc["jobs"].values()))
    job_environment = job.get("env", {})
    env_expression = job_environment.get("RELEASE_APP_KEY_SET", "")
    env_flag = "unset"
    if "secrets.RELEASE_APP_PRIVATE_KEY != ''" in env_expression:
        env_flag = "true" if app_key_present else "false"

    steps = job["steps"]
    mint_step = next(s for s in steps if s.get("uses", "").startswith(MINT))
    mint_condition = mint_step.get("if", "")

    mint_ran = mint_condition == "env.RELEASE_APP_KEY_SET == 'true'" and env_flag == "true"
    mint_output = "App" if mint_ran else ""

    release_step = next(s for s in steps if s.get("uses", "").startswith(RELEASE_PLEASE))
    auth_field = release_step.get("with", {}).get("token", "")

    if "steps.app-token.outputs.token || github.token" in auth_field:
        return mint_output if mint_output else "GITHUB_TOKEN"
    if "steps.app-token.outputs.token" in auth_field:
        return mint_output if mint_output else ""
    return ""


class ReleaseAppTokenChainTests(unittest.TestCase):
    """The caller, the reusable workflow and the README example read as one chain."""

    def setUp(self):
        """Load the caller and the reusable workflow; the reusable one has a single job."""
        self.caller = load(WORKFLOWS / "release-automation.yml")
        self.called = load(WORKFLOWS / "release-please.yml")
        (self.job,) = self.called["jobs"].values()

    def test_every_secret_the_reusable_workflow_requires_reaches_it_from_each_caller(self):
        """No secret is required; callers still pass the release App key by name."""
        declared = self.called["on"]["workflow_call"]["secrets"]
        required = {name for name, spec in declared.items() if spec.get("required")}
        self.assertEqual(set(), required)
        callers = {"release-automation.yml": self.caller["jobs"]["release"], "README.md": readme_caller_example()}
        for name, job in callers.items():
            with self.subTest(caller=name):
                self.assertTrue(job["uses"].split("@")[0].endswith(".github/workflows/release-please.yml"))
                self.assertEqual({APP_KEY_INPUT: f"${{{{ secrets.{APP_KEY_INPUT} }}}}"}, job["secrets"])

    def test_minted_token_reaches_release_please_and_no_other_step(self):
        """Mint step runs conditionally on env flag; release-please takes App token or GITHUB_TOKEN fallback."""
        job_env = self.job.get("env", {})
        self.assertEqual(f"${{{{ secrets.{APP_KEY_INPUT} != '' }}}}", job_env.get("RELEASE_APP_KEY_SET"))
        steps = self.job["steps"]
        mints = [index for index, step in enumerate(steps) if step.get("uses", "").startswith(MINT)]
        self.assertEqual(1, len(mints))
        mint = steps[mints[0]]
        self.assertEqual("app-token", mint["id"])
        self.assertEqual("env.RELEASE_APP_KEY_SET == 'true'", mint.get("if"))
        self.assertNotIn("continue-on-error", mint)
        self.assertEqual(f"${{{{ secrets.{APP_KEY_INPUT} }}}}", mint["with"]["private-key"])
        self.assertEqual("${{ vars.RELEASE_APP_CLIENT_ID }}", mint["with"]["client-id"])
        holders = [index for index, step in enumerate(steps) if "steps.app-token.outputs.token" in yaml.safe_dump(step)]
        self.assertEqual(1, len(holders), "only release-please may hold the App token")
        release = steps[holders[0]]
        self.assertTrue(release["uses"].startswith(RELEASE_PLEASE))
        self.assertEqual(EXPECTED_AUTH_EXPR, release["with"]["token"])
        self.assertGreater(holders[0], mints[0])

    def test_token_choice_simulated_for_key_set_and_absent(self):
        """Evaluate token choice from workflow for key set (App token) and key absent (GITHUB_TOKEN)."""
        self.assertEqual("App", simulate_release_auth_choice(self.called, app_key_present=True))
        self.assertEqual("GITHUB_TOKEN", simulate_release_auth_choice(self.called, app_key_present=False))

    def test_no_step_dispatches_the_release_pr_checks_by_hand(self):
        """The App token's pull_request events start CI; only prepare-release.yml is still dispatched."""
        dispatched = re.findall(r"'gh', 'workflow', 'run', '([^']+)'", yaml.safe_dump(self.job["steps"], width=10**6))
        self.assertEqual(["prepare-release.yml"], dispatched)
        self.assertNotIn("steps.release.outputs.prs", yaml.safe_dump(self.job["steps"], width=10**6))


if __name__ == "__main__":
    unittest.main()
