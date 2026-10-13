"""The server check talks real HTTP to a stand-in Jellyfin and a stand-in container runtime; no test starts a container."""

import contextlib
import io
import json
import re
import secrets
import signal
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from jellysin_tooling import catalog, servercheck
from jellysin_tooling.cli import parser, run
from jellysin_tooling.common import ValidationError, json_bytes, read_json
from tests.fixtures import Workspace

ROOT = Path(__file__).resolve().parents[1]
DIGEST = servercheck.IMAGE.fullmatch(servercheck.SERVER_IMAGE).group(2)
OTHER_IMAGE = "jellyfin/jellyfin:12.0@sha256:" + "b" * 64
FIRST_GUID = "11111111-1111-4111-8111-111111111111"
SECOND_GUID = "22222222-2222-4222-8222-222222222222"
STEPS = ["pull", "run", "inspect", "image", "port", "ps", "stop", "rm", "ps"]


class Jellyfin:
    """The endpoints the check uses, with the rule of the real server: a version above the server is not offered."""

    def __init__(self):
        self.version = "12.2.0"
        self.files = {}
        self.filtering = True
        self.starting = 0
        self.failing = None
        self.packages = None
        self.session = None
        self.password = None
        self.token = None
        self.repositories = []
        self.requests = []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, self))
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    def offered(self):
        limit = (*(int(part) for part in self.version.split(".")), 0)
        result = {}
        for repository in self.repositories:
            try:
                plugins = json.loads(self.files.get(repository["Url"].rsplit("/", 1)[1]))
            except (TypeError, ValueError):
                continue  # The real server logs the unreadable file and offers nothing from it.
            for plugin in plugins:
                versions = [
                    {**version, "repositoryName": repository["Name"]}
                    for version in plugin["versions"]
                    if not self.filtering or tuple(int(part) for part in version["targetAbi"].split(".")) <= limit
                ]
                if versions:
                    listed = {**plugin, "guid": plugin["guid"].replace("-", ""), "versions": []}
                    result.setdefault(plugin["guid"], listed)["versions"] += versions
        return list(result.values())

    def info(self, _body):
        if self.starting:
            self.starting -= 1
            return 503, None
        return 200, {"Version": self.version}

    def accept(self, _body):
        return 204, None

    def read(self, _body):
        return 200, {}

    def user(self, body):
        self.password = body["Password"]
        return 204, None

    def sign_in(self, body):
        if body["Pw"] != self.password:
            return 401, None
        self.token = secrets.token_hex(16)
        return 200, self.session if self.session is not None else {"AccessToken": self.token}

    def set_repositories(self, body):
        self.repositories = body
        return 204, None

    def list_packages(self, _body):
        return 200, self.packages if self.packages is not None else self.offered()


