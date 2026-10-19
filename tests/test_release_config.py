"""Tests for release-please configuration schema validation and stale last-release-sha checks."""

from pathlib import Path
from unittest.mock import patch

from jellysin_tooling import releaseconfig
from jellysin_tooling.common import ValidationError, read_json
from jellysin_tooling.github import command
from tests.fixtures import Workspace
from tests.test_tool_name import ROOT, message


class ReleaseConfigSchemaTests(Workspace):
    def test_repo_own_config_passes_validation(self):
        config = read_json(ROOT / "release-please-config.json")
        releaseconfig.validate_config(config)

    def test_unknown_top_level_key_fails_naming_it(self):
        config = {
            "packages": {".": {}},
            "unexpected_property": 123,
        }
        with self.assertRaisesRegex(ValidationError, "unexpected_property"):
            releaseconfig.validate_config(config)

    def test_wrong_type_fails_with_path(self):
        config = {
            "packages": {".": {}},
            "draft": "yes",
        }
        with self.assertRaisesRegex(ValidationError, r"\$\.draft"):
            releaseconfig.validate_config(config)

    def test_enum_violation_fails(self):
        config = {
            "packages": {".": {}},
            "changelog-type": "custom",
        }
        with self.assertRaisesRegex(ValidationError, r"changelog-type"):
            releaseconfig.validate_config(config)

    def test_tampered_vendored_schema_digest_refused(self):
        tampered = self.root / "tampered-schema.json"
        tampered.write_text('{"type": "object"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "Schema digest mismatch"):
            releaseconfig.load_schema(tampered)

    def test_schema_with_unknown_keyword_refused(self):
        schema = {
            "type": "object",
            "unsupportedKeyword": True,
        }
        with self.assertRaisesRegex(ValidationError, "Unsupported schema keyword 'unsupportedKeyword'"):
            releaseconfig.validate_config({"packages": {}}, schema=schema)

    def test_nested_path_in_anyof_failure(self):
        config = {
            "packages": {
                ".": {
                    "extra-files": [123],
                }
            }
        }
        with self.assertRaisesRegex(ValidationError, r"\$\.packages\[\"\.\"\]\.extra-files\[0\]"):
            releaseconfig.validate_config(config)

    def test_composition_inside_anyof_refused(self):
        definitions = {"Text": {"type": "string"}}
        for candidate in (
            {"$ref": "#/definitions/Text"},
            {"allOf": [{"type": "integer"}]},
            {"type": "object", "properties": {"x": {"anyOf": [{"type": "integer"}]}}},
        ):
            schema = {"definitions": definitions, "anyOf": [candidate]}
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValidationError, "inside anyOf"):
                releaseconfig.validate_config({"x": "text"} if "properties" in candidate else 5, schema=schema)
        releaseconfig.validate_config(5, schema={"anyOf": [{"type": "string"}, {"type": "integer"}]})

    def test_schema_ref_and_type_checks(self):
        with self.assertRaisesRegex(ValidationError, "Only local"):
            releaseconfig.validate_config({}, schema={"$ref": "https://example.com/schema"})
        with self.assertRaisesRegex(ValidationError, "Unresolved definition"):
            releaseconfig.validate_config({}, schema={"$ref": "#/definitions/Missing", "definitions": {}})
        with self.assertRaisesRegex(ValidationError, "Unsupported schema type"):
            releaseconfig.validate_config({}, schema={"type": "unknown_type"})

    def test_child_path_formatting(self):
        self.assertEqual(releaseconfig.child_path("$", "packages"), "$.packages")
        self.assertEqual(releaseconfig.child_path("$.packages", "."), '$.packages["."]')
        self.assertEqual(releaseconfig.child_path("$.items", 0), "$.items[0]")
        self.assertEqual(releaseconfig.child_path("$", "has.dot"), '$["has.dot"]')

    def test_scalar_types_and_boolean_schemas(self):
        releaseconfig.validate_config(10, schema={"type": "number"})
        releaseconfig.validate_config(10, schema={"type": "integer"})
        releaseconfig.validate_config(None, schema={"type": "null"})
        with self.assertRaisesRegex(ValidationError, "Disallowed"):
            releaseconfig.validate_config({}, schema=False)
        with self.assertRaisesRegex(ValidationError, "Disallowed"):
            releaseconfig.validate_config(
                {"items": [1]}, schema={"type": "object", "properties": {"items": {"items": False}}}
            )
        # Boolean True passes
        releaseconfig.validate_config({}, schema=True)
        # additionalProperties with schema
        releaseconfig.validate_config(
            {"extra": "val"},
            schema={"type": "object", "additionalProperties": {"type": "string"}},
        )
        # additionalProperties with True
        releaseconfig.validate_config(
            {"extra": "val"},
            schema={"type": "object", "additionalProperties": True},
        )
        # non-dict schema in check_schema_keywords
        releaseconfig.check_schema_keywords("not-dict", "$")


