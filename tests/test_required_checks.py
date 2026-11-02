"""Tests verifying required status checks against workflow reported checks."""

import itertools
import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RULESET_PATH = ROOT / ".github" / "rulesets" / "main.json"


def _is_pr_workflow(data: object) -> bool:
    """Return True if workflow data contains a pull_request trigger."""
    if not isinstance(data, dict):
        return False
    triggers = data.get("on") if "on" in data else data.get(True)
    if isinstance(triggers, str):
        return triggers == "pull_request"
    if isinstance(triggers, (list, tuple, set)):
        return "pull_request" in triggers
    if isinstance(triggers, dict):
        return "pull_request" in triggers
    return False


def _expand_name(template: str, keys: list[str], combo: tuple[str, ...]) -> str:
    """Format a job check name from a template and matrix combination."""
    if re.search(r"\$\{\{\s*matrix\.", template):
        expanded = template
        for k, v in zip(keys, combo, strict=True):
            expanded = re.sub(rf"\$\{{\{{\s*matrix\.{re.escape(k)}\s*\}}\}}", v, expanded)
        return expanded
    return f"{template} ({', '.join(combo)})"


def _expand_job_checks(job_id: str, job: dict[str, object]) -> list[str]:
    """Generate reported status check names for a single workflow job."""
    name = job.get("name")
    raw_name = str(name) if name is not None else None
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None

    if not isinstance(matrix, dict):
        return [raw_name if raw_name is not None else job_id]

    for key, val in list(matrix.items())[:20]:
        if key in ("include", "exclude"):
            raise ValueError(f"Job {job_id} uses unsupported matrix key: {key}")
        if not isinstance(val, (list, tuple)):
            raise ValueError(f"Job {job_id} matrix axis {key} must be a list")

    keys = list(matrix.keys())[:10]
    axes = [[str(v) for v in list(matrix[k])[:50]] for k in keys]
    checks: list[str] = []
    for combo in itertools.product(*axes):
        combo_tuple = combo[:10]
        if raw_name is not None:
            checks.append(_expand_name(raw_name, keys, combo_tuple))
        else:
            checks.append(f"{job_id} ({', '.join(combo_tuple)})")
    return checks


def reported_checks(workflow_dir: Path) -> set[str]:
    """Collect all check contexts reported by pull_request-triggered workflows."""
    checks: set[str] = set()
    workflow_files = sorted(workflow_dir.glob("*.yml"))[:100]
    for path in workflow_files:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not _is_pr_workflow(data):
            continue
        jobs = data.get("jobs", {})
        if not isinstance(jobs, dict):
            continue
        for job_id, job in list(jobs.items())[:100]:
            if isinstance(job, dict):
                checks.update(_expand_job_checks(str(job_id), job))
    return checks


def required_contexts(ruleset_path: Path) -> list[str]:
    """Extract required status check contexts from a GitHub ruleset JSON file."""
    data = json.loads(ruleset_path.read_text(encoding="utf-8"))
    contexts: list[str] = []
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        return contexts
    for rule in rules[:100]:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            params = rule.get("parameters", {})
            raw_checks = params.get("required_status_checks", []) if isinstance(params, dict) else []
            for check in list(raw_checks)[:100]:
                if isinstance(check, dict) and isinstance(check.get("context"), str):
                    contexts.append(check["context"])
    return contexts


class RequiredChecksTests(unittest.TestCase):
    """Test suite validating required status check ruleset contexts and workflows."""

    def test_real_ruleset_required_contexts_are_reported_by_workflows(self):
        """Ensure every required status check context in the ruleset is reported by a PR workflow."""
        required = required_contexts(RULESET_PATH)
        reported = reported_checks(WORKFLOW_DIR)
        self.assertTrue(required, "Ruleset must specify at least one required status check")
        for context in required:
            with self.subTest(context=context):
                self.assertIn(context, reported)

    def test_codeql_contexts_are_required(self):
        """Ensure analyze (python) and analyze (actions) are both required contexts."""
        required = required_contexts(RULESET_PATH)
        self.assertIn("analyze (python)", required)
        self.assertIn("analyze (actions)", required)

    def test_unreported_required_context_detected_missing(self):
        """Ensure a ruleset requiring analyze (go) is detected as missing from reported checks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ruleset_file = Path(temp_dir) / "ruleset.json"
            ruleset_data = {
                "rules": [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [
                                {"context": "analyze (go)"},
                            ]
                        },
                    }
                ]
            }
            ruleset_file.write_text(json.dumps(ruleset_data), encoding="utf-8")
            required = required_contexts(ruleset_file)
            reported = reported_checks(WORKFLOW_DIR)
            self.assertIn("analyze (go)", required)
            self.assertNotIn("analyze (go)", reported)

    def test_push_only_workflow_does_not_report_checks(self):
        """Ensure a push-only workflow does not report PR status checks such as analyze (python)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            wf_file = Path(temp_dir) / "codeql.yml"
            wf_content = (
                "name: CodeQL\n"
                "on:\n"
                "  push:\n"
                "    branches: [main]\n"
                "jobs:\n"
                "  analyze:\n"
                "    strategy:\n"
                "      matrix:\n"
                "        language: [python, actions]\n"
                "    steps: []\n"
            )
            wf_file.write_text(wf_content, encoding="utf-8")
            reported = reported_checks(Path(temp_dir))
            self.assertNotIn("analyze (python)", reported)
            self.assertNotIn("analyze (actions)", reported)

    def test_boundary_matrix_substitution_and_plain_job(self):
        """Ensure matrix expressions are substituted and jobs without matrix/name report job id."""
        with tempfile.TemporaryDirectory() as temp_dir:
            wf_file = Path(temp_dir) / "boundary.yml"
            wf_content = (
                "name: Boundary\n"
                "on:\n"
                "  pull_request:\n"
                "jobs:\n"
                "  matrix_job:\n"
                "    name: 'lint (${{ matrix.py }})'\n"
                "    strategy:\n"
                "      matrix:\n"
                "        py: ['3.14']\n"
                "    steps: []\n"
                "  plain_job:\n"
                "    steps: []\n"
            )
            wf_file.write_text(wf_content, encoding="utf-8")
            reported = reported_checks(Path(temp_dir))
            self.assertEqual(reported, {"lint (3.14)", "plain_job"})

    def test_unsupported_matrix_keys_raise_error(self):
        """Ensure unsupported matrix keys like include or exclude raise ValueError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            wf_file = Path(temp_dir) / "unsupported.yml"
            wf_content = (
                "name: Unsupported\n"
                "on: pull_request\n"
                "jobs:\n"
                "  test:\n"
                "    strategy:\n"
                "      matrix:\n"
                "        include:\n"
                "          - os: ubuntu\n"
                "    steps: []\n"
            )
            wf_file.write_text(wf_content, encoding="utf-8")
            with self.assertRaises(ValueError):
                reported_checks(Path(temp_dir))

    def test_planted_proof_missing_actions_in_copy_fails_requirement(self):
        """Ensure removing actions from codeql.yml copy causes analyze (actions) to be missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            for p in list(WORKFLOW_DIR.glob("*.yml"))[:20]:
                target = temp_path / p.name
                content = p.read_text(encoding="utf-8")
                if p.name == "codeql.yml":
                    content = content.replace("language: [python, actions]", "language: [python]")
                target.write_text(content, encoding="utf-8")

            reported = reported_checks(temp_path)
            required = set(required_contexts(RULESET_PATH))
            self.assertNotIn("analyze (actions)", reported)
            self.assertIn("analyze (actions)", required)
            self.assertFalse(required.issubset(reported))