OPEN = {
    ("GET", "/System/Info/Public"): Jellyfin.info,
    ("GET", "/Startup/Configuration"): Jellyfin.read,
    ("POST", "/Startup/Configuration"): Jellyfin.accept,
    ("GET", "/Startup/User"): Jellyfin.read,
    ("POST", "/Startup/User"): Jellyfin.user,
    ("POST", "/Startup/RemoteAccess"): Jellyfin.accept,
    ("POST", "/Startup/Complete"): Jellyfin.accept,
    ("POST", "/Users/AuthenticateByName"): Jellyfin.sign_in,
}
SIGNED_IN = {("POST", "/Repositories"): Jellyfin.set_repositories, ("GET", "/Packages"): Jellyfin.list_packages}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, state, *arguments):
        self.state = state
        super().__init__(*arguments)

    def log_message(self, *_arguments):
        pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        state = self.state
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        authorization = self.headers.get("Authorization", "")
        state.requests.append((method, self.path, authorization))
        key = (method, self.path)
        if key == state.failing:
            status, answer = 500, {"detail": "secret-server-detail"}
        elif key in SIGNED_IN and f'Token="{state.token}"' not in authorization:
            status, answer = 401, None
        else:
            status, answer = (OPEN | SIGNED_IN)[key](state, body)
        data = b"" if answer is None else json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Docker:
    """Container runtime stand-in: it hands the mounted files to the stand-in server and records every call."""

    def __init__(self, jellyfin):
        self.jellyfin = jellyfin
        self.calls = []
        self.containers = set()
        self.fail = set()
        self.interrupt = None
        self.half_created = False
        self.stuck = False
        self.digests = None
        self.published = None
        self.sources = []
        self.handler = None

    def __call__(self, arguments, cwd=None, timeout=120):
        assert arguments[0] == "docker" and cwd is None and 0 < timeout <= 600, arguments
        self.calls.append(arguments[1:])
        step = arguments[1]
        if step == self.interrupt:
            raise SystemExit(143)
        if step in self.fail:
            raise ValidationError("docker command failed (1)")
        return getattr(self, step)(arguments[2:])

    def steps(self):
        return [call[0] for call in self.calls]

    def pull(self, arguments):
        return arguments[-1]

    def run(self, arguments):
        self.handler = signal.getsignal(signal.SIGTERM)
        self.containers.add(arguments[arguments.index("--name") + 1])
        if self.half_created:
            raise ValidationError("docker command failed (125)")
        self.started = arguments
        for index, argument in enumerate(arguments):
            if argument == "--mount":
                fields = dict(part.split("=", 1) for part in arguments[index + 1].split(",") if "=" in part)
                self.sources.append(Path(fields["src"]))
                self.jellyfin.files[fields["dst"].rsplit("/", 1)[1]] = self.sources[-1].read_bytes()
        return "c" * 64

    def inspect(self, _arguments):
        return "sha256:" + "e" * 64

    def image(self, _arguments):
        repository, digest = self.started[-1].split("@")
        return json.dumps([repository.split(":")[0] + "@" + digest] if self.digests is None else self.digests)

    def port(self, _arguments):
        return f"127.0.0.1:{self.jellyfin.port}" if self.published is None else self.published

    def ps(self, arguments):
        name = arguments[arguments.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
        return name if name in self.containers else ""

    def stop(self, arguments):
        return arguments[-1]

    def rm(self, arguments):
        if not self.stuck:
            self.containers.discard(arguments[-1])
        return arguments[-1]


class ServerCheckCase(Workspace):
    def setUp(self):
        super().setUp()
        self.jellyfin = Jellyfin()
        self.addCleanup(self.jellyfin.close)
        self.docker = Docker(self.jellyfin)
        self.pauses = []

    def manifest(self, *entries):
        releases = [
            servercheck.synthetic(guid, "Plugin " + guid[:4], version, abi, "jellysin/plugin-" + guid[:4])
            for guid, version, abi in entries
        ]
        path = self.root / "manifest.json"
        path.write_bytes(json_bytes(catalog.merge_manifest([], releases)))
        return path

    def outcome(self, path, image=None):
        """Return the refusal, or None, and the lines the check printed."""
        output = io.StringIO()
        error = None
        with contextlib.redirect_stdout(output):
            try:
                servercheck.check(path, image, runner=self.docker, pause=self.pauses.append)
            except ValidationError as exc:
                error = str(exc)
        return error, output.getvalue()


class OfferedEntryTests(ServerCheckCase):
    def test_passes_when_the_server_offers_every_entry_and_names_what_ran(self):
        path = self.manifest(
            (FIRST_GUID, "1.0.0", "12.0.0.0"), (SECOND_GUID, "1.0.0", "12.2.0.0"), (SECOND_GUID, "0.9.0", "12.0.0.0")
        )
        before = path.read_bytes()
        error, output = self.outcome(path)
        self.assertIsNone(error)
        self.assertEqual(
            output.splitlines(),
            [
                f"server-check: image jellyfin/jellyfin@{DIGEST}",
                "server-check: server version 12.2.0",
                "server-check: controls passed (compatible entry offered, entries above the server withheld)",
                "server-check: 3 expected entries, all offered by server 12.2.0",
            ],
        )
        self.assertEqual(self.docker.steps(), STEPS)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(before, self.jellyfin.files[servercheck.FILES[servercheck.MANIFEST]])

    def test_refuses_an_entry_above_the_server_and_names_identity_version_and_abi(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"), (SECOND_GUID, "1.0.0", "12.2.0.0"))
        self.jellyfin.version = "12.0.0"
        error, output = self.outcome(path, OTHER_IMAGE)
        self.assertEqual(error, "Server 12.0.0 withholds 1 of 2 manifest entries")
        self.assertIn(f"server 12.0.0 does not offer {SECOND_GUID} 1.0.0.0 (targetAbi 12.2.0.0)", output)
        self.assertNotIn(FIRST_GUID, output)
        self.assertNotIn("all offered", output)
        self.assertIn("image jellyfin/jellyfin@sha256:" + "b" * 64, output)
        self.assertEqual(self.docker.containers, set())

    def test_same_file_passes_on_the_server_that_supports_it(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"), (SECOND_GUID, "1.0.0", "12.2.0.0"))
        self.assertIsNone(self.outcome(path)[0])

    def test_withheld_version_of_an_offered_plugin_is_refused(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"), (FIRST_GUID, "2.0.0", "12.3.0.0"))
        error, output = self.outcome(path)
        self.assertEqual(error, "Server 12.2.0 withholds 1 of 2 manifest entries")
        self.assertIn(f"does not offer {FIRST_GUID} 2.0.0.0 (targetAbi 12.3.0.0)", output)

    def test_empty_file_passes_and_says_that_nothing_was_expected(self):
        path = self.root / "manifest.json"
        path.write_text("[]\n")
        error, output = self.outcome(path)
        self.assertIsNone(error)
        self.assertIn("server-check: 0 expected entries, all offered by server 12.2.0", output)
        self.assertIn("controls passed", output)

    def test_reports_at_most_the_bounded_number_of_withheld_entries(self):
        entries = [(FIRST_GUID, f"1.0.{patch_number}", "12.9.0.0") for patch_number in range(25)]
        error, output = self.outcome(self.manifest(*entries))
        self.assertEqual(error, "Server 12.2.0 withholds 25 of 25 manifest entries")
        self.assertEqual(output.count("does not offer"), servercheck.MAX_REPORTED)

    def test_entry_offered_only_under_another_repository_name_does_not_count(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))
        control = servercheck.controls()
        foreign = read_json(path)[0]
        self.jellyfin.packages = [
            {**control[0], "versions": [{**control[0]["versions"][1], "repositoryName": servercheck.CONTROLS}]},
            {**foreign, "versions": [{**foreign["versions"][0], "repositoryName": "elsewhere"}]},
        ]
        self.assertEqual(self.outcome(path)[0], "Server 12.2.0 withholds 1 of 1 manifest entries")


class ControlTests(ServerCheckCase):
    def test_controls_come_from_the_entry_building_code_of_the_catalog(self):
        with patch.object(catalog, "manifest_entry", wraps=catalog.manifest_entry) as build:
            control = servercheck.controls()
        self.assertEqual(build.call_count, 3)
        self.assertEqual(control, catalog.validate_manifest(control))
        entries = {
            (plugin["guid"], version["version"], version["targetAbi"])
            for plugin in control
            for version in plugin["versions"]
        }
        self.assertEqual(
            entries,
            {
                (servercheck.CONTROL_MIXED, "1.0.0.0", "12.0.0.0"),
                (servercheck.CONTROL_MIXED, "2.0.0.0", "12.65534.0.0"),
                (servercheck.CONTROL_WITHHELD, "1.0.0.0", "12.65534.0.0"),
            },
        )

    def test_server_that_does_not_filter_fails_the_control_even_for_a_compatible_file(self):
        self.jellyfin.filtering = False
        error, output = self.outcome(self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0")))
        self.assertIn("does not filter", error)
        self.assertNotIn("controls passed", output)
        self.assertEqual(self.docker.containers, set())

    def test_one_unfiltered_control_entry_is_enough_to_fail(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))
        control = servercheck.controls()
        for plugin, version in ((control[0], control[0]["versions"][0]), (control[1], control[1]["versions"][0])):
            self.jellyfin.packages = [
                {**control[0], "versions": [{**control[0]["versions"][1], "repositoryName": servercheck.CONTROLS}]},
                {**plugin, "versions": [{**version, "repositoryName": servercheck.CONTROLS}]},
            ]
            with self.subTest(guid=plugin["guid"]):
                self.assertIn("does not filter", self.outcome(path)[0])

    def test_server_that_lists_nothing_fails_the_control_even_for_an_empty_file(self):
        path = self.root / "manifest.json"
        path.write_text("[]\n")
        self.jellyfin.packages = []
        with patch.object(self.jellyfin, "offered", return_value=[]):
            error, _ = self.outcome(path)
        self.assertIn("proves nothing", error)

    def test_manifest_with_a_control_identity_is_refused_before_any_container_step(self):
        for guid in sorted(servercheck.RESERVED):
            with self.subTest(guid=guid):
                error, _ = self.outcome(self.manifest((guid, "1.0.0", "12.0.0.0")))
                self.assertEqual(error, "Manifest uses an identity of the built-in controls")
        self.assertEqual(self.docker.calls, [])


class InputTests(ServerCheckCase):
    def test_invalid_manifest_is_refused_before_any_container_step(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))
        valid = read_json(path)
        valid[0]["versions"][0]["targetAbi"] = "13.0.0.0"
        path.write_bytes(json_bytes(valid))
        self.assertIn("ABI", self.outcome(path)[0])
        path.write_text("{not json")
        self.assertEqual(self.outcome(path)[0], "Invalid JSON")
        path.write_bytes(b"[" + b" " * servercheck.MAX_JSON + b"]")
        self.assertEqual(self.outcome(path)[0], "JSON exceeds size limit")
        self.assertEqual(self.docker.calls, [])

    def test_only_a_digest_pinned_image_of_the_reviewed_repository_runs(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))
        digest = "@sha256:" + "b" * 64
        refused = (
            "jellyfin/jellyfin:12.2",
            "jellyfin/jellyfin",
            "evil/jellyfin:12.2" + digest,
            "ghcr.io/jellyfin/jellyfin" + digest,
            "jellyfin/jellyfin:12.2@sha256:" + "B" * 64,
            "jellyfin/jellyfin:12.2" + digest + " --privileged",
            "--privileged",
            "",
            7,
        )
        for image in refused:
            with self.subTest(image=image):
                self.assertEqual(
                    self.outcome(path, image)[0], "Server image must be jellyfin/jellyfin with a sha256 digest"
                )
        self.assertEqual(self.docker.calls, [])
        self.assertIsNone(self.outcome(path, "jellyfin/jellyfin" + digest)[0])
        self.assertEqual(self.docker.started[-1], "jellyfin/jellyfin" + digest)

    def test_default_image_is_the_reviewed_pin(self):
        self.assertIsNone(self.outcome(self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0")))[0])
        self.assertEqual(self.docker.calls[0], ["pull", "--quiet", servercheck.SERVER_IMAGE])
        self.assertEqual(self.docker.started[-1], servercheck.SERVER_IMAGE)
        self.assertRegex(servercheck.SERVER_IMAGE, r"^jellyfin/jellyfin:12\.[0-9]+@sha256:[0-9a-f]{64}$")


class ContainerTests(ServerCheckCase):
    def setUp(self):
        super().setUp()
        self.path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))

    def test_container_is_confined_and_reads_copies_only(self):
        self.assertIsNone(self.outcome(self.path)[0])
        started = self.docker.started
        for option, value in (
            ("--user", "1000:1000"),
            ("--memory", "1536m"),
            ("--cpus", "2"),
            ("--stop-timeout", "20"),
            ("--publish", "127.0.0.1::8096"),
        ):
            self.assertEqual(started[started.index(option) + 1], value)
        self.assertIn("--detach", started)
        self.assertFalse({"--privileged", "--network", "--volume", "-v", "--env", "-e"} & set(started))
        mounts = [started[index + 1] for index, argument in enumerate(started) if argument == "--mount"]
        self.assertEqual(len(mounts), 2)
        for mount in mounts:
            self.assertRegex(
                mount, r"^type=bind,src=[^,]+,dst=/jellyfin/jellyfin-web/jellysin-server-check-[a-z]+\.json,readonly$"
            )
        self.assertNotIn(self.path, self.docker.sources)
        self.assertFalse(any(source.exists() or source.parent.exists() for source in self.docker.sources))
        self.assertRegex(started[started.index("--name") + 1], r"^jellysin-server-check-[0-9a-f]{12}$")

    def test_server_reads_both_files_from_itself_and_only_those(self):
        self.assertIsNone(self.outcome(self.path)[0])
        self.assertEqual(
            self.jellyfin.repositories,
            [
                {
                    "Name": "manifest",
                    "Url": "http://127.0.0.1:8096/web/jellysin-server-check-manifest.json",
                    "Enabled": True,
                },
                {
                    "Name": "controls",
                    "Url": "http://127.0.0.1:8096/web/jellysin-server-check-controls.json",
                    "Enabled": True,
                },
            ],
        )
        self.assertEqual(
            json_bytes(servercheck.controls()), self.jellyfin.files[servercheck.FILES[servercheck.CONTROLS]]
        )

    def test_failed_pull_is_an_error_and_starts_nothing(self):
        self.docker.fail = {"pull"}
        self.assertEqual(self.outcome(self.path)[0], "Server container step failed: pull")
        self.assertEqual(self.docker.steps(), ["pull"])

    def test_failed_start_removes_what_was_created(self):
        self.docker.fail = {"run"}
        self.assertEqual(self.outcome(self.path)[0], "Server container step failed: run")
        self.assertEqual(self.docker.steps(), ["pull", "run", "ps"])
        self.docker.fail = set()
        self.docker.calls.clear()
        self.docker.half_created = True
        self.assertEqual(self.outcome(self.path)[0], "Server container step failed: run")
        self.assertEqual(self.docker.steps(), ["pull", "run", "ps", "stop", "rm", "ps"])
        self.assertEqual(self.docker.containers, set())

    def test_every_failure_after_the_start_removes_the_container(self):
        for step in ("inspect", "image", "port"):
            self.docker.fail = {step}
            with self.subTest(step=step):
                self.assertIn("Server container step failed: " + step, self.outcome(self.path)[0])
                self.assertEqual(self.docker.containers, set())
        self.docker.fail = set()
        for key in list(OPEN | SIGNED_IN):
            self.jellyfin.failing = key
            self.jellyfin.token = None
            with patch.object(servercheck, "READY_SECONDS", 0.2), self.subTest(key=key):
                error, output = self.outcome(self.path)
                self.assertIsNotNone(error)
                self.assertNotIn("secret-server-detail", error + output)
                self.assertEqual(self.docker.containers, set())
                self.assertEqual(self.docker.steps()[-3:], ["stop", "rm", "ps"])

    def test_container_that_runs_another_image_is_refused(self):
        for digests in (["jellyfin/jellyfin@sha256:" + "f" * 64], ["other/image@" + DIGEST], [], {"x": 1}):
            self.docker.digests = digests
            with self.subTest(digests=digests):
                error, output = self.outcome(self.path)
                self.assertEqual(error, "Container runs another image")
                self.assertEqual(output, "")
                self.assertEqual(self.docker.containers, set())

    def test_port_must_be_published_on_loopback_only(self):
        for published in ("0.0.0.0:8096", "127.0.0.1:8096\n[::1]:8096", "127.0.0.1:port", ""):
            with self.subTest(published=published):
                self.docker.published = published
                self.assertEqual(self.outcome(self.path)[0], "Server port is not published on loopback only")
                self.assertEqual(self.docker.containers, set())

    def test_container_that_cannot_be_removed_is_an_error(self):
        self.docker.stuck = True
        self.assertEqual(self.outcome(self.path)[0], "Server container could not be removed")

    def test_failed_stop_still_removes_and_reports(self):
        self.docker.fail = {"stop"}
        self.assertEqual(self.outcome(self.path)[0], "Server container step failed: stop")
        self.assertEqual(self.docker.steps()[-3:], ["stop", "rm", "ps"])
        self.assertEqual(self.docker.containers, set())

    def test_termination_removes_the_container_and_restores_the_handler(self):
        # A handler of the test's own, so that a handler left behind by any earlier run cannot pass as restored.
        def before(_number, _frame):
            raise AssertionError("unexpected termination signal")

        self.addCleanup(signal.signal, signal.SIGTERM, signal.signal(signal.SIGTERM, before))
        self.docker.interrupt = "inspect"
        with self.assertRaises(SystemExit) as caught:
            self.outcome(self.path)
        self.assertEqual(caught.exception.code, 143)
        self.assertEqual(self.docker.containers, set())
        self.assertIs(self.docker.handler, servercheck.interrupted)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.docker.interrupt = None
        self.assertIsNone(self.outcome(self.path)[0])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        with self.assertRaises(SystemExit) as caught:
            servercheck.interrupted(signal.SIGTERM, None)
        self.assertEqual(caught.exception.code, 143)


class ServerTests(ServerCheckCase):
    def setUp(self):
        super().setUp()
        self.path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))

    def test_waits_for_a_starting_server_within_the_bound(self):
        self.jellyfin.starting = 3
        self.assertIsNone(self.outcome(self.path)[0])
        self.assertEqual(self.pauses, [0.5, 0.5, 0.5])

    def test_server_that_never_becomes_ready_is_an_error_after_the_bound(self):
        self.jellyfin.starting = 1000
        ticks = iter(range(0, 10_000, 50))
        with self.assertRaisesRegex(ValidationError, "not ready within 120 seconds"):
            servercheck.wait_ready(servercheck.Server(self.jellyfin.port), self.pauses.append, lambda: next(ticks))
        self.assertEqual(self.pauses, [0.5, 0.5])

    def test_unusable_version_is_refused(self):
        for version in ("12.2", "12.2.0-rc1", "<b>12.2.0</b>", None, 12):
            self.jellyfin.version = version
            with patch.object(Jellyfin, "info", lambda state, _body: (200, {"Version": state.version})):
                with self.subTest(version=version):
                    self.assertEqual(self.outcome(self.path)[0], "Server reports no usable version")
        with patch.object(Jellyfin, "info", lambda _state, _body: (200, ["12.2.0"])):
            self.assertEqual(self.outcome(self.path)[0], "Server reports no usable version")

    def test_unreachable_server_is_a_bounded_error_without_address_details(self):
        closed = Jellyfin()
        closed.close()
        with self.assertRaises(ValidationError) as caught:
            servercheck.Server(closed.port).call("GET", "/System/Info/Public")
        self.assertEqual(str(caught.exception), "Server request failed or timed out")

    def test_oversized_answer_is_refused(self):
        with patch.object(servercheck, "MAX_JSON", 8), self.assertRaisesRegex(ValidationError, "size limit"):
            servercheck.Server(self.jellyfin.port).call("GET", "/System/Info/Public")

    def test_sign_in_uses_a_fresh_password_and_sends_the_token_only_afterwards(self):
        self.assertIsNone(self.outcome(self.path)[0])
        first = self.jellyfin.password
        tokens = [('Token="' in authorization) for _, _, authorization in self.jellyfin.requests]
        self.assertEqual(tokens, [False] * 8 + [True] * 2)
        self.assertEqual([path for _, path, _ in self.jellyfin.requests][-2:], ["/Repositories", "/Packages"])
        self.assertGreaterEqual(len(first), 32)
        self.assertIsNone(self.outcome(self.path)[0])
        self.assertNotEqual(first, self.jellyfin.password)

    def test_sign_in_without_a_usable_token_is_refused(self):
        for session in ({}, {"AccessToken": 5}, {"AccessToken": 'x", Admin="1'}, []):
            self.jellyfin.session = session
            with self.subTest(session=session):
                self.assertEqual(self.outcome(self.path)[0], "Server sign-in returned no token")

    def test_malformed_package_lists_are_refused(self):
        version = {"version": "1.0.0.0", "repositoryName": "manifest"}
        cases = (
            ({"not": "a list"}, "invalid package list"),
            ([{}] * 65, "invalid package list"),
            (["text"], "invalid package"),
            ([{"guid": FIRST_GUID}], "invalid package"),
            ([{"guid": FIRST_GUID, "versions": [version] * 2001}], "invalid package"),
            ([{"guid": "not-a-guid", "versions": [version]}], "invalid package identity"),
            ([{"guid": 5, "versions": [version]}], "invalid package identity"),
            ([{"guid": FIRST_GUID, "versions": ["text"]}], "invalid package version"),
        )
        for packages, message in cases:
            self.jellyfin.packages = packages
            with self.subTest(message=message, packages=str(packages)[:40]):
                self.assertIn(message, self.outcome(self.path)[0])
                self.assertEqual(self.docker.containers, set())


