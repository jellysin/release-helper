"""The tool names itself once; requests, SBOMs and messages carry the project name."""

import contextlib
import io
import re
import tomllib
from pathlib import Path
from unittest.mock import Mock, patch

from jellysin_tooling.cli import main
from jellysin_tooling.common import TOOL_NAME, decode_json
from jellysin_tooling.github import GitHub
from tests.fixtures import Workspace

ROOT = Path(__file__).resolve().parents[1]
RETIRED = re.compile(r"jellysin-(?:plugin-)?tooling\b")


def message(arguments):
    stderr = io.StringIO()
    with (
        patch("sys.argv", ["tools.py", *arguments]),
        contextlib.redirect_stderr(stderr),
        contextlib.suppress(SystemExit),
    ):
        main()
    return stderr.getvalue()


class ToolNameTests(Workspace):
    def test_name_equals_the_project_name(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["name"], TOOL_NAME)

    def test_requests_carry_the_name(self):
        client = GitHub("")
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b"asset")
        client.download("https://github.com/x")
        self.assertEqual(TOOL_NAME, client.opener.open.call_args.args[0].get_header("User-agent"))

    def test_sbom_names_the_tool_and_its_format_generation(self):
        _, output = self.plugin()
        document = decode_json((output / "sbom.spdx.json").read_bytes())
        self.assertEqual(["Tool: jellysin-release-helper-1"], document["creationInfo"]["creators"])

    def test_both_failure_messages_start_with_the_name(self):
        manifest = self.root / "manifest.json"
        manifest.write_text("[1]", encoding="utf-8")
        refused = message(["validate-catalog", "--manifest", str(manifest)])
        unreadable = message(["validate-catalog", "--manifest", str(self.root / "missing")])
        self.assertRegex(refused, r"\Ajellysin-release-helper: \S")
        self.assertRegex(unreadable, r"\Ajellysin-release-helper: [A-Za-z]+Error: operation failed")
        self.assertNotIn("operation failed", refused)

    def test_no_source_line_keeps_a_retired_name(self):
        for planted in ('"User-Agent": "jellysin-plugin-tooling"', 'print(f"jellysin-tooling: {exc}")'):
            self.assertIsNotNone(RETIRED.search(planted), planted)
        self.assertIsNone(RETIRED.search("import jellysin_tooling"))
        sources = sorted((ROOT / "jellysin_tooling").glob("*.py")) + sorted((ROOT / "actions").rglob("*.yml"))
        self.assertGreater(len(sources), 15)
        for path in sources:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertIsNone(RETIRED.search(path.read_text(encoding="utf-8")))
