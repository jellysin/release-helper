"""Tests for overlay_working_tree in tests/release_audit_replay.py."""

from pathlib import Path

from jellysin_tooling.github import command
from tests.fixtures import Workspace
from tests.release_audit_replay import overlay_working_tree
from tests.test_release_config import init_git_repo

BASE_FILES = {
    "keep.txt": "base keep\n",
    "gone.txt": "base gone\n",
    "mv.txt": "base mv\n",
    "edit.txt": "base edit\n",
    "drop.txt": "base drop\n",
    "both.txt": "base both\n",
}


class ReleaseAuditReplayOverlayTests(Workspace):
    """Validate overlay_working_tree behavior across staged, unstaged, and untracked changes."""

    def setup_repos(self) -> tuple[Path, Path]:
        """Create a source git repo with committed base files and a replay tree with base files written."""
        src = self.root / "src"
        tree = self.root / "tree"
        src.mkdir(parents=True, exist_ok=True)
        tree.mkdir(parents=True, exist_ok=True)

        init_git_repo(src)
        for name, content in BASE_FILES.items():
            (src / name).write_text(content, encoding="utf-8")
        (src / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        command(["git", "add", "-A"], cwd=str(src))
        command(["git", "commit", "-m", "base"], cwd=str(src))

        init_git_repo(tree)
        for name, content in BASE_FILES.items():
            (tree / name).write_text(content, encoding="utf-8")
        (tree / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")

        return src, tree

    def test_overlay_positive_staged_unstaged_and_untracked(self):
        src, tree = self.setup_repos()

        # Staged modifications, addition, removal, and rename in src.
        (src / "keep.txt").write_text("updated keep\n", encoding="utf-8")
        command(["git", "add", "keep.txt"], cwd=str(src))
        (src / "new.txt").write_text("content new\n", encoding="utf-8")
        command(["git", "add", "new.txt"], cwd=str(src))
        command(["git", "rm", "gone.txt"], cwd=str(src))
        command(["git", "mv", "mv.txt", "moved.txt"], cwd=str(src))

        # Unstaged edit and deletion in src.
        (src / "edit.txt").write_text("updated edit\n", encoding="utf-8")
        (src / "drop.txt").unlink()

        # Untracked file in src.
        (src / "untracked.txt").write_text("content untracked\n", encoding="utf-8")

        paths = overlay_working_tree(src, tree)

        # Assert tree content and file presence / absence.
        self.assertEqual((tree / "keep.txt").read_text(encoding="utf-8"), "updated keep\n")
        self.assertEqual((tree / "edit.txt").read_text(encoding="utf-8"), "updated edit\n")
        self.assertTrue((tree / "new.txt").is_file())
        self.assertEqual((tree / "new.txt").read_text(encoding="utf-8"), "content new\n")
        self.assertTrue((tree / "moved.txt").is_file())
        self.assertEqual((tree / "moved.txt").read_text(encoding="utf-8"), "base mv\n")
        self.assertTrue((tree / "untracked.txt").is_file())
        self.assertEqual((tree / "untracked.txt").read_text(encoding="utf-8"), "content untracked\n")
        self.assertFalse((tree / "gone.txt").exists())
        self.assertFalse((tree / "mv.txt").exists())
        self.assertFalse((tree / "drop.txt").exists())

        expected_paths = [
            "drop.txt",
            "edit.txt",
            "gone.txt",
            "keep.txt",
            "moved.txt",
            "mv.txt",
            "new.txt",
            "untracked.txt",
        ]
        self.assertEqual(paths, expected_paths)

    def test_overlay_negative_ignored_and_unchanged_base_files(self):
        src, tree = self.setup_repos()

        (src / "ignored.txt").write_text("ignored content\n", encoding="utf-8")
        orig_bytes = (tree / "keep.txt").read_bytes()

        paths = overlay_working_tree(src, tree)

        self.assertEqual(paths, [])
        self.assertFalse((tree / "ignored.txt").exists())
        self.assertEqual((tree / "keep.txt").read_bytes(), orig_bytes)
        self.assertEqual((tree / "keep.txt").read_text(encoding="utf-8"), BASE_FILES["keep.txt"])

    def test_overlay_boundary_staged_plus_unstaged_and_staged_then_unlinked(self):
        src, tree = self.setup_repos()

        # both.txt: staged edit then further unstaged edit.
        (src / "both.txt").write_text("staged both\n", encoding="utf-8")
        command(["git", "add", "both.txt"], cwd=str(src))
        (src / "both.txt").write_text("working both\n", encoding="utf-8")

        # staged_then_gone.txt: staged new file then unlinked from working tree.
        (src / "staged_then_gone.txt").write_text("staged new\n", encoding="utf-8")
        command(["git", "add", "staged_then_gone.txt"], cwd=str(src))
        (src / "staged_then_gone.txt").unlink()

        paths = overlay_working_tree(src, tree)

        self.assertEqual(paths, ["both.txt"])
        self.assertEqual((tree / "both.txt").read_text(encoding="utf-8"), "working both\n")
        self.assertFalse((tree / "staged_then_gone.txt").exists())
