"""Tests for Makefile targets with spaced, dollar, and boundary DEV_VENV values."""

import os
import shlex
import shutil
import subprocess  # nosec B404
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKE = shutil.which("make")


def clean_env(**extra):
    """Return the environment without inherited make or git overrides, plus extra."""
    # A parent make or git hook would otherwise pin DEV_VENV or point git at the
    # enclosing repository.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DEV_VENV", "MAKEFLAGS", "MFLAGS", "MAKELEVEL"} and not k.startswith("GIT_")
    }
    env.update(extra)
    return env


def create_python3_stub(stub_dir):
    """Write an executable python3 stub that logs invocations and provisions a python stub upon venv creation."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub_py = stub_dir / "python3"
    stub_py.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$STUB_LOG"\n'
        'if [ "$2" = "venv" ]; then\n'
        '  mkdir -p "$4/bin"\n'
        "  cat <<'EOF' > \"$4/bin/python\"\n"
        "#!/bin/sh\n"
        'echo "$*" >> "$STUB_LOG"\n'
        "exit 0\n"
        "EOF\n"
        '  chmod +x "$4/bin/python"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub_py.chmod(0o755)


def run_make(*args, cwd=REPO_ROOT, env=None, check=False):
    """Run the make binary found on PATH with a sanitized environment and return the process."""
    if MAKE is None or Path(MAKE).name != "make":
        raise RuntimeError(f"expected a make binary on PATH, found {MAKE!r}")
    # Fixed make binary from shutil.which, argument list, no shell, test-controlled arguments.
    return subprocess.run(  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        [MAKE, *args],
        cwd=cwd,
        env=clean_env() if env is None else env,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


@unittest.skipUnless(MAKE, "make is not installed")
class TestMakefile(unittest.TestCase):
    """Test Makefile targets with spaced, boundary, and invalid DEV_VENV values."""

    def test_bootstrap_spaced_dev_venv_succeeds(self):
        """Verify make bootstrap succeeds with a spaced DEV_VENV path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shutil.copy(REPO_ROOT / "Makefile", tmp / "Makefile")
            (tmp / "requirements-dev.txt").write_text("", encoding="utf-8")
            stub = tmp / "stub"
            log = tmp / "stub.log"
            create_python3_stub(stub)

            spaced_venv = "dir with space/.venv"
            res = run_make(
                "bootstrap",
                f"DEV_VENV={spaced_venv}",
                cwd=tmp,
                env=clean_env(PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)),
            )
            self.assertEqual(res.returncode, 0)
            target_venv = tmp / spaced_venv
            self.assertTrue((target_venv / "bin" / "python").exists())
            self.assertTrue((target_venv / "requirements-dev.txt").exists())
            log_content = log.read_text(encoding="utf-8")
            self.assertIn(f"-m venv --clear {spaced_venv}", log_content)
            self.assertIn("-m pip install", log_content)

    def test_boundary_dot_venv_accepted(self):
        """Verify the bootstrap guard accepts an exact .venv boundary path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shutil.copy(REPO_ROOT / "Makefile", tmp / "Makefile")
            (tmp / "requirements-dev.txt").write_text("", encoding="utf-8")
            stub = tmp / "stub"
            log = tmp / "stub.log"
            create_python3_stub(stub)

            res = run_make(
                "bootstrap",
                "DEV_VENV=.venv",
                cwd=tmp,
                env=clean_env(PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)),
            )
            self.assertEqual(res.returncode, 0)
            target_venv = tmp / ".venv"
            self.assertTrue((target_venv / "bin" / "python").exists())
            self.assertTrue((target_venv / "requirements-dev.txt").exists())
            log_content = log.read_text(encoding="utf-8")
            self.assertIn("-m venv --clear .venv", log_content)
            self.assertIn("-m pip install", log_content)

    def test_negative_dev_venv_build_notvenv_refused(self):
        """Verify DEV_VENV=build/notvenv is refused before python3 stub runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shutil.copy(REPO_ROOT / "Makefile", tmp / "Makefile")
            (tmp / "requirements-dev.txt").write_text("", encoding="utf-8")
            stub = tmp / "stub"
            log = tmp / "stub.log"
            create_python3_stub(stub)

            res = run_make(
                "bootstrap",
                "DEV_VENV=build/notvenv",
                cwd=tmp,
                env=clean_env(PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)),
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("DEV_VENV must name a .venv directory, not build/notvenv", res.stderr)
            self.assertFalse(log.exists())

    def test_negative_dev_venv_spaced_suffix_refused(self):
        """Verify DEV_VENV='/a b/.venv x' is refused before python3 stub runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shutil.copy(REPO_ROOT / "Makefile", tmp / "Makefile")
            (tmp / "requirements-dev.txt").write_text("", encoding="utf-8")
            stub = tmp / "stub"
            log = tmp / "stub.log"
            create_python3_stub(stub)

            res = run_make(
                "bootstrap",
                "DEV_VENV=/a b/.venv x",
                cwd=tmp,
                env=clean_env(PATH=f"{stub}:{os.environ['PATH']}", STUB_LOG=str(log)),
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("DEV_VENV must name a .venv directory, not /a b/.venv x", res.stderr)
            self.assertFalse(log.exists())

    def test_python_env_spaced_dev_venv_missing_python(self):
        """Verify python-env fails naming the full path when python is missing."""
        spaced_venv = "/a b/.venv"
        res = run_make("python-env", f"DEV_VENV={spaced_venv}")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"{spaced_venv}/bin/python is missing", res.stderr)

    def test_lint_policy_test_targets_use_quoted_dev_venv_python(self):
        """Verify lint, policy, and test recipes invoke quoted $DEV_VENV/bin/python."""
        for target in ("lint", "policy", "test"):
            with self.subTest(target=target):
                res = run_make(
                    "-n",
                    target,
                    "DEV_VENV=/a b/.venv",
                    check=True,
                )
                self.assertIn('"$DEV_VENV/bin/python"', res.stdout)

    def test_spaced_dev_venv_tokens_quoted(self):
        """Verify make -n properly quotes DEV_VENV paths containing whitespace."""
        with tempfile.TemporaryDirectory(prefix="spaced path ") as tmpdir:
            spaced_venv = str(Path(tmpdir) / ".venv")
            dry_run = run_make(
                "-n",
                "bootstrap",
                "python-env",
                "lint",
                "policy",
                "test",
                f"DEV_VENV={spaced_venv}",
                check=True,
            )
            found_var = False
            found_subpath = False
            for raw_line in dry_run.stdout.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.endswith("\\"):
                    line = line[:-1].rstrip()
                tokens = shlex.split(line)
                for tok in tokens:
                    if tok == "$DEV_VENV":
                        found_var = True
                    elif tok.startswith("$DEV_VENV/"):
                        found_subpath = True
            self.assertTrue(found_var, "Expected token '$DEV_VENV' not found in make -n output lines")
            self.assertTrue(
                found_subpath, "Expected token starting with '$DEV_VENV/' not found in make -n output lines"
            )

    def test_dollar_dev_venv_preserved(self):
        """Verify DEV_VENV paths containing literal dollar signs are preserved."""
        with tempfile.TemporaryDirectory(prefix="work$backup") as tmpdir:
            dollar_venv = str(Path(tmpdir) / ".venv")
            dollar_venv_make = dollar_venv.replace("$", "$$")

            cmd_res = run_make("python-env", f"DEV_VENV={dollar_venv_make}")
            self.assertNotEqual(cmd_res.returncode, 0)
            self.assertIn(f"{dollar_venv}/bin/python is missing", cmd_res.stderr)

            env_res = run_make(
                "python-env",
                env=clean_env(DEV_VENV=dollar_venv),
            )
            self.assertNotEqual(env_res.returncode, 0)
            self.assertIn(f"{dollar_venv}/bin/python is missing", env_res.stderr)


class TestRunMakeGuard(unittest.TestCase):
    """run_make runs only a binary named make."""

    def test_negative_non_make_binary_refused(self):
        """A PATH lookup that resolved to another binary is refused before any process starts."""
        with unittest.mock.patch(f"{__name__}.MAKE", "/bin/sh"), unittest.mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "expected a make binary"):
                run_make("--version")
        run.assert_not_called()

    def test_negative_missing_make_refused(self):
        """A missing make is refused with a message instead of a TypeError from subprocess."""
        with unittest.mock.patch(f"{__name__}.MAKE", None), unittest.mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "expected a make binary"):
                run_make("--version")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