class StaleLastReleaseShaTests(Workspace):
    def make_git_repo(self):
        repo_dir = self.root / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        command(["git", "init", "-b", "main"], cwd=str(repo_dir))
        command(["git", "config", "user.name", "Test User"], cwd=str(repo_dir))
        command(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir))

        file1 = repo_dir / "file.txt"
        file1.write_text("initial\n", encoding="utf-8")
        command(["git", "add", "."], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Commit 1"], cwd=str(repo_dir))
        commit1 = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))

        file1.write_text("second\n", encoding="utf-8")
        command(["git", "add", "."], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Commit 2"], cwd=str(repo_dir))
        commit2 = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))

        return repo_dir, commit1, commit2

    def test_positive_sha_equals_tag_commit_passes(self):
        repo_dir, _commit1, commit2 = self.make_git_repo()
        command(["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0", commit2], cwd=str(repo_dir))
        config = {"last-release-sha": commit2}
        manifest = {".": "1.0.0"}
        releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_negative_sha_is_parent_of_tag_commit_fails_with_both_shas(self):
        repo_dir, commit1, commit2 = self.make_git_repo()
        command(["git", "tag", "-a", "v1.0.1", "-m", "v1.0.1", commit2], cwd=str(repo_dir))
        config = {"last-release-sha": commit1}
        manifest = {".": "1.0.1"}
        with self.assertRaises(ValidationError) as ctx:
            releaseconfig.stale_last_release_sha(repo_dir, config, manifest)
        err = str(ctx.exception)
        self.assertIn(commit1, err)
        self.assertIn(commit2, err)
        self.assertIn("1.0.1", err)
        self.assertIn("older than release", err)

    def test_boundary_sha_descendant_of_tag_passes(self):
        repo_dir, commit1, commit2 = self.make_git_repo()
        command(["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0", commit1], cwd=str(repo_dir))
        config = {"last-release-sha": commit2}
        manifest = {".": "1.0.0"}
        releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_unrelated_sha_fails_not_in_tag_history(self):
        repo_dir, commit1, commit2 = self.make_git_repo()
        command(["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0", commit2], cwd=str(repo_dir))
        command(["git", "checkout", "--orphan", "unrelated-branch"], cwd=str(repo_dir))
        orphan_file = repo_dir / "orphan.txt"
        orphan_file.write_text("orphan\n", encoding="utf-8")
        command(["git", "add", "orphan.txt"], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Unrelated commit"], cwd=str(repo_dir))
        unrelated_commit = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))

        config = {"last-release-sha": unrelated_commit}
        manifest = {".": "1.0.0"}
        with self.assertRaisesRegex(ValidationError, "not in tag history"):
            releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_missing_tag_fails_closed(self):
        repo_dir, commit1, _commit2 = self.make_git_repo()
        config = {"last-release-sha": commit1}
        manifest = {".": "1.0.0"}
        with self.assertRaisesRegex(ValidationError, "Tag 'v1.0.0' commit missing locally.*fetch-depth: 0"):
            releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_missing_last_release_sha_fails_closed(self):
        repo_dir, _commit1, commit2 = self.make_git_repo()
        command(["git", "tag", "-a", "v1.0.0", "-m", "v1.0.0", commit2], cwd=str(repo_dir))
        nonexistent = "0" * 40
        config = {"last-release-sha": nonexistent}
        manifest = {".": "1.0.0"}
        with self.assertRaisesRegex(ValidationError, "last-release-sha .* commit missing locally.*fetch-depth: 0"):
            releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_no_last_release_sha_makes_no_git_call(self):
        with patch("jellysin_tooling.releaseconfig.command") as mock_command:
            releaseconfig.stale_last_release_sha(Path("/nonexistent"), {}, {})
            mock_command.assert_not_called()

    def test_config_absent_does_nothing(self):
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        self.assertIsNone(releaseconfig.check(empty_dir))

    def test_manifest_absent_fails_when_config_present(self):
        repo_dir = self.root / "missing-manifest"
        repo_dir.mkdir()
        (repo_dir / "release-please-config.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "Missing required .release-please-manifest.json"):
            releaseconfig.check(repo_dir)

    def test_include_v_in_tag_false_handled(self):
        repo_dir, _commit1, commit2 = self.make_git_repo()
        command(["git", "tag", "-a", "1.0.0", "-m", "1.0.0", commit2], cwd=str(repo_dir))
        config = {
            "last-release-sha": commit2,
            "include-v-in-tag": False,
        }
        manifest = {".": "1.0.0"}
        releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_include_component_in_tag_true_refused(self):
        repo_dir, _commit1, commit2 = self.make_git_repo()
        config = {
            "last-release-sha": commit2,
            "include-component-in-tag": True,
        }
        manifest = {".": "1.0.0"}
        with self.assertRaisesRegex(ValidationError, "include-component-in-tag.*unsupported"):
            releaseconfig.stale_last_release_sha(repo_dir, config, manifest)

    def test_unsupported_manifest_package_refused(self):
        repo_dir, _commit1, commit2 = self.make_git_repo()
        config = {"last-release-sha": commit2}
        manifest = {".": "1.0.0", "other": "1.0.0"}
        with self.assertRaisesRegex(ValidationError, "Only manifest package '.' is supported"):
            releaseconfig.stale_last_release_sha(repo_dir, config, manifest)


class CheckPolicyCliEndToEndTests(Workspace):
    def prepare_compliant_repo(self):
        repo_dir = self.root / "compliant-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        command(["git", "init", "-b", "main"], cwd=str(repo_dir))
        command(["git", "config", "user.name", "Test User"], cwd=str(repo_dir))
        command(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir))

        for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md", "renovate.json"):
            (repo_dir / name).write_text("{}" if name.endswith("json") else "Doc\n", encoding="utf-8")

        workflows = repo_dir / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text(
            "- uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
            encoding="utf-8",
        )

        command(["git", "add", "."], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir))
        commit1 = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))

        (repo_dir / "README.md").write_text("Updated\n", encoding="utf-8")
        command(["git", "add", "README.md"], cwd=str(repo_dir))
        command(["git", "commit", "-m", "Second commit"], cwd=str(repo_dir))
        commit2 = command(["git", "rev-parse", "HEAD"], cwd=str(repo_dir))

        command(["git", "tag", "-a", "v1.0.1", "-m", "v1.0.1", commit2], cwd=str(repo_dir))
        return repo_dir, commit1, commit2

    def test_check_policy_cli_fails_on_stale_sha(self):
        repo_dir, commit1, commit2 = self.prepare_compliant_repo()
        config = {
            "packages": {
                ".": {
                    "package-name": "test-pkg",
                }
            },
            "last-release-sha": commit1,
        }
        (repo_dir / "release-please-config.json").write_text(
            json_bytes(config).decode("utf-8"),
            encoding="utf-8",
        )
        (repo_dir / ".release-please-manifest.json").write_text(
            '{\n  ".": "1.0.1"\n}\n',
            encoding="utf-8",
        )

        status, stderr = message(["check-policy", "--directory", str(repo_dir)])
        self.assertEqual(status, 1)
        self.assertIn("older than release 1.0.1 tag commit", stderr)
        self.assertIn(commit1, stderr)
        self.assertIn(commit2, stderr)


def json_bytes(value):
    import json

    return (json.dumps(value, indent=2) + "\n").encode()
