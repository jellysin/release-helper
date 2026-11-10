"""Tests for planted_commit context manager and CASES configuration in tests/release_audit_replay.py."""

from jellysin_tooling.github import command
from tests.fixtures import Workspace
from tests.release_audit_replay import CASES, planted_commit
from tests.test_release_config import init_git_repo


class ReleaseAuditReplayPlantTests(Workspace):
    """Verify planted commit lifecycle on release tag heads and CASES plant configurations."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        init_git_repo(self.repo)

        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        command(["git", "add", "base.txt"], cwd=str(self.repo))
        command(["git", "commit", "-m", "base"], cwd=str(self.repo))
        self.tagged_sha = command(["git", "rev-parse", "HEAD"], cwd=str(self.repo))
        command(["git", "tag", "v9.9.9", self.tagged_sha], cwd=str(self.repo))

        (self.repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        command(["git", "add", "staged.txt"], cwd=str(self.repo))

    def test_planted_commit_positive(self):
        with planted_commit(self.repo, "fix: planted"):
            log_output = command(["git", "log", "--format=%s", "v9.9.9..HEAD"], cwd=str(self.repo))
            self.assertEqual(log_output, "fix: planted")

            diff_tree = command(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                cwd=str(self.repo),
            )
            self.assertEqual(diff_tree, "")

            staged = command(["git", "diff", "--cached", "--name-only"], cwd=str(self.repo))
            self.assertEqual(staged, "staged.txt")

            symbolic_inside = command(["git", "rev-parse", "--symbolic-full-name", "HEAD"], cwd=str(self.repo))
            self.assertEqual(symbolic_inside, "HEAD")
            branch_head = command(["git", "rev-parse", "refs/heads/main"], cwd=str(self.repo))
            self.assertEqual(branch_head, self.tagged_sha)

        head_after = command(["git", "rev-parse", "HEAD"], cwd=str(self.repo))
        self.assertEqual(head_after, self.tagged_sha)
        symbolic_after = command(["git", "rev-parse", "--symbolic-full-name", "HEAD"], cwd=str(self.repo))
        self.assertEqual(symbolic_after, "refs/heads/main")
        staged_after = command(["git", "diff", "--cached", "--name-only"], cwd=str(self.repo))
        self.assertEqual(staged_after, "staged.txt")

    def test_planted_commit_negative_none_subject(self):
        with planted_commit(self.repo, None):
            self.assertEqual(command(["git", "rev-parse", "HEAD"], cwd=str(self.repo)), self.tagged_sha)
            log_output = command(["git", "log", "v9.9.9..HEAD"], cwd=str(self.repo))
            self.assertEqual(log_output, "")

        self.assertEqual(command(["git", "rev-parse", "HEAD"], cwd=str(self.repo)), self.tagged_sha)

    def test_planted_commit_boundary_exception_restores_head(self):
        with self.assertRaises(RuntimeError):
            with planted_commit(self.repo, "fix: planted"):
                raise RuntimeError("simulated error inside block")

        self.assertEqual(command(["git", "rev-parse", "HEAD"], cwd=str(self.repo)), self.tagged_sha)
        symbolic_after = command(["git", "rev-parse", "--symbolic-full-name", "HEAD"], cwd=str(self.repo))
        self.assertEqual(symbolic_after, "refs/heads/main")

    def test_planted_commit_boundary_detached_head_stays_detached(self):
        command(["git", "checkout", "-q", "--detach"], cwd=str(self.repo))
        with planted_commit(self.repo, "fix: planted"):
            pass

        symbolic_after = command(["git", "rev-parse", "--symbolic-full-name", "HEAD"], cwd=str(self.repo))
        self.assertEqual(symbolic_after, "HEAD")
        self.assertEqual(command(["git", "rev-parse", "HEAD"], cwd=str(self.repo)), self.tagged_sha)
        staged_after = command(["git", "diff", "--cached", "--name-only"], cwd=str(self.repo))
        self.assertEqual(staged_after, "staged.txt")

    def test_planted_commit_boundary_moved_head_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            with planted_commit(self.repo, "fix: planted"):
                command(["git", "update-ref", "--no-deref", "HEAD", self.tagged_sha], cwd=str(self.repo))

    def test_cases_declared_plant_commit(self):
        cases_with_plant = [case for case in CASES if case.get("plant_commit") is not None]
        self.assertEqual(len(cases_with_plant), 1)
        planted_case = cases_with_plant[0]
        self.assertTrue(planted_case["plant_commit"].startswith("fix: "))
        self.assertIsNone(planted_case["plant_old"])
