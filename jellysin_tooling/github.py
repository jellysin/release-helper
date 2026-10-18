"""Small HTTPS GitHub client with fixed hosts, bounded reads, and no token redirects."""

import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from .common import MAX_ASSET, MAX_JSON, TOOL_NAME, ValidationError, decode_json, require

DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _response, _code, _message, _headers, _newurl):
        return None


def safe_url(url, hosts):
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname in hosts, "Unapproved download host")
    require(parsed.username is None and parsed.password is None and parsed.port in (None, 443), "Unsafe URL authority")
    require(not parsed.fragment, "Unexpected URL fragment")
    return url


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, url, limit=MAX_JSON, authenticated=False, binary=False):
        hosts = {"api.github.com"} if authenticated else DOWNLOAD_HOSTS
        deadline = time.monotonic() + 90
        for _attempt in range(5):
            safe_url(url, hosts)
            headers = {
                "User-Agent": TOOL_NAME,
                "Accept": "application/vnd.github+json" if authenticated and not binary else "application/octet-stream",
            }
            if authenticated and self.token:
                headers["Authorization"] = "Bearer " + self.token
            try:
                with self.opener.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
                    return read_response(response, limit, deadline)
            except urllib.error.HTTPError as exc:
                # The error holds the response body and socket until closed.
                exc.close()
                if (not authenticated or binary) and exc.code in (301, 302, 303, 307, 308):
                    url = urllib.parse.urljoin(url, exc.headers.get("Location", ""))
                    authenticated = False
                    hosts = DOWNLOAD_HOSTS
                    continue
                raise ValidationError(f"GitHub request failed with HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                raise ValidationError("GitHub request failed or timed out") from exc
        raise ValidationError("Too many download redirects")

    def api(self, path):
        require(path.startswith("repos/") and ".." not in path, "Invalid API path")
        return decode_json(self.get("https://api.github.com/" + path, authenticated=True))

    def download(self, url):
        return self.get(url, MAX_ASSET)

    def download_asset(self, repo, asset_id):
        require(type(asset_id) is int and asset_id > 0, "Invalid GitHub asset ID")
        return self.get(
            f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}",
            MAX_ASSET,
            authenticated=True,
            binary=True,
        )


def read_response(response, limit, deadline):
    parts = []
    total = 0
    while total <= limit:
        require(time.monotonic() < deadline, "Download exceeded deadline")
        part = response.read(min(65536, limit + 1 - total))
        if not part:
            return b"".join(parts)
        total += len(part)
        parts.append(part)
    raise ValidationError("Download exceeds size limit")


def command(arguments, cwd=None, timeout=120):
    try:
        result = subprocess.run(arguments, cwd=cwd, capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationError("Required command unavailable or timed out") from exc
    # Never echo stderr: gh errors may include signed download URLs or response data.
    require(result.returncode == 0, f"{arguments[0]} command failed ({result.returncode})")
    require(len(result.stdout) <= MAX_JSON, "Command output exceeds limit")
    return result.stdout.decode("utf-8").strip()


def verify_attestation(path, repo, workflow, commit, tag):
    return command(
        [
            "gh",
            "attestation",
            "verify",
            str(path),
            "--repo",
            repo,
            "--signer-workflow",
            f"{repo}/{workflow}",
            "--source-digest",
            commit,
            "--source-ref",
            "refs/tags/" + tag,
            "--deny-self-hosted-runners",
            "--limit",
            "10",
        ]
    )
