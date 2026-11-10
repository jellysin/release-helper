"""Regression tests verifying commit pin integrity for Renovate presets and composite actions.

Covers review follow-up items from #148 (a570793):
1. renovate.json extends entries starting with 'github>' must end with '#<40 lowercase hex>'.
2. actions/publish/action.yml must install cosign before invoking the publish shell step.
"""

import json
import re
import unittest

import yaml

from tests.action_steps import ROOT

MINIMAL_ACTION = """\
runs:
  using: composite
  steps:
    - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0
    - uses: {cosign_ref}
      with:
        cosign-release: {cosign_release}
    - shell: bash
      run: |
        python "$TOOL_ROOT/tools.py" publish --directory dist
"""

REVERSED_ACTION = """\
runs:
  using: composite
  steps:
    - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0
    - shell: bash
      run: |
        python "$TOOL_ROOT/tools.py" publish --directory dist
    - uses: sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532
      with:
        cosign-release: v3.1.4
"""

NO_COSIGN_ACTION = """\
runs:
  using: composite
  steps:
    - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0
    - shell: bash
      run: |
        python "$TOOL_ROOT/tools.py" publish --directory dist
"""


def validate_renovate_preset_pins(config: dict) -> list[str]:
    """Validate that all GitHub preset references in extends are pinned to a 40-char lowercase hex SHA.

    Uppercase hexadecimal is refused: git prints object names in lowercase, so an uppercase
    pin was typed by hand rather than copied from a resolved commit.
    """
    extends = config.get("extends", [])
    if not isinstance(extends, list):
        raise ValueError("renovate.json 'extends' must be a list")

    pattern = re.compile(r"^github>[^#@\s]+#[0-9a-f]{40}$")
    pinned = []
    has_horistes = False

    for entry in extends:
        if not isinstance(entry, str):
            continue
        if entry.startswith("github>"):
            if not pattern.match(entry):
                raise ValueError(f"Preset entry '{entry}' must end with '#<40 lowercase hex>' commit SHA")
            pinned.append(entry)
            if entry.startswith("github>plurano/horistes#"):
                has_horistes = True

    if not has_horistes:
        raise ValueError("Required preset 'github>plurano/horistes#' is missing from extends")

    return pinned


