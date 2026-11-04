"""Exercise tools.py publish with retained bundle bytes and a real cosign stand-in."""

import contextlib
import hashlib
import io
import json
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling import cli, github
from tests.fixtures import COMMIT, FIRST, FakeGitHub, Workspace, make_bundle, write_bundles


class PublishBundleRetryTests(Workspace):
    """Keep verified uploaded signatures on retry and refuse failed verification before writes."""

    def setUp(self):
        """Reuse plugin artifacts and fake GitHub with one earlier, differing signature bundle."""
        super().setUp()
        self.release, self.output = self.plugin()
        write_bundles(self.output)
        self.client = FakeGitHub()
        self.remote = self.client.add(FIRST, self.release, self.output, draft=True)
        self.bundle = next(asset for asset in self.remote["assets"] if asset["name"] == "checksums.txt.sigstore.json")
        self.uploaded = make_bundle((self.output / "checksums.txt").read_bytes(), padding="earlier signing run")
        self.bundle["size"] = len(self.uploaded)
        self.client.assets[FIRST["repository"], self.bundle["id"]] = self.uploaded
        self.writes = []
        self.attestations = []
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.log = self.root / "cosign.jsonl"
        self.reject = self.root / "reject-cosign"
        self.identity = (
            f"https://github.com/{FIRST['repository']}/.github/workflows/build-signed.yml"
            f"@refs/tags/{self.release['tag']}"
        )
        self.write_cosign_stub()

    def write_cosign_stub(self):
        """Install an owner-only verifier checking exact arguments, bundle bytes and artifact bytes."""
        expected = {}
        for asset in self.remote["assets"]:
            if asset["name"].endswith(".sigstore.json"):
                artifact = (self.output / asset["name"].removesuffix(".sigstore.json")).read_bytes()
                bundle = self.client.assets[FIRST["repository"], asset["id"]]
                expected[hashlib.sha256(artifact).hexdigest()] = hashlib.sha256(bundle).hexdigest()
        stub = self.bin / "cosign"
        stub.write_text(
            '#!/usr/bin/env python3\n"""Check only synthetic fixture verification requests."""\n'
            "import hashlib\nimport json\nimport sys\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "assert len(args) == 8, args\n"
            "assert args[:2] == ['verify-blob', '--bundle'], args\n"
            f"assert args[3:7] == ['--certificate-identity', {self.identity!r}, "
            "'--certificate-oidc-issuer', 'https://token.actions.githubusercontent.com'], args\n"
            "artifact = hashlib.sha256(Path(args[7]).read_bytes()).hexdigest()\n"
            "bundle = hashlib.sha256(Path(args[2]).read_bytes()).hexdigest()\n"
            f"assert {expected!r}.get(artifact) == bundle, 'wrong uploaded bundle or artifact bytes'\n"
            f"with Path({str(self.log)!r}).open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({'args': args, 'bundle': bundle}) + '\\n')\n"
            f"sys.exit(1 if Path({str(self.reject)!r}).exists() else 0)\n",
            encoding="utf-8",
        )
        # Only this test's owner may execute the fixed synthetic verifier; no shared writable executable.
        stub.chmod(0o700)  # nosec B103  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions

    def verify_attestation_call(self, arguments):
        """Validate intercepted gh attestation verification arguments and record verified target."""
        self.assertEqual(
            [
                "gh",
                "attestation",
                "verify",
                arguments[3],
                "--repo",
                FIRST["repository"],
                "--signer-workflow",
                f"{FIRST['repository']}/.github/workflows/build-signed.yml",
                "--source-digest",
                COMMIT,
                "--source-ref",
                "refs/tags/" + self.release["tag"],
                "--deny-self-hosted-runners",
                "--limit",
                "10",
            ],
            arguments,
        )
        self.attestations.append(Path(arguments[3]).name)
        return b"verified synthetic attestation"

    def verify_publication_call(self, arguments):
        """Validate intercepted gh release PATCH arguments, record write, and update fake release."""
        self.assertEqual(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{FIRST['repository']}/releases/{self.remote['id']}",
                "--field",
                "draft=false",
                "--raw-field",
                "make_latest=true",
                "--silent",
            ],
            arguments,
        )
        self.writes.append(arguments)
        self.client.publish(self.remote)
        return b""

    def publish(self):
        """Drive tools.py publish; intercept all gh calls and execute only the cosign stub."""
        tool = Path(cli.__file__).resolve().parents[1] / "tools.py"
        arguments = [
            str(tool),
            "publish",
            "--directory",
            str(self.output),
            "--repo",
            FIRST["repository"],
            "--tag",
            self.release["tag"],
            "--signer-workflow",
            ".github/workflows/build-signed.yml",
        ]
        original = github.command_bytes

        def command_bytes(arguments, cwd=None, timeout=120, limit=github.MAX_ASSET):
            """Validate fake attestation/publication calls and retain the bounded real cosign process."""
            if arguments[0] == "cosign":
                self.assertEqual(120, timeout)
                return original(arguments, cwd=cwd, timeout=timeout, limit=limit)
            self.assertEqual("gh", arguments[0])
            if arguments[1:3] == ["attestation", "verify"]:
                return self.verify_attestation_call(arguments)
            return self.verify_publication_call(arguments)

        stderr = io.StringIO()
        with (
            patch("sys.argv", arguments),
            patch.dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', os.defpath)}"),
            patch("jellysin_tooling.cli.GitHub", return_value=self.client),
            patch("jellysin_tooling.release.exact_tag", return_value=COMMIT),
            patch("jellysin_tooling.github.command_bytes", side_effect=command_bytes),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                runpy.run_path(str(tool), run_name="__main__")
            except SystemExit as exc:
                return exc.code, stderr.getvalue()
        return 0, stderr.getvalue()

    def test_verified_uploaded_bundle_is_kept_and_publication_completes(self):
        """Accept earlier signing bytes for the approved signer at the exact release tag."""
        fresh = (self.output / self.bundle["name"]).read_bytes()
        self.assertNotEqual(fresh, self.uploaded)
        status, stderr = self.publish()
        self.assertEqual(0, status, stderr)
        self.assertFalse(self.remote["draft"])
        self.assertTrue(self.remote["immutable"])
        self.assertEqual(1, len(self.writes), "only the final publication PATCH may write")
        self.assertEqual(self.uploaded, self.client.assets[FIRST["repository"], self.bundle["id"]])
        self.assertEqual(fresh, (self.output / self.bundle["name"]).read_bytes())
        self.assertEqual(4, len(self.attestations))
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(8, len(calls), "all four retained bundles must verify in both release reads")
        self.assertEqual(2, sum(call["bundle"] == hashlib.sha256(self.uploaded).hexdigest() for call in calls))

    def test_refused_uploaded_bundle_leaves_draft_and_assets_untouched(self):
        """Plant a cosign refusal despite structurally valid bundles and matching artifact bytes."""
        self.reject.touch()
        original_assets = dict(self.client.assets)
        status, stderr = self.publish()
        self.assertEqual(1, status, stderr)
        self.assertIn(
            "cosign verification unavailable or failed for uploaded bundle: checksums.txt.sigstore.json", stderr
        )
        self.assertEqual(1, len(self.log.read_text(encoding="utf-8").splitlines()))
        self.assertTrue(self.remote["draft"])
        self.assertFalse(self.remote["immutable"])
        self.assertEqual([], self.writes)
        self.assertEqual(original_assets, self.client.assets)
