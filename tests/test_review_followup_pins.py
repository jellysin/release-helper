"""Regression tests verifying commit pin integrity for Renovate presets and composite actions.

Covers review follow-up items from #148 (a570793):
1. renovate.json extends entries starting with 'github>' must end with '#<40 lowercase hex>'.
2. actions/publish/action.yml must install cosign before invoking the publish shell step.
"""

import json
import re
import unittest

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


def validate_cosign_step_content(step: str) -> None:
    """Validate that the cosign-installer step is pinned to 40 lowercase hex characters with an exact release tag.

    Uppercase hexadecimal is refused: git prints object names in lowercase, so an uppercase
    pin was typed by hand rather than copied from a resolved commit.
    """
    if not re.search(r"uses:\s*sigstore/cosign-installer@[0-9a-f]{40}(?:\s|$|#)", step):
        raise ValueError("Step 'sigstore/cosign-installer' must be pinned to a 40-character lowercase hex commit SHA")
    if not re.search(r"^\s*cosign-release:\s*v\d+\.\d+\.\d+\s*$", step, re.MULTILINE):
        raise ValueError("cosign-release must specify an exact version 'v<digits>.<digits>.<digits>'")


def validate_publish_action_steps(action_text: str) -> tuple[int, int]:
    """Validate that actions/publish/action.yml installs cosign before running the publish command."""
    steps = re.split(r"(?m)^ {4}- ", action_text)[1:]
    cosign_indices = [i for i, s in enumerate(steps) if "sigstore/cosign-installer" in s]
    publish_indices = [i for i, s in enumerate(steps) if 'tools.py" publish' in s]

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

    validate_cosign_step_content(steps[cosign_idx])
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
        action_text = action_path.read_text(encoding="utf-8")
        cosign_idx, publish_idx = validate_publish_action_steps(action_text)
        self.assertLess(cosign_idx, publish_idx)

    def test_negative_cosign_step_missing_refused(self):
        """Refuse action definitions where the sigstore/cosign-installer step is missing."""
        with self.assertRaisesRegex(ValueError, "Step 'sigstore/cosign-installer' is missing"):
            validate_publish_action_steps(NO_COSIGN_ACTION)

    def test_negative_cosign_step_after_publish_refused(self):
        """Refuse action definitions where the cosign-installer step appears after the publish step."""
        with self.assertRaisesRegex(ValueError, "must precede publish step"):
            validate_publish_action_steps(REVERSED_ACTION)

    def test_negative_cosign_release_latest_refused(self):
        """Refuse cosign installer configurations using unpinned 'latest' instead of exact version."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@756fe2d284c7292c4e9b62c8a28f1e5629c59532",
            cosign_release="latest",
        )
        with self.assertRaisesRegex(ValueError, "cosign-release must specify an exact version"):
            validate_publish_action_steps(action_text)

    def test_negative_cosign_tag_ref_refused(self):
        """Refuse cosign installer step referencing a git tag instead of full commit SHA."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@v4.1.3",
            cosign_release="v3.1.4",
        )
        with self.assertRaisesRegex(ValueError, "must be pinned to a 40-character lowercase hex"):
            validate_publish_action_steps(action_text)

    def test_negative_cosign_short_sha_refused(self):
        """Refuse cosign installer step referencing a short commit SHA instead of full 40-character SHA."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@756fe2d",
            cosign_release="v3.1.4",
        )
        with self.assertRaisesRegex(ValueError, "must be pinned to a 40-character lowercase hex"):
            validate_publish_action_steps(action_text)

    def test_boundary_cosign_uppercase_hex_refused(self):
        """Refuse an uppercase action SHA; git prints object names in lowercase."""
        action_text = MINIMAL_ACTION.format(
            cosign_ref="sigstore/cosign-installer@756FE2D284C7292C4E9B62C8A28F1E5629C59532",
            cosign_release="v3.1.4",
        )
        with self.assertRaisesRegex(ValueError, "must be pinned to a 40-character lowercase hex"):
            validate_publish_action_steps(action_text)
