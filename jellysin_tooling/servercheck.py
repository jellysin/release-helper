"""Load a plugin repository file into a real Jellyfin server and require that the server offers every entry."""

import http.client
import json
import re
import secrets
import signal
import tempfile
import time
from pathlib import Path
from uuid import UUID

from . import catalog
from .common import JELLYFIN_ABI_MAJOR, MAX_JSON, ValidationError, decode_json, json_bytes, require
from .github import command, read_response

# The reviewed default server; tag and digest change together in one reviewed update.
SERVER_IMAGE = "jellyfin/jellyfin:12.4@sha256:fea863d0cd9ec445985d9c75fc6ed64e38a1d2766cf0c250a33d553f4fc1b771"
IMAGE = re.compile(r"(jellyfin/jellyfin)(?::[A-Za-z0-9][A-Za-z0-9._-]{0,63})?@(sha256:[0-9a-f]{64})")
SERVER_VERSION = re.compile(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}")
# The server serves its web directory at /web; files mounted there need no second container and no host network.
WEB_DIRECTORY = "/jellyfin/jellyfin-web"
INSIDE = "http://127.0.0.1:8096/web/"
MANIFEST = "manifest"
CONTROLS = "controls"
FILES = {MANIFEST: "jellysin-server-check-manifest.json", CONTROLS: "jellysin-server-check-controls.json"}
FLOOR = f"{JELLYFIN_ABI_MAJOR}.0.0.0"
ABOVE_EVERY_SERVER = f"{JELLYFIN_ABI_MAJOR}.65534.0.0"
CONTROL_MIXED = "4d47bc76-d44f-47a2-8695-f2e268c44570"
CONTROL_WITHHELD = "bbed0d7f-8463-4f2a-815c-1a7b5c14eb03"
FIXTURE = "d0973630-1561-4f3a-b26e-664229e6043e"
RESERVED = frozenset({CONTROL_MIXED, CONTROL_WITHHELD})
OFFERED = (CONTROLS, CONTROL_MIXED, "1.0.0.0")
# Fixture kinds: a file every supported server offers, and a valid file that no server offers.
FIXTURES = {"floor": FLOOR, "wrong-abi": ABOVE_EVERY_SERVER}
READY_SECONDS = 120
MAX_REPORTED = 20


def say(line):
    # Flushed at once, so that a later error on stderr follows the lines that led to it.
    print("server-check: " + line, flush=True)


def synthetic(guid, name, version, abi, repo="jellysin/server-check-control"):
    """Return the release fields that the catalog's own entry-building code reads."""
    return {
        "plugin": {
            "guid": guid,
            "name": name,
            "description": "Synthetic entry of the server check",
            "overview": "Never installable",
            "owner": "JellySin",
            "category": "General",
            "targetAbi": abi,
            "repository": repo,
        },
        "version": version + ".0",
        "tag": "v" + version,
        "timestamp": "2026-01-01T00:00:00Z",
        "archive": {
            "url": f"https://github.com/{repo}/releases/download/v{version}/{repo.split('/')[1]}-{version}.zip",
            "md5": "0" * 32,
        },
    }


def controls():
    """One plugin with an offered and a withheld version, and one plugin that is withheld entirely."""
    return catalog.merge_manifest(
        [],
        [
            synthetic(CONTROL_MIXED, "Server check control", "1.0.0", FLOOR),
            synthetic(CONTROL_MIXED, "Server check control", "2.0.0", ABOVE_EVERY_SERVER),
            synthetic(CONTROL_WITHHELD, "Server check withheld control", "1.0.0", ABOVE_EVERY_SERVER),
        ],
    )


def emit_fixture(kind, path):
    """Write a valid plugin repository file for a caller's own positive or negative case; never replace a file."""
    require(kind in FIXTURES, "Unknown fixture; available: " + ", ".join(sorted(FIXTURES)))
    release = synthetic(FIXTURE, "Server check fixture", "1.0.0", FIXTURES[kind], "jellysin/server-check-fixture")
    data = json_bytes(catalog.merge_manifest([], [release]))
    try:
        with Path(path).open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        raise ValidationError("Fixture path exists; the command creates a new file only") from None