def validate_cosign_step(step: dict) -> None:
    """Validate that the cosign-installer step is pinned to 40 lowercase hex characters with an exact release tag.

    The step must carry neither 'if' nor 'continue-on-error' keys.
    """
    if "if" in step:
        raise ValueError("Installer step 'sigstore/cosign-installer' must not define an 'if' condition")
    if "continue-on-error" in step:
        raise ValueError("Installer step 'sigstore/cosign-installer' must not define 'continue-on-error'")

    uses = step.get("uses", "")
    parts = uses.split("@")
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{40}", parts[1]):
        raise ValueError("Step 'sigstore/cosign-installer' must be pinned to a 40-character lowercase hex commit SHA")

    with_block = step.get("with")
    if not isinstance(with_block, dict):
        raise ValueError("Installer step 'sigstore/cosign-installer' missing 'with' configuration")
    cosign_release = with_block.get("cosign-release")
    if not isinstance(cosign_release, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", cosign_release):
        raise ValueError("cosign-release must specify an exact version 'v<digits>.<digits>.<digits>'")


def _extract_action_steps(action: dict) -> list[dict]:
    """Extract and validate the runs.steps list from an action mapping."""
    if not isinstance(action, dict):
        raise ValueError("Action definition must be a dictionary")
    runs = action.get("runs")
    if not isinstance(runs, dict):
        raise ValueError("Action definition missing 'runs' dictionary")
    steps = runs.get("steps")
    if not isinstance(steps, list):
        raise ValueError("Action definition missing 'runs.steps' list")
    return steps


def _find_step_indices(steps: list[dict]) -> tuple[list[int], list[int]]:
    """Locate cosign-installer and publish step positions in the action steps."""
    cosign_indices = []
    publish_indices = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        uses = step.get("uses")
        if isinstance(uses, str) and uses.split("@")[0] == "sigstore/cosign-installer":
            validate_cosign_step(step)
            cosign_indices.append(idx)
        run = step.get("run")
        if isinstance(run, str) and 'tools.py" publish' in run:
            publish_indices.append(idx)
    return cosign_indices, publish_indices


def validate_publish_action_steps(action: dict) -> tuple[int, int]:
    """Validate that actions/publish/action.yml installs cosign before running the publish command."""
    steps = _extract_action_steps(action)
    cosign_indices, publish_indices = _find_step_indices(steps)

    if not cosign_indices:
        raise ValueError("Step 'sigstore/cosign-installer' is missing")
    if len(cosign_indices) > 1:
        raise ValueError(f"Expected exactly one cosign-installer step, found {len(cosign_indices)}")
    if not publish_indices:
        raise ValueError("Publish shell step running 'tools.py\" publish' is missing")

    cosign_idx = cosign_indices[0]
    publish_idx = publish_indices[0]
    if cosign_idx >= publish_idx:
        raise ValueError(f"cosign-installer step (index {cosign_idx}) must precede publish step (index {publish_idx})")

    return cosign_idx, publish_idx


class RenovatePresetPinTests(unittest.TestCase):
    """Test verification and rejection of Renovate preset pins in renovate.json."""

    def test_positive_repo_renovate_json_pinned(self):
        """Validate repository renovate.json extends entries are properly pinned to full lowercase commit SHAs."""
        config_path = ROOT / "renovate.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        pinned = validate_renovate_preset_pins(config)
        self.assertIn("github>plurano/horistes#2368b588cf97621b275ae7fd4eb10d720dc64688", pinned)

    def test_negative_unpinned_preset_refused(self):
        """Refuse an unpinned GitHub preset reference lacking a commit SHA."""
        config = {"extends": ["github>plurano/horistes"]}
        with self.assertRaisesRegex(ValueError, "must end with '#<40 lowercase hex>'"):
            validate_renovate_preset_pins(config)

    def test_negative_short_sha_refused(self):
        """Refuse a truncated commit SHA instead of full 40-character hash."""
        config = {"extends": ["github>plurano/horistes#2368b58"]}
        with self.assertRaisesRegex(ValueError, "must end with '#<40 lowercase hex>'"):
            validate_renovate_preset_pins(config)

    def test_negative_tag_ref_refused(self):
        """Refuse a mutable tag reference, which Renovate writes as `github>owner/repo#<tag>`."""
        config = {"extends": ["github>plurano/horistes#v4.0.0"]}
        with self.assertRaisesRegex(ValueError, "must end with '#<40 lowercase hex>'"):
            validate_renovate_preset_pins(config)

    def test_negative_missing_horistes_preset_refused(self):
        """Refuse configuration when the required horistes preset pin is absent."""
        config = {"extends": ["github>other/repo#2368b588cf97621b275ae7fd4eb10d720dc64688"]}
        with self.assertRaisesRegex(ValueError, "Required preset 'github>plurano/horistes#' is missing"):
            validate_renovate_preset_pins(config)

    def test_boundary_uppercase_hex_refused(self):
        """Refuse an uppercase preset SHA; git prints object names in lowercase."""
        config = {"extends": ["github>plurano/horistes#2368B588CF97621B275AE7FD4EB10D720DC64688"]}
        with self.assertRaisesRegex(ValueError, "must end with '#<40 lowercase hex>'"):
            validate_renovate_preset_pins(config)

    def test_boundary_github_entry_without_hash_refused(self):
        """Refuse a github> entry without a hash delimiter."""
        config = {"extends": ["github>plurano/horistes", "helpers:pinGitHubActionDigests"]}
        with self.assertRaisesRegex(ValueError, "must end with '#<40 lowercase hex>'"):
            validate_renovate_preset_pins(config)

    def test_boundary_non_github_presets_ignored(self):
        """Ignore non-github> preset references such as helper presets."""
        config = {
            "extends": [
                "helpers:pinGitHubActionDigests",
                "github>plurano/horistes#2368b588cf97621b275ae7fd4eb10d720dc64688",
                "docker:pinDigests",
            ]
        }
        pinned = validate_renovate_preset_pins(config)
        self.assertEqual(["github>plurano/horistes#2368b588cf97621b275ae7fd4eb10d720dc64688"], pinned)


class PublishActionPinTests(unittest.TestCase):
    """Test verification and rejection of action pins and step order in actions/publish/action.yml."""

    def test_positive_repo_publish_action_valid(self):
        """Validate repository actions/publish/action.yml installs cosign before running the publish shell step."""
        action_path = ROOT / "actions" / "publish" / "action.yml"
        action = yaml.safe_load(action_path.read_text(encoding="utf-8"))
        cosign_idx, publish_idx = validate_publish_action_steps(action)
        self.assertLess(cosign_idx, publish_idx)
        self.assertEqual(cosign_idx, 1)

    def test_negative_cosign_step_missing_refused(self):
        """Refuse action definitions where the sigstore/cosign-installer step is missing."""
        action = yaml.safe_load(NO_COSIGN_ACTION)
        with self.assertRaisesRegex(ValueError, "Step 'sigstore/cosign-installer' is missing"):
            validate_publish_action_steps(action)

    def test_negative_cosign_step_after_publish_refused(self):
        """Refuse action definitions where the cosign-installer step appears after the publish step."""
        action = yaml.safe_load(REVERSED_ACTION)
        with self.assertRaisesRegex(ValueError, "must precede publish step"):
            validate_publish_action_steps(action)

    def test_negative_cosign_release_latest_refused(self):
        """Refuse cosign installer configurations using unpinned 'latest' instead of exact version."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532",
            cosign_release="latest",
        )
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "cosign-release must specify an exact version"):
            validate_publish_action_steps(action)

    def test_negative_cosign_tag_ref_refused(self):
        """Refuse cosign installer step referencing a git tag instead of full commit SHA."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@v4.1.3",
            cosign_release="v3.1.4",
        )
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "must be pinned to a 40-character lowercase hex"):
            validate_publish_action_steps(action)

    def test_negative_cosign_short_sha_refused(self):
        """Refuse cosign installer step referencing a short commit SHA instead of full 40-character SHA."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@756fe2d",
            cosign_release="v3.1.4",
        )
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "must be pinned to a 40-character lowercase hex"):
            validate_publish_action_steps(action)

    def test_boundary_cosign_uppercase_hex_refused(self):
        """Refuse an uppercase action SHA; git prints object names in lowercase."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@756FE2D284C7292C4E9B62C8A28F1E5629C59532",
            cosign_release="v3.1.4",
        )
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "must be pinned to a 40-character lowercase hex"):
            validate_publish_action_steps(action)

    def test_negative_cosign_in_comment_and_run_script_refused(self):
        """Refuse action when installer name appears only in comments and another step's run script."""
        action_text = """\
runs:
  using: composite
  steps:
    - # uses: sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532
      shell: bash
      run: |
        echo "sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532"
        python "$TOOL_ROOT/tools.py" publish --directory dist
"""
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "Step 'sigstore/cosign-installer' is missing"):
            validate_publish_action_steps(action)

    def test_negative_cosign_with_if_condition_refused(self):
        """Refuse cosign installer step that defines an if condition."""
        action_text = """\
runs:
  using: composite
  steps:
    - uses: sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532
      if: false
      with:
        cosign-release: v3.1.4
    - shell: bash
      run: |
        python "$TOOL_ROOT/tools.py" publish --directory dist
"""
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "must not define an 'if' condition"):
            validate_publish_action_steps(action)

    def test_negative_cosign_with_continue_on_error_refused(self):
        """Refuse cosign installer step that defines continue-on-error."""
        action_text = """\
runs:
  using: composite
  steps:
    - uses: sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532
      continue-on-error: true
      with:
        cosign-release: v3.1.4
    - shell: bash
      run: |
        python "$TOOL_ROOT/tools.py" publish --directory dist
"""
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "must not define 'continue-on-error'"):
            validate_publish_action_steps(action)

    def test_negative_cosign_release_in_comment_only_refused(self):
        """Refuse cosign installer step where cosign-release is present only in a comment."""
        action_text = """\
runs:
  using: composite
  steps:
    - uses: sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532
      with:
        # cosign-release: v3.1.4
        other: value
    - shell: bash
      run: |
        python "$TOOL_ROOT/tools.py" publish --directory dist
"""
        action = yaml.safe_load(action_text)
        with self.assertRaisesRegex(ValueError, "cosign-release must specify an exact version"):
            validate_publish_action_steps(action)
