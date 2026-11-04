"""Exercise the prepare action's entry point against real release-merge history."""

import contextlib
import io
import runpy
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling import cli, github
from tests import test_prepare as prepare_fixtures
from tests.fixtures import Workspace


class PrepareReleaseMergeTests(Workspace):
    """Authorize only the merged release PR, even when later main files still match."""

    def setUp(self):
        """Create an actual release branch merge and a later unchanged-version main commit."""
        super().setUp()
        self.git("init", "-q", "-b", "main")
        (self.root / "version.txt").write_text("1.2.2\n", encoding="utf-8")
        self.commit("initial version")
        self.git("checkout", "-q", "-b", "release-please--branches--main")
        (self.root / "version.txt").write_text("1.2.3\n", encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(prepare_fixtures.CHANGELOG, encoding="utf-8")
        self.commit("chore(main): release 1.2.3")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-m", "Merge release PR", "release-please--branches--main")
        self.merge = self.git("rev-parse", "HEAD")
        (self.root / "README.md").write_text("Later main change\n", encoding="utf-8")
        self.later = self.commit("docs: later main change")
        self.git("update-ref", "refs/remotes/origin/main", self.later)
        self.remote = prepare_fixtures.Remote()
        pull = prepare_fixtures.release_pull(self.merge)
        self.remote.pulls = {self.merge: [pull], self.later: [pull]}

    def git(self, *arguments):
        """Reuse the bounded fixture git runner, with signing and hooks disabled locally."""
        return prepare_fixtures.GitHistoryTests.git(self, *arguments)

    def commit(self, message):
        """Reuse the fixture commit helper without changing inherited credential configuration."""
        return prepare_fixtures.GitHistoryTests.commit(self, message)

    def prepare(self, commit):
        """Execute tools.py prepare; serve all forge operations from the existing fake."""
        tool = Path(cli.__file__).resolve().parents[1] / "tools.py"
        arguments = [
            str(tool),
            "prepare",
            "--repo",
            prepare_fixtures.REPO,
            "--tag",
            prepare_fixtures.TAG,
            "--commit",
            commit,
            "--changelog-path",
            "CHANGELOG.md",
        ]
        original = github.command_bytes

        def command_bytes(arguments, cwd=None, timeout=120, limit=github.MAX_ASSET):
            """Intercept gh before execution and retain real bounded git reads in the fixture."""
            if arguments[0] == "gh":
                self.assertEqual(["gh", "api", "--method", "POST"], arguments[:4])
                return (self.remote.run(arguments) or "").encode()
            self.assertEqual("git", arguments[0])
            return original(arguments, cwd=self.root, timeout=timeout, limit=limit)

        stderr = io.StringIO()
        with (
            patch("sys.argv", arguments),
            patch("jellysin_tooling.cli.GitHub", return_value=self.remote),
            patch("jellysin_tooling.github.command_bytes", side_effect=command_bytes),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                runpy.run_path(str(tool), run_name="__main__")
            except SystemExit as exc:
                return exc.code, stderr.getvalue()
        return 0, stderr.getvalue()

    def test_release_pr_merge_is_tagged_and_draft_is_prepared(self):
        """Tag exactly the one merged pending release PR and preserve its changelog notes."""
        self.assertEqual(2, len(self.git("rev-list", "--parents", "-n", "1", self.merge).split()) - 1)
        status, stderr = self.prepare(self.merge)
        self.assertEqual(0, status, stderr)
        self.assertEqual({prepare_fixtures.TAG: self.merge}, self.remote.tags)
        self.assertEqual([prepare_fixtures.REFS, prepare_fixtures.RELEASES], self.remote.endpoints())
        self.assertEqual(prepare_fixtures.SECTION, self.remote.payloads[0]["body"])
        self.assertTrue(self.remote.releases[0]["draft"])

    def test_later_main_commit_is_refused_before_any_tag_write(self):
        """Plant the ancestor-only regression with identical version and changelog files."""
        for path in ("version.txt", "CHANGELOG.md"):
            self.assertEqual(self.git("show", f"{self.merge}:{path}"), self.git("show", f"{self.later}:{path}"))
        status, stderr = self.prepare(self.later)
        self.assertEqual(1, status, stderr)
        self.assertIn("Commit is not the merge commit of a merged release pull request", stderr)
        self.assertEqual({}, self.remote.tags, "refused commit must leave no remote tag ref")
        self.assertEqual([], self.remote.writes)
        self.assertEqual([], self.remote.releases)
        self.assertEqual("", self.git("for-each-ref", "refs/tags/"))
