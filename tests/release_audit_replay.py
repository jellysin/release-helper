#!/usr/bin/env python3
"""Replay release-audit gate failures across default and release pull request contexts.

Validates that governance enforcement passes for clean trees and fails predictably
when end-to-end evidence or release test levels are missing.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LOAD_SOAK_BLOCK = """    # Repeated catalog publish cycles and volume at the manifest limits.
    load_soak:
      paths: [tests/load/**]
      job: checks
      run: unittest discover -s tests"""

CASES = [
    {
        "name": "fixed-tree",
        "plant_old": None,
        "plant_new": "",
        "default": (0, ["[PASS] Release end-to-end evidence", "[PASS] Release test levels"]),
        "release": (0, ["[PASS] Release end-to-end evidence", "[PASS] Release test levels"]),
    },
    {
        "name": "e2e-since",
        "plant_old": "since: 2625f4b711c3580a6e7d59cb854dbb11c16ec34a",
        "plant_new": "since: 17153e53b83d2b3c36d8542659b0edf51ea8495b",
        "default": (0, ["[WARN] Release end-to-end evidence"]),
        "release": (1, ["[FAIL] Release end-to-end evidence"]),
    },
    {
        "name": "recovery-restore-gap",
        "plant_old": "paths: [tests/test_release_recovery.py]",
        "plant_new": "paths: [tests/no_such_recovery.py]",
        "default": (0, ["[WARN] Release test levels", "recovery_restore"]),
        "release": (1, ["[FAIL] Release test levels", "recovery_restore"]),
    },
    {
        "name": "load-soak-removed",
        "plant_old": LOAD_SOAK_BLOCK,
        "plant_new": "",
        "default": (1, ["[FAIL] Release test levels", "load_soak"]),
        "release": (1, ["[FAIL] Release test levels", "load_soak"]),
    },
]


def resolve_engine(arg: str) -> str:
    """Resolve and validate the praetorctl binary from CLI arguments."""
    resolved = shutil.which(arg)
    if not resolved:
        print(f"Error: praetorctl engine not found for '{arg}'", file=sys.stderr)
        sys.exit(2)
    return resolved


def overlay_working_tree(repo_root: Path, tree: Path) -> None:
    """Overlay uncommitted modified, untracked, and deleted files onto the worktree."""
    raw_mod = subprocess.check_output(
        ["git", "ls-files", "-z", "--modified", "--others", "--exclude-standard"],
        cwd=str(repo_root),
        timeout=60,
    )
    for rel in raw_mod.split(b"\0"):
        if not rel:
            continue
        rel_str = rel.decode("utf-8")
        src = repo_root / rel_str
        dst = tree / rel_str
        # --modified also lists unstaged deletions; the deleted pass below removes those.
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    raw_del = subprocess.check_output(["git", "ls-files", "-z", "--deleted"], cwd=str(repo_root), timeout=60)
    for rel in raw_del.split(b"\0"):
        if not rel:
            continue
        dst = tree / rel.decode("utf-8")
        if dst.exists():
            dst.unlink()

    subprocess.check_call(["git", "add", "-A"], cwd=str(tree), timeout=60)


def create_replay_tree(repo_root: Path, temp_dir: Path) -> Path:
    """Create a detached git worktree of HEAD, the only tree shape the offline audit accepts.

    A plain clone fails the audit's ruleset check, so there is no fallback: a worktree that
    cannot be created stops the replay with git's message.
    """
    tree = temp_dir / "tree"
    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(tree), "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed with exit {result.returncode}: {result.stderr.strip()}")
    return tree


def remove_replay_tree(repo_root: Path, tree: Path) -> bool:
    """Remove the replay worktree and prune its records; report each failure without raising."""
    removed = True
    for command in (["git", "worktree", "remove", "--force", str(tree)], ["git", "worktree", "prune"]):
        result = subprocess.run(command, cwd=str(repo_root), capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            print(
                f"cleanup: {' '.join(command[:3])} exit {result.returncode}: {result.stderr.strip()}", file=sys.stderr
            )
            removed = False
    return removed


def run_audit(engine: str, cwd: Path, release_context: bool) -> tuple[int, str]:
    """Run praetorctl audit --offline in cwd under default or release PR environment."""
    env = os.environ.copy()
    env.pop("PRAETOR_RELEASE_PR", None)
    env.pop("GITHUB_HEAD_REF", None)
    if release_context:
        env["PRAETOR_RELEASE_PR"] = "true"
    res = subprocess.run(
        [engine, "audit", "--offline"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return res.returncode, res.stdout + res.stderr


def replay_case(engine: str, tree: Path, standards_orig: str, case: dict) -> bool:
    """Execute both default and release context audits for a single test case."""
    name = case["name"]
    plant_old = case["plant_old"]
    standards_path = tree / ".standards.yaml"
    standards_path.write_text(standards_orig, encoding="utf-8")

    if plant_old is not None:
        if standards_orig.count(plant_old) != 1:
            raise ValueError(f"Plant old text not found exactly once for case '{name}'")
        planted = standards_orig.replace(plant_old, case["plant_new"])
        standards_path.write_text(planted, encoding="utf-8")

    case_passed = True
    for context in ("default", "release"):
        expected_rc, required_substrings = case[context]
        rc, output = run_audit(engine, tree, release_context=(context == "release"))
        matched_text = all(sub in output for sub in required_substrings)
        if rc == expected_rc and matched_text:
            print(f"{name} {context} rc={rc} ok")
        else:
            print(f"{name} {context} rc={rc} MISMATCH")
            for line in output.splitlines()[-40:]:
                print(line)
            case_passed = False

    standards_path.write_text(standards_orig, encoding="utf-8")
    return case_passed


def main() -> None:
    """Replay all gate failure scenarios against a clean worktree."""
    if len(sys.argv) < 2:
        print("Usage: python3 tests/release_audit_replay.py <praetorctl>", file=sys.stderr)
        sys.exit(2)

    engine = resolve_engine(sys.argv[1])
    repo_root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True, timeout=60).strip())

    with tempfile.TemporaryDirectory() as td:
        tree = create_replay_tree(repo_root, Path(td))
        try:
            overlay_working_tree(repo_root, tree)
            standards_orig = (tree / ".standards.yaml").read_text(encoding="utf-8")
            results = [replay_case(engine, tree, standards_orig, case) for case in CASES]
        finally:
            cleaned = remove_replay_tree(repo_root, tree)
    if not (all(results) and cleaned):
        sys.exit(1)


if __name__ == "__main__":
    main()
