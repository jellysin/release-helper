#!/usr/bin/env python3
"""Replay release-audit gate failures across default and release pull request contexts.

Validates that governance enforcement passes for clean trees and fails predictably
when end-to-end evidence or release test levels are missing.
"""

import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

GIT = shutil.which("git")

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


def run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in cwd using the resolved GIT binary and return the process."""
    if GIT is None:
        raise RuntimeError("expected a git binary on PATH")
    # Fixed git binary from shutil.which, argument list, no shell, script-controlled arguments.
    res = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        [GIT, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed with exit {res.returncode}: {res.stderr.strip()}")
    return res


def collect_overlay_paths(repo_root: Path) -> list[str]:
    """Collect tracked changes vs HEAD and untracked unignored paths."""
    diff_res = run_git(repo_root, "diff", "--no-renames", "--name-only", "-z", "HEAD")
    untracked_res = run_git(repo_root, "ls-files", "-z", "--others", "--exclude-standard")
    diff_paths = {rel for rel in diff_res.stdout.split("\0") if rel}
    untracked_paths = {rel for rel in untracked_res.stdout.split("\0") if rel}
    return sorted(diff_paths | untracked_paths)


def overlay_working_tree(repo_root: Path, tree: Path) -> list[str]:
    """Overlay staged and unstaged changes plus untracked files, synced from the working tree."""
    paths = collect_overlay_paths(repo_root)
    for rel in paths:
        src = repo_root / rel
        dst = tree / rel
        # A path that is now a directory or submodule matches neither branch and is left alone.
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif not src.exists() and dst.exists():
            dst.unlink()

    run_git(tree, "add", "-A")
    return paths


def create_replay_tree(repo_root: Path, temp_dir: Path) -> Path:
    """Create a detached git worktree of HEAD, the only tree shape the offline audit accepts.

    A plain clone fails the audit's ruleset check, so there is no fallback: a worktree that
    cannot be created stops the replay with git's message.
    """
    tree = temp_dir / "tree"
    run_git(repo_root, "worktree", "add", "--detach", str(tree), "HEAD")
    return tree


def remove_replay_tree(repo_root: Path, tree: Path) -> bool:
    """Remove the replay worktree and prune its records; report each failure without raising."""
    removed = True
    for args in (("worktree", "remove", "--force", str(tree)), ("worktree", "prune")):
        result = run_git(repo_root, *args, check=False)
        if result.returncode != 0:
            print(
                f"cleanup: git {' '.join(args[:2])} exit {result.returncode}: {result.stderr.strip()}",
                file=sys.stderr,
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
    # Engine path from the gate's argument, resolved by shutil.which in resolve_engine; list argv, no shell.
    # Each finding is suppressed on its own line: the call (audit rule) and the argv (tainted-argument rule).
    argv = [engine, "audit", "--offline"]
    res = subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        argv,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
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
    repo_root = Path(run_git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip())

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
