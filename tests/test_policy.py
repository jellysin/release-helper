"""Policy gates prevent accidental unpinned Actions and unreviewable functions."""

from jellysin_tooling.common import ValidationError
from jellysin_tooling.policy import check_repository
from tests.fixtures import Workspace
from tests.test_tool_name import ROOT, message


class PolicyTests(Workspace):
    def prepare(self):
        for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md", "renovate.json"):
            (self.root / name).write_text("{}" if name.endswith("json") else "Fixture\n")
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        return workflows / "ci.yml"

    def test_requires_documents_and_immutable_action_pins(self):
        with self.assertRaises(ValidationError):
            check_repository(self.root)
        workflow = self.prepare()
        for target in ("actions/checkout@main", "actions/checkout@" + "0" * 40):
            workflow.write_text("- uses: " + target + "\n")
            with self.subTest(target=target), self.assertRaises(ValidationError):
                check_repository(self.root)
        workflow.write_text("- uses: actions/checkout@" + "a" * 40 + "\n- uses: ./local\n")
        check_repository(self.root)

    def test_repository_root_policy_passes(self):
        check_repository(ROOT)

    def test_function_and_privileged_event_limits(self):
        workflow = self.prepare()
        workflow.write_text("pull_request_target:\n")
        with self.assertRaises(ValidationError):
            check_repository(self.root)
        workflow.write_text("pull_request:\n")
        source = self.root / "jellysin_tooling"
        source.mkdir()
        (source / "long.py").write_text("def too_large():\n" + "    pass\n" * 121)
        with self.assertRaisesRegex(ValidationError, "120"):
            check_repository(self.root)
        (source / "long.py").write_text("def too_complex():\n" + "    pass\n" * 61)
        with self.assertRaisesRegex(ValidationError, "60"):
            check_repository(self.root)

    def test_sizes_apply_to_top_level_repository_helpers(self):
        self.prepare().write_text("pull_request:\n")
        source = self.root / "tools"
        source.mkdir()
        helper = source / "helper.py"
        helper.write_text("async def helper():\n    return True\n")
        # A .NET helper project may have large generated files; never recurse into it.
        generated = source / "CodePolicy" / "obj"
        generated.mkdir(parents=True)
        (generated / "generated.py").write_text("def oversized():\n" + "    pass\n" * 121)
        check_repository(self.root)
        helper.write_text("async def too_long():\n" + "\n" * 119 + "    return True\n")
        with self.assertRaisesRegex(ValidationError, "120 lines: helper.py:too_long"):
            check_repository(self.root)
        helper.write_text("async def too_complex():\n" + "    pass\n" * 61)
        with self.assertRaisesRegex(ValidationError, "60 statements: helper.py:too_complex"):
            check_repository(self.root)


class PolicyLimitTests(PolicyTests):
    def test_functions_exactly_at_the_limits_pass(self):
        self.prepare().write_text("pull_request:\n")
        source = self.root / "jellysin_tooling"
        source.mkdir()
        # 120 lines from def to the last statement; 60 statements, the def itself included.
        (source / "long.py").write_text("def at_line_limit():\n" + "\n" * 118 + "    pass\n")
        (source / "busy.py").write_text("def at_statement_limit():\n" + "    pass\n" * 59)
        check_repository(self.root)

    def test_check_policy_command_passes_a_compliant_repository_and_refuses_a_missing_document(self):
        self.prepare().write_text("pull_request:\n")
        self.assertEqual(message(["check-policy", "--directory", str(self.root)]), (0, ""))
        (self.root / "SECURITY.md").unlink()
        self.assertEqual(
            message(["check-policy", "--directory", str(self.root)]),
            (1, "jellysin-release-helper: Missing required repository document: SECURITY.md\n"),
        )