class FixtureTests(ServerCheckCase):
    def test_wrong_abi_fixture_is_valid_and_refused_by_the_check(self):
        path = self.root / "wrong.json"
        servercheck.emit_fixture("wrong-abi", path)
        self.assertEqual(read_json(path), catalog.validate_manifest(read_json(path)))
        error, output = self.outcome(path)
        self.assertEqual(error, "Server 12.2.0 withholds 1 of 1 manifest entries")
        self.assertIn(f"does not offer {servercheck.FIXTURE} 1.0.0.0 (targetAbi 12.65534.0.0)", output)

    def test_floor_fixture_is_offered(self):
        path = self.root / "floor.json"
        servercheck.emit_fixture("floor", path)
        self.jellyfin.version = "12.0.0"
        error, output = self.outcome(path)
        self.assertIsNone(error)
        self.assertIn("1 expected entries, all offered by server 12.0.0", output)

    def test_fixture_never_replaces_a_file_and_knows_its_kinds(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))
        before = path.read_bytes()
        with self.assertRaisesRegex(ValidationError, "creates a new file only"):
            servercheck.emit_fixture("wrong-abi", path)
        self.assertEqual(before, path.read_bytes())
        with self.assertRaisesRegex(ValidationError, "available: floor, wrong-abi"):
            servercheck.emit_fixture("skip-attestation", self.root / "other.json")
        self.assertFalse((self.root / "other.json").exists())

    def test_command_line_runs_the_check_or_writes_a_fixture_never_both(self):
        with patch.object(servercheck, "check") as check:
            run(parser().parse_args(["server-check", "--manifest", "manifest.json"]))
            run(parser().parse_args(["server-check", "--manifest", "manifest.json", "--image", OTHER_IMAGE]))
        self.assertEqual(
            [call.args for call in check.call_args_list], [("manifest.json", None), ("manifest.json", OTHER_IMAGE)]
        )
        target = self.root / "fixture.json"
        with patch.object(servercheck, "check") as check:
            run(parser().parse_args(["server-check", "--emit-fixture", "wrong-abi", str(target)]))
        check.assert_not_called()
        self.assertTrue(target.is_file())
        with self.assertRaisesRegex(ValidationError, "without a server image"):
            run(parser().parse_args(["server-check", "--emit-fixture", "floor", "new.json", "--image", OTHER_IMAGE]))
        for arguments in (
            ["--manifest", "a.json", "--emit-fixture", "floor", "b.json"],
            [],
            ["--emit-fixture", "floor"],
        ):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser().parse_args(["server-check", *arguments])

    def test_catalog_commands_gain_no_option(self):
        for name in ("catalog", "validate-catalog"):
            self.assertEqual(set(vars(parser().parse_args([name]))), {"command", "manifest", "allowlist"})


