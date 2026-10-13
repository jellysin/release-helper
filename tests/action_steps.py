"""Run a composite action's shell step as a workflow runs it, with its inputs bound through the step's env.

The step text, the env names its inputs reach and their defaults all come from the action file, so a
test exercises what a consumer's workflow runs. `python` is either the interpreter running the tests
or a recorder that writes the arguments it got, one JSON list per call, and changes nothing.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
INPUT = re.compile(r"^  (?P<name>[\w-]+):\n(?P<body>(?:    .*\n)*)", re.MULTILINE)
DEFAULT = re.compile(r"^    default: (?P<value>.*)$", re.MULTILINE)
BOUND = re.compile(r"^        (?P<env>\w+): \$\{\{ inputs\.(?P<input>[\w-]+) \}\}$", re.MULTILINE)
GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "bash.exe"


def bash():
    """The bash that runs composite steps here, or None: on Windows only Git Bash qualifies."""
    if os.name != "nt":
        return shutil.which("bash")
    return str(GIT_BASH) if GIT_BASH.is_file() else None


SKIP_REASON = "no bash to run the action's step (on Windows, Git Bash is required)"


def posix(path):
    """The path as the bash that runs the step names it."""
    if os.name != "nt":
        return str(path)
    windows = PureWindowsPath(path)
    return "/" + windows.drive.rstrip(":").lower() + windows.as_posix()[len(windows.drive) :]


def action_step(name):
    """Return the last shell step's script, its {env: input} bindings and the action's input defaults."""
    text = (ROOT / "actions" / name / "action.yml").read_text(encoding="utf-8")
    inputs, runs = text.split("\ninputs:\n", 1)[1].split("\nruns:\n", 1)
    inputs = inputs.split("\noutputs:\n", 1)[0] + "\n"
    defaults = {}
    for entry in INPUT.finditer(inputs):
        found = DEFAULT.search(entry["body"])
        defaults[entry["name"]] = found["value"].strip("'\"") if found else ""
    step = runs.rsplit("\n    - ", 1)[1]
    bindings = {match["env"]: match["input"] for match in BOUND.finditer(step)}
    if "      run: |\n" in step:
        script = dedent(step.split("      run: |\n", 1)[1])
    else:
        script = step.split("      run: ", 1)[1].strip() + "\n"
    return script, bindings, defaults


class ActionRunner:
    """Runs action steps in a scratch directory; call close() when done."""

    def __init__(self, record=False):
        self.temporary = tempfile.TemporaryDirectory(prefix="jellysin-action-")
        self.work = Path(self.temporary.name)
        self.records = self.work / "python-calls.jsonl"
        shims = self.work / "bin"
        shims.mkdir()
        recorder = (
            "import json, os, sys; "
            f"open({self.records.as_posix()!r}, 'a', encoding='utf-8').write("
            "json.dumps({'argv': sys.argv[1:], 'GH_TOKEN': os.environ.get('GH_TOKEN')}) + '\\n')"
        )
        target = f'"{posix(sys.executable)}" -c "{recorder}"' if record else f'"{posix(sys.executable)}"'
        python = shims / "python"
        python.write_text(f'#!/bin/sh\nexec {target} "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        self.shims = posix(shims)

    def close(self):
        self.temporary.cleanup()

    def run(self, name, inputs, environment=None):
        """Run the action's step with the given inputs (others take their defaults)."""
        script, bindings, defaults = action_step(name)
        values = {**defaults, **inputs}
        env = {**os.environ, "TOOL_ROOT": str(ROOT), **(environment or {})}
        env.update({variable: values.get(source, "") for variable, source in bindings.items()})
        # The shell GitHub runs for `shell: bash` in a composite action, with setup-python's python first.
        command = [bash(), "--noprofile", "--norc", "-eo", "pipefail", "-c", f'PATH="{self.shims}:$PATH"\n{script}']
        return subprocess.run(command, cwd=self.work, env=env, capture_output=True, check=False, timeout=120)

    def calls(self):
        """The recorded python calls: each the argument list after `python` and the GH_TOKEN it saw."""
        if not self.records.exists():
            return []
        return [json.loads(line) for line in self.records.read_text(encoding="utf-8").splitlines()]