def read_manifest(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_JSON + 1)
    manifest = catalog.validate_manifest(decode_json(data))
    require(
        not RESERVED & {plugin["guid"] for plugin in manifest}, "Manifest uses an identity of the built-in controls"
    )
    expected = [
        (plugin["guid"], version["version"], version["targetAbi"])
        for plugin in manifest
        for version in plugin["versions"]
    ]
    return data, expected


def docker(runner, step, arguments, timeout=120):
    try:
        return runner(["docker", *arguments], timeout=timeout)
    except ValidationError as exc:
        raise ValidationError(f"Server container step failed: {step}") from exc


def start(runner, name, image, files):
    """Start the server with the files in its web directory; return the loopback port and the digest that runs."""
    repository, digest = IMAGE.fullmatch(image).groups()
    mounts = []
    for path in files:
        require("," not in str(path), "Temporary path cannot be mounted")
        mounts += ["--mount", f"type=bind,src={path},dst={WEB_DIRECTORY}/{path.name},readonly"]
    limits = ["--user", "1000:1000", "--memory", "1536m", "--cpus", "2", "--stop-timeout", "20"]
    storage = ["--tmpfs", "/config:uid=1000,gid=1000", "--tmpfs", "/cache:uid=1000,gid=1000"]
    publish = ["--publish", "127.0.0.1::8096"]
    docker(runner, "run", ["run", "--detach", "--name", name, *limits, *storage, *publish, *mounts, image])
    running = docker(runner, "inspect", ["inspect", "--format", "{{.Image}}", name])
    digests = decode_json(
        docker(runner, "image inspect", ["image", "inspect", "--format", "{{json .RepoDigests}}", running]).encode()
    )
    # No silent substitute: the container must run the digest that was asked for.
    require(isinstance(digests, list) and f"{repository}@{digest}" in digests, "Container runs another image")
    port = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", docker(runner, "port", ["port", name, "8096/tcp"]))
    require(port, "Server port is not published on loopback only")
    return int(port.group(1)), f"{repository}@{digest}"


def remove(runner, name):
    listed = ["ps", "--all", "--filter", f"name=^{name}$", "--format", "{{.Names}}"]
    if not docker(runner, "list", listed):
        return
    try:
        # A stopped container needs no kill at removal; the daemon can fail to deliver one to a busy server.
        docker(runner, "stop", ["stop", name])
    finally:
        docker(runner, "remove", ["rm", "--force", "--volumes", name])
        require(not docker(runner, "list", listed), "Server container could not be removed")


