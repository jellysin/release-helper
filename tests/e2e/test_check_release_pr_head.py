"""Exercise the check action's entry point on a release PR head whose version tag is not cut yet."""

import json
import subprocess  # nosec B404
import sys
from pathlib import Path

from jellysin_tooling.github import command
from tests.fixtures import Workspace

TOOL = Path(__file__).resolve().parents[2] / "tools.py"
WORKFLOW = (
    "- uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
    "- uses: jellysin/release-helper/actions/prepare@0123456789abcdef0123456789abcdef01234567\n"
)


class CheckReleasePrHeadTests(Workspace):
    """A release-please branch bumps the manifest; prepare-release cuts its tag only after the merge."""

    def git(self, *arguments):
        """Run bounded git in the fixture repository with signing disabled locally."""
        return command(["git", *arguments], cwd=str(self.repo))

    def write_manifest(self, version):
        """Record one package version the way release-please writes it."""
        manifest = json.dumps({".": version}, indent=2) + "\n"
        (self.repo / ".release-please-manifest.json").write_text(manifest, encoding="utf-8")

    def commit(self, message):
        """Commit every change and return the new commit."""
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        """Release 1.0.0 on main, a later main commit, then the release PR head for 1.1.0."""
        super().setUp()
        self.repo = self.root / "consumer"
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
            self.git("config", key, value)
        for key in ("tag.gpgSign", "commit.gpgSign"):
            self.git("config", key, "false")
        for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md"):
            (self.repo / name).write_text("Doc\n", encoding="utf-8")
        (self.repo / "renovate.json").write_text("{}", encoding="utf-8")
        (self.repo / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW, encoding="utf-8")
        self.write_manifest("1.0.0")
        self.release = self.commit("chore(main): release 1.0.0")
        config = {"packages": {".": {"package-name": "consumer"}}, "last-release-sha": self.release}
        (self.repo / "release-please-config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self.later = self.commit("feat: pin the last release")
        self.git("checkout", "-q", "-b", "release-please--branches--main")
        self.write_manifest("1.1.0")
        self.head = self.commit("chore(main): release 1.1.0")

    def check_policy(self):
        """Run the exact check-policy entry point the check composite action runs."""
        # Fixed CLI and fixture directory, argument list without a shell.
        return subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            [sys.executable, str(TOOL), "check-policy", "--directory", str(self.repo)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_release_pr_head_without_its_tag_passes_against_previous_tag(self):
        """The 1.1.0 tag is absent; last-release-sha equals the 1.0.0 tag commit."""
        self.git("tag", "-a", "v1.0.0", "-m", "v1.0.0", self.release)
        result = self.check_policy()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", self.git("tag", "--list", "v1.1.0"))

    def test_release_pr_head_with_stale_sha_still_fails(self):
        """The 1.0.0 tag sits on a later commit, so the pinned sha is older than the release."""
        self.git("tag", "-a", "v1.0.0", "-m", "v1.0.0", self.later)
        result = self.check_policy()
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("older than release 1.0.0 tag commit", result.stderr)
        self.assertIn(self.release, result.stderr)
        self.assertIn(self.later, result.stderr)

    def test_release_pr_head_without_any_release_tag_fails_closed(self):
        """Neither the head version nor the previous version has a tag."""
        result = self.check_policy()
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("Release tag 'v1.1.0' is absent", result.stderr)
