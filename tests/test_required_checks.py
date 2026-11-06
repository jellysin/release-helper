"""Tests verifying required status checks against workflow reported checks."""

import copy
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
DRAFT_STOP_STEP_TEXT = (
    "      - name: Stop on a draft pull request\n"
    "        if: github.event.pull_request.draft == true\n"
    "        shell: bash\n"
    "        env:\n"
    "          TITLE: Gate not run on a draft\n"
    "          MESSAGE: >-\n"
    "            The gate did not run because the pull request is a draft.\n"
    "            It runs when the pull request is marked ready for review.\n"
    "        run: |\n"
    '          echo "::error title=$TITLE::$MESSAGE"\n'
    '          echo "$MESSAGE" >> "$GITHUB_STEP_SUMMARY"\n'
    "          exit 1\n"
)
DRAFT_STOP_JOBS_CI = ("checks", "secrets", "trivy", "semgrep", "governance")
DRAFT_STOP_JOBS_CODEQL = ("analyze",)


def is_draft_stop_step(step: object) -> bool:
    """Return True if step matches the required draft stop step specification."""
    if not isinstance(step, dict):
        return False
    if step.get("name") != "Stop on a draft pull request":
        return False
    if step.get("if") != "github.event.pull_request.draft == true":
        return False
    if step.get("shell") != "bash":
        return False
    env = step.get("env")
    if not isinstance(env, dict) or env.get("TITLE") != "Gate not run on a draft":
        return False
    expected_msg = (
        "The gate did not run because the pull request is a draft. "
        "It runs when the pull request is marked ready for review."
    )
    if env.get("MESSAGE") != expected_msg:
        return False
    run = step.get("run")
    return isinstance(run, str) and run.strip().endswith("exit 1")


def job_has_draft_stop(job: object) -> bool:
    """Return True if the job's first step is the exact draft PR stop step."""
    if not isinstance(job, dict):
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    return is_draft_stop_step(steps[0])


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
                    data = yaml.safe_load(content)
                    languages = data["jobs"]["analyze"]["strategy"]["matrix"]["language"]
                    self.assertIn("actions", languages)
                    languages.remove("actions")
                    self.assertTrue(languages)
                    content = yaml.safe_dump(data, sort_keys=False)
                target.write_text(content, encoding="utf-8")

            reported = reported_checks(temp_path)
            required = set(required_contexts(RULESET_PATH))
            self.assertNotIn("analyze (actions)", reported)
            self.assertIn("analyze (actions)", required)
            self.assertFalse(required.issubset(reported))

    def test_pr_jobs_begin_with_exact_draft_stop_step(self):
        """Ensure all 6 pull_request jobs begin with the exact draft stop step and raw text matches."""
        ci_text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        codeql_text = (WORKFLOW_DIR / "codeql.yml").read_text(encoding="utf-8")
        self.assertEqual(ci_text.count(DRAFT_STOP_STEP_TEXT), len(DRAFT_STOP_JOBS_CI))
        self.assertEqual(codeql_text.count(DRAFT_STOP_STEP_TEXT), len(DRAFT_STOP_JOBS_CODEQL))

        ci_data = yaml.safe_load(ci_text)
        for job_id in DRAFT_STOP_JOBS_CI:
            with self.subTest(workflow="ci.yml", job=job_id):
                job = ci_data.get("jobs", {}).get(job_id)
                self.assertTrue(job_has_draft_stop(job))

        codeql_data = yaml.safe_load(codeql_text)
        for job_id in DRAFT_STOP_JOBS_CODEQL:
            with self.subTest(workflow="codeql.yml", job=job_id):
                job = codeql_data.get("jobs", {}).get(job_id)
                self.assertTrue(job_has_draft_stop(job))

    def test_pr_triggers_include_ready_for_review(self):
        """Ensure both ci.yml and codeql.yml pull_request triggers list ready_for_review."""
        ci_data = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
        ci_triggers = ci_data.get("on") or ci_data.get(True) or {}
        ci_pr_types = ci_triggers.get("pull_request", {}).get("types", [])
        expected_ci_types = ["opened", "synchronize", "reopened", "labeled", "unlabeled", "ready_for_review"]
        self.assertEqual(ci_pr_types, expected_ci_types)

        codeql_data = yaml.safe_load((WORKFLOW_DIR / "codeql.yml").read_text(encoding="utf-8"))
        codeql_triggers = codeql_data.get("on") or codeql_data.get(True) or {}
        codeql_pr_types = codeql_triggers.get("pull_request", {}).get("types", [])
        expected_codeql_types = ["opened", "synchronize", "reopened", "ready_for_review"]
        self.assertEqual(codeql_pr_types, expected_codeql_types)

    def test_planted_proof_job_copy_without_draft_step_detected(self):
        """Ensure a job copy with the draft stop step removed is detected as missing by helper."""
        ci_data = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
        for job_id in DRAFT_STOP_JOBS_CI:
            with self.subTest(workflow="ci.yml", job=job_id):
                job = ci_data["jobs"][job_id]
                job_copy = copy.deepcopy(job)
                self.assertTrue(job_has_draft_stop(job_copy))
                job_copy["steps"].pop(0)
                self.assertFalse(job_has_draft_stop(job_copy))

        codeql_data = yaml.safe_load((WORKFLOW_DIR / "codeql.yml").read_text(encoding="utf-8"))
        codeql_job = codeql_data["jobs"]["analyze"]
        codeql_copy = copy.deepcopy(codeql_job)
        self.assertTrue(job_has_draft_stop(codeql_copy))
        codeql_copy["steps"].pop(0)
        self.assertFalse(job_has_draft_stop(codeql_copy))

    def test_draft_stop_step_boundary_conditions(self):
        """Ensure boundary variations on step if condition and run exit code fail validation."""
        valid_step = {
            "name": "Stop on a draft pull request",
            "if": "github.event.pull_request.draft == true",
            "shell": "bash",
            "env": {
                "TITLE": "Gate not run on a draft",
                "MESSAGE": (
                    "The gate did not run because the pull request is a draft. "
                    "It runs when the pull request is marked ready for review."
                ),
            },
            "run": ('echo "::error title=$TITLE::$MESSAGE"\necho "$MESSAGE" >> "$GITHUB_STEP_SUMMARY"\nexit 1\n'),
        }
        self.assertTrue(is_draft_stop_step(valid_step))

        for bad_if in (
            "github.event.pull_request.draft",
            "github.event.pull_request.draft == 'true'",
            "true",
            "github.event.pull_request.draft != false",
        ):
            step = copy.deepcopy(valid_step)
            step["if"] = bad_if
            self.assertFalse(is_draft_stop_step(step))

        for bad_run in (
            'echo "$MESSAGE"\nexit 0\n',
            'echo "$MESSAGE"\nexit 2\n',
            'echo "done"\n',
        ):
            step = copy.deepcopy(valid_step)
            step["run"] = bad_run
            self.assertFalse(is_draft_stop_step(step))
