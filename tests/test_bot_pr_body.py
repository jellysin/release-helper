"""Tests for pull request template heading enforcement in bot configuration headers."""

import re
import unittest

from jellysin_tooling.common import read_json
from tests.test_tool_name import ROOT

HEADING_PATTERN = re.compile(r"^## .+$", re.MULTILINE)


def template_headings(text: str) -> list[str]:
    """Extract level-2 heading lines from markdown text in order, stripping trailing spaces."""
    return [match.group(0).rstrip() for match in HEADING_PATTERN.finditer(text)]


def missing_headings(header: str, headings: list[str]) -> list[str]:
    """Return headings from the template not present as whole lines in header, order kept."""
    header_lines = {line.rstrip() for line in header.splitlines()}
    return [heading for heading in headings if heading.rstrip() not in header_lines]


class BotPullRequestBodyTemplateTests(unittest.TestCase):
    """Verify bot headers include all required repository pull request template headings."""

    def test_pr_template_has_headings(self):
        template_path = ROOT / ".github" / "pull_request_template.md"
        template_text = template_path.read_text(encoding="utf-8")
        headings = template_headings(template_text)
        self.assertGreaterEqual(len(headings), 1)

    def test_renovate_pr_header_contains_all_template_headings(self):
        template_path = ROOT / ".github" / "pull_request_template.md"
        headings = template_headings(template_path.read_text(encoding="utf-8"))
        config = read_json(ROOT / "renovate.json")
        pr_header = config.get("prHeader", "")
        missing = missing_headings(pr_header, headings)
        self.assertEqual(missing, [])

    def test_release_please_pr_header_contains_all_template_headings(self):
        template_path = ROOT / ".github" / "pull_request_template.md"
        headings = template_headings(template_path.read_text(encoding="utf-8"))
        config = read_json(ROOT / "release-please-config.json")
        pr_header = config.get("pull-request-header", "")
        missing = missing_headings(pr_header, headings)
        self.assertEqual(missing, [])

    def test_negative_header_lacking_validation_reports_missing(self):
        headings = ["## Change", "## Validation"]
        header = "## Change\n\nSome changes introduced here.\n"
        self.assertEqual(missing_headings(header, headings), ["## Validation"])

    def test_boundary_headings_detection(self):
        headings = ["## Change", "## Validation"]

        # "### Change" line does not count
        level3_header = "### Change\n\n## Validation\n"
        self.assertEqual(missing_headings(level3_header, headings), ["## Change"])

        # "see ## Change" mid-line does not count
        midline_header = "Please see ## Change for details.\n\n## Validation\n"
        self.assertEqual(missing_headings(midline_header, headings), ["## Change"])

        # Heading line with trailing spaces counts
        trailing_spaces_header = "## Change   \n\n## Validation  \n"
        self.assertEqual(missing_headings(trailing_spaces_header, headings), [])

        # Empty header -> all headings missing
        self.assertEqual(missing_headings("", headings), headings)

    def test_boundary_template_headings_extraction(self):
        text = "### Change\nsee ## Change\n## Change  \n## Validation\n"
        self.assertEqual(template_headings(text), ["## Change", "## Validation"])
