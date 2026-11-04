"""Execute real Makefile recipes with a recording interpreter under a spaced path."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_makefile as make_fixtures


@unittest.skipUnless(make_fixtures.MAKE, "make is not installed")
class MakefileSpacedVenvTests(unittest.TestCase):
    """Run policy and test recipes without invoking policy, coverage, or repository tests."""

    def setUp(self):
        """Copy the real Makefile and requirements and install a recording Python stub."""
        temporary = tempfile.TemporaryDirectory(prefix="make e2e ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copyfile(make_fixtures.REPO_ROOT / "Makefile", self.root / "Makefile")
        requirements = make_fixtures.REPO_ROOT / "requirements-dev.txt"
        shutil.copyfile(requirements, self.root / "requirements-dev.txt")
        self.venv = self.root / "dev environment" / ".venv"
        self.python = self.venv / "bin" / "python"
        self.python.parent.mkdir(parents=True)
        self.write_python_stub()
        shutil.copyfile(requirements, self.venv / "requirements-dev.txt")
        self.log = self.root / "python-arguments.jsonl"

    def write_python_stub(self):
        """Install the dependency-free argument recorder at the fixture interpreter path."""
        self.python.write_text(
            "#!/usr/bin/env python3\n"
            '"""Record the executable path and each argument without running repository tools."""\n'
            "import json\n"
            "import os\n"
            "import sys\n"
            'with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as stream:\n'
            '    stream.write(json.dumps(sys.argv) + "\\n")\n',
            encoding="utf-8",
        )
        self.python.chmod(0o755)

    def run_recipes(self):
        """Reuse the bounded make runner while preserving inherited git configuration."""
        return make_fixtures.run_make(
            "policy",
            "test",
            f"DEV_VENV={self.venv}",
            cwd=self.root,
            env=make_fixtures.clean_env(STUB_LOG=str(self.log)),
        )

    def test_policy_and_test_recipes_use_spaced_venv(self):
        """Reach the stub with the full spaced interpreter path and exact recipe arguments."""
        result = self.run_recipes()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [
                [str(self.python), "tools.py", "check-policy"],
                [str(self.python), "-m", "coverage", "run", "-m", "unittest", "discover", "-s", "tests"],
                [str(self.python), "-m", "coverage", "report"],
            ],
            calls,
        )

    def test_stale_requirements_refused_before_stub(self):
        """Reject a planted stale environment before either recipe reaches the interpreter."""
        (self.venv / "requirements-dev.txt").write_text("planted stale requirements\n", encoding="utf-8")
        result = self.run_recipes()
        self.assertNotEqual(0, result.returncode)
        self.assertIn(f"{self.venv} does not match requirements-dev.txt", result.stderr)
        self.assertFalse(self.log.exists())

    def test_recording_stub_with_spaced_host_interpreter(self):
        """Reach the recorder even when the test interpreter itself has a spaced path."""
        interpreter = self.root / "host environment" / "python"
        interpreter.parent.mkdir()
        interpreter.symlink_to(sys.executable)
        with patch("sys.executable", str(interpreter)):
            self.write_python_stub()
        result = self.run_recipes()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([str(self.python)] * 3, [call[0] for call in calls])