class Server:
    """Plain HTTP to the check's own container on loopback: fixed address, no proxy, no redirect, bounded reads."""

    def __init__(self, port):
        self.port = port
        self.token = ""
        self.device = secrets.token_hex(16)

    def call(self, method, path, body=None):
        identity = f'MediaBrowser Client="jellysin-server-check", Device="check", DeviceId="{self.device}"'
        identity += ', Version="1.0.0"' + (f', Token="{self.token}"' if self.token else "")
        headers = {"Authorization": identity, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            status = response.status
            payload = read_response(response, MAX_JSON, time.monotonic() + 60)
        except (OSError, http.client.HTTPException) as exc:
            raise ValidationError("Server request failed or timed out") from exc
        finally:
            connection.close()
        # Paths are constants of this module; the response body is never echoed.
        require(status in (200, 204), f"Server answered {method} {path} with HTTP {status}")
        return decode_json(payload) if payload else None


def wait_ready(server, pause, clock=time.monotonic):
    deadline = clock() + READY_SECONDS
    while clock() < deadline:
        try:
            info = server.call("GET", "/System/Info/Public")
            server.call("GET", "/Startup/Configuration")
        except ValidationError:
            # Refused connections and early error statuses are the expected answers of a starting server.
            pause(0.5)
            continue
        version = info.get("Version") if isinstance(info, dict) else None
        require(isinstance(version, str) and SERVER_VERSION.fullmatch(version), "Server reports no usable version")
        return version
    raise ValidationError(f"Server was not ready within {READY_SECONDS} seconds")


def sign_in(server):
    """Finish the first-run wizard of the throwaway server and sign in as its only user."""
    password = secrets.token_urlsafe(24)
    culture = {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"}
    server.call("POST", "/Startup/Configuration", culture)
    server.call("GET", "/Startup/User")
    server.call("POST", "/Startup/User", {"Name": "check", "Password": password})
    server.call("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    server.call("POST", "/Startup/Complete")
    session = server.call("POST", "/Users/AuthenticateByName", {"Username": "check", "Pw": password})
    token = session.get("AccessToken") if isinstance(session, dict) else None
    require(isinstance(token, str) and re.fullmatch(r"[0-9a-f]{16,128}", token), "Server sign-in returned no token")
    server.token = token


def listing(server):
    """Replace the server's repositories with the mounted files and return what the server offers."""
    repositories = [{"Name": name, "Url": INSIDE + file, "Enabled": True} for name, file in FILES.items()]
    server.call("POST", "/Repositories", repositories)
    packages = server.call("GET", "/Packages")
    require(isinstance(packages, list) and len(packages) <= 64, "Server returned an invalid package list")
    listed = set()
    for package in packages:
        versions = package.get("versions") if isinstance(package, dict) else None
        require(isinstance(versions, list) and len(versions) <= 2000, "Server returned an invalid package")
        try:
            guid = str(UUID(package.get("guid")))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Server returned an invalid package identity") from None
        for version in versions:
            require(isinstance(version, dict), "Server returned an invalid package version")
            listed.add((version.get("repositoryName"), guid, version.get("version")))
    return listed


def judge(expected, listed):
    """Return the manifest entries the server withholds, after the controls proved that the listing means something."""
    require(OFFERED in listed, "Control failed: the server offers no compatible entry, so its list proves nothing")
    require(
        {entry for entry in listed if entry[0] == CONTROLS} == {OFFERED},
        "Control failed: the server offers an entry above its own version, so it does not filter",
    )
    return [entry for entry in expected if (MANIFEST, entry[0], entry[1]) not in listed]


def interrupted(_number, _frame):
    raise SystemExit(143)


def offered(runner, pause, image, data):
    """Run one throwaway server over the manifest bytes and the controls; the container never outlives the call."""
    name = "jellysin-server-check-" + secrets.token_hex(6)
    docker(runner, "pull", ["pull", "--quiet", image], timeout=600)
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="jellysin-server-check-") as temporary:
            Path(temporary).chmod(0o755)
            files = []
            for key, content in ((MANIFEST, data), (CONTROLS, json_bytes(controls()))):
                files.append(Path(temporary) / FILES[key])
                files[-1].write_bytes(content)
                files[-1].chmod(0o644)
            try:
                port, digest = start(runner, name, image, files)
                say(f"image {digest}")
                server = Server(port)
                version = wait_ready(server, pause)
                say(f"server version {version}")
                sign_in(server)
                return listing(server), version
            finally:
                remove(runner, name)
    finally:
        signal.signal(signal.SIGTERM, previous)


def check(manifest_path, image=None, runner=command, pause=time.sleep):
    image = SERVER_IMAGE if image is None else image
    require(
        isinstance(image, str) and IMAGE.fullmatch(image), "Server image must be jellyfin/jellyfin with a sha256 digest"
    )
    data, expected = read_manifest(manifest_path)
    listed, version = offered(runner, pause, image, data)
    missing = judge(expected, listed)
    say("controls passed (compatible entry offered, entries above the server withheld)")
    for guid, number, abi in missing[:MAX_REPORTED]:
        say(f"server {version} does not offer {guid} {number} (targetAbi {abi})")
    require(not missing, f"Server {version} withholds {len(missing)} of {len(expected)} manifest entries")
    say(f"{len(expected)} expected entries, all offered by server {version}")