class SurfaceTests(ServerCheckCase):
    def test_action_passes_the_two_inputs_and_holds_no_credential(self):
        contents = (ROOT / "actions" / "server-check" / "action.yml").read_text(encoding="utf-8")
        inputs = contents.split("\ninputs:\n", 1)[1].split("\nruns:\n", 1)[0]
        self.assertEqual(re.findall(r"(?m)^  (\S+):$", inputs), ["manifest", "image"])
        self.assertRegex(
            inputs, r"(?s)^  manifest:\n    description: [^\n]+\n    required: true\n  image:\n.*    default: ''$"
        )
        self.assertIn(
            '        args=(--manifest "$MANIFEST_PATH")\n'
            '        if [[ -n "$SERVER_IMAGE" ]]; then args+=(--image "$SERVER_IMAGE"); fi\n'
            '        python "$TOOL_ROOT/tools.py" server-check "${args[@]}"\n',
            contents,
        )
        for forbidden in ("token", "secrets.", "emit-fixture", "allowlist", '${{ inputs.manifest }}"', "sudo"):
            self.assertNotIn(
                forbidden, contents.split("\nruns:\n", 1)[1].replace("MANIFEST_PATH: ${{ inputs.manifest }}", "")
            )

    def test_ci_proves_the_check_on_a_real_server_in_both_directions(self):
        contents = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "        uses: ./actions/server-check\n        with:\n          manifest: ${{ runner.temp }}/floor.json\n",
            contents,
        )
        refusal = f"does not offer {servercheck.FIXTURE} 1.0.0.0 (targetAbi {servercheck.ABOVE_EVERY_SERVER})"
        self.assertIn(f"grep --fixed-strings --quiet '{refusal}'", contents)
        self.assertIn('[[ "$status" -eq 1 ]]', contents)

    def test_renovate_finds_the_default_image_pin(self):
        managers = read_json(ROOT / "renovate.json")["customManagers"]
        self.assertEqual(len(managers), 1)
        manager = managers[0]
        self.assertEqual((manager["customType"], manager["datasourceTemplate"]), ("regex", "docker"))
        (pattern,) = manager["managerFilePatterns"]
        self.assertTrue(pattern.startswith("/") and pattern.endswith("/"))
        self.assertTrue(re.search(pattern[1:-1], "jellysin_tooling/servercheck.py"))
        self.assertFalse(re.search(pattern[1:-1], "tests/jellysin_tooling/servercheck.py.orig"))
        (expression,) = manager["matchStrings"]
        source = (ROOT / "jellysin_tooling" / "servercheck.py").read_text(encoding="utf-8")
        (found,) = re.finditer(expression.replace("(?<", "(?P<"), source)
        self.assertEqual(found.group(0), f'SERVER_IMAGE = "{servercheck.SERVER_IMAGE}"')
        self.assertEqual(
            found.groupdict(),
            {
                "depName": "jellyfin/jellyfin",
                "currentValue": servercheck.SERVER_IMAGE.split(":")[1].split("@")[0],
                "currentDigest": DIGEST,
            },
        )


class ListingLimitTests(ServerCheckCase):
    def test_a_listing_exactly_at_both_bounds_is_read(self):
        path = self.manifest((FIRST_GUID, "1.0.0", "12.0.0.0"))
        control = servercheck.controls()
        entry = read_json(path)[0]
        filler = {"version": "9.9.9.0", "repositoryName": "elsewhere"}
        packages = [
            {**control[0], "versions": [{**control[0]["versions"][1], "repositoryName": servercheck.CONTROLS}]},
            {**entry, "versions": [{**entry["versions"][0], "repositoryName": servercheck.MANIFEST}] + [filler] * 1999},
        ]
        packages += [{"guid": f"22222222-2222-4222-8222-{index:012d}", "versions": [filler]} for index in range(62)]
        self.assertEqual((len(packages), len(packages[1]["versions"])), (64, 2000))
        self.jellyfin.packages = packages
        self.assertIsNone(self.outcome(path)[0])
