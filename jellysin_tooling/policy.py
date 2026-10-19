"""Language-neutral repository checks and Python size limits."""

import ast
import re
from pathlib import Path

from . import releaseconfig
from .common import read_json, require


def check_repository(root):
    root = Path(root)
    for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md", "renovate.json"):
        require((root / name).is_file(), "Missing required repository document: " + name)
    for path in root.glob("*.json"):
        read_json(path)
    for folder in (root / ".github" / "workflows", root / "actions"):
        for path in folder.rglob("*.yml"):
            contents = path.read_text(encoding="utf-8")
            for target in re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", contents, re.MULTILINE):
                if target.startswith("./"):
                    continue
                require(
                    re.fullmatch(r"[A-Za-z0-9_.\-/]+@[0-9a-f]{40}", target), "Actions must use complete commit SHAs"
                )
                require(not target.endswith("@" + "0" * 40), "Unresolved action pin")
            require("pull_request_target:" not in contents, "Privileged PR workflow needs explicit security review")
    for folder in ("jellysin_tooling", "tools"):
        # Only conventional source files; do not traverse generated bin/obj trees.
        for path in (root / folder).glob("*.py"):
            check_python_functions(path)
    releaseconfig.check(root)


def check_python_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            require(node.end_lineno - node.lineno + 1 <= 120, f"Function exceeds 120 lines: {path.name}:{node.name}")
            require(
                sum(isinstance(child, ast.stmt) for child in ast.walk(node)) <= 60,
                f"Function exceeds 60 statements: {path.name}:{node.name}",
            )
