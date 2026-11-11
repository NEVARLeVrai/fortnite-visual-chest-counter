"""
Which releases exist on GitHub, and which one is the newest this copy of the app may update to.

Every release so far is a Public Beta, which GitHub marks as a pre-release, and /releases/latest skips those. So this reads
the release list and chooses for itself. Only an exact vMAJOR.MINOR.PATCH tag counts: an RC build, or any other tag, is not
an update. A release is complete only when it has the zip and both signing files; anything else is ignored.

The address comes from GITHUB below. Nothing in config.toml, the environment or the command line can change it: only tests
build another ReleaseSource, in code.
"""

import contextlib
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from fortnite_chest_counter.version import __version__

MANIFEST_NAME = "update-manifest.json"
SIGNATURE_NAME = MANIFEST_NAME + ".sig"
MAX_API_BYTES = 4 * 1024 * 1024            # 30 releases with their notes are about 200 KB
MAX_SMALL_ASSET = 16 * 1024                # the manifest and the signature file are a few hundred bytes
USER_AGENT = f"Fortnite-Visual-Chest-Counter/{__version__}"
_NUMBER = "(0|[1-9][0-9]{0,8})"            # [0-9], not \d: \d also accepts digits from other alphabets
_VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}")
_DIGEST = re.compile("sha256:([0-9a-f]{64})")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class UpdateError(Exception):
    """An expected updater failure. `kind` is for the program; the message is written for the user; `detail` keeps the
    technical cause (the operation and the system error) for the log."""

    def __init__(self, kind, message, detail=""):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class ReleaseSource:
    url: str
    allow_loopback_http: bool = False      # tests only: a mock GitHub on this PC
    timeout: float = 10.0                  # seconds without any network progress
    total_timeout: float = 20.0            # seconds for one whole request, so a trickling server cannot hold it open


GITHUB = ReleaseSource("https://api.github.com/repos/NEVARLeVrai/fortnite-visual-chest-counter/releases?per_page=30")


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    download_url: str
    api_url: str | None                    # GitHub's second route to the same file
    sha256: str | None                     # GitHub's own digest of the stored file, when it reports one


@dataclass(frozen=True)
class Release:
    version: tuple
    tag: str
    title: str
    notes: str                             # text from GitHub: show it as plain text
    prerelease: bool
    page_url: str
    package: Asset
    manifest: Asset
    signature: Asset

    @property
    def version_text(self):
        return version_text(self.version)


def zip_name(version):
    return f"Fortnite-Visual-Chest-Counter-v{version}.zip"


def parse_version(text):
    """'0.2.3' -> (0, 2, 3). Anything else -> None."""
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return tuple(int(n) for n in match.groups()) if match else None


def parse_tag(tag):
    """'v0.2.3' -> (0, 2, 3). RC builds and every other kind of tag -> None."""
    return parse_version(tag[1:]) if isinstance(tag, str) and tag.startswith("v") else None


def version_text(version):
    return ".".join(str(n) for n in version)


def url_allowed(url, source):
    """HTTPS only (tests may also use plain HTTP to this PC). Which host is deliberately not checked: GitHub can move its
    file storage, and a fixed host name would then break the updater in every copy already installed."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except (ValueError, AttributeError, TypeError):
        return False
    if parts.scheme == "https":
        return bool(host)
    return source.allow_loopback_http and parts.scheme == "http" and host in _LOOPBACK


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """GitHub redirects downloads to its file storage; a redirect may never leave HTTPS."""

    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl, self.source):
            raise UpdateError("bad_redirect", "The update server sent the app to an unsafe address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_failure(error):
    headers = error.headers
    limited = error.code == 429 or (error.code == 403 and (headers.get("X-RateLimit-Remaining") == "0"
                                                           or headers.get("Retry-After") is not None))
    code = error.code
    error.close()
    if limited:
        return UpdateError("rate_limited", "GitHub is limiting update checks from this network. Please try again later.")
    if code == 416:
        return UpdateError("bad_range", "The server could not continue the download from where it stopped.")
    return UpdateError("api_error", f"GitHub answered with an error (HTTP {code}). Please try again later.")


@contextlib.contextmanager
def network_errors():
    """Everything the network or the server can do wrong becomes an UpdateError."""
    try:
        yield
    except UpdateError:
        raise
    except urllib.error.HTTPError as error:
        raise _http_failure(error) from None
    except TimeoutError:
        raise UpdateError("timeout", "GitHub took too long to answer. Please try again later.") from None
    except urllib.error.URLError as error:
        kind = "timeout" if isinstance(error.reason, TimeoutError) else "network"
        raise UpdateError(kind, "Could not reach GitHub. Check the internet connection and try again.") from None
    except (OSError, http.client.HTTPException, ValueError):
        raise UpdateError("network", "The connection to GitHub failed. Please try again.") from None


def open_response(url, source, accept, extra_headers=None):
    """An open response (use it in a `with`). Redirects are followed, and may never leave HTTPS."""
    if not url_allowed(url, source):
        raise UpdateError("bad_response", "The update information contained an unsafe address.")
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": USER_AGENT, **(extra_headers or {})})
    opener = urllib.request.build_opener(_SafeRedirects(source),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with network_errors():
        return opener.open(request, timeout=source.timeout)


def http_get(url, source, accept, max_bytes):
    """The body of one small GET, within the source's total deadline."""
    deadline = time.monotonic() + source.total_timeout
    response = open_response(url, source, accept)
    body = bytearray()
    with response, network_errors():
        expected = response.headers.get("Content-Length")
        while chunk := response.read1(64 * 1024):          # read1: returns as data arrives, so the deadline is checked
            body += chunk
            if len(body) > max_bytes:
                raise UpdateError("bad_response", "The update server sent far more data than expected.")
            if time.monotonic() > deadline:
                raise TimeoutError
    if expected is not None and expected.isdigit() and int(expected) != len(body):
        raise UpdateError("network", "The connection to GitHub was cut off. Please try again.")
    return bytes(body)


def fetch_release_list(source=GITHUB):
    """The raw list of releases from the API."""
    raw = http_get(source.url, source, "application/vnd.github+json", MAX_API_BYTES)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise UpdateError("bad_response", "GitHub sent an answer the app could not read.") from None
    if not isinstance(data, list):
        raise UpdateError("bad_response", "GitHub sent an answer the app could not read.")
    return data


def _asset(raw, source):
    """A finished, usable asset from the API, or None."""
    if not isinstance(raw, dict) or raw.get("state", "uploaded") != "uploaded":
        return None
    name, size, url = raw.get("name"), raw.get("size"), raw.get("browser_download_url")
    if not isinstance(name, str) or type(size) is not int or size <= 0 or not url_allowed(url, source):
        return None
    api_url = raw.get("url") if url_allowed(raw.get("url"), source) else None
    digest = _DIGEST.fullmatch(raw["digest"]) if isinstance(raw.get("digest"), str) else None
    return Asset(name, size, url, api_url, digest.group(1) if digest else None)


def _text(value):
    return value if isinstance(value, str) else ""


def parse_release(entry, source=GITHUB):
    """One API entry -> a complete Release, or None for a draft, a tag that is not a version, or an incomplete release."""
    if not isinstance(entry, dict) or entry.get("draft") is not False:
        return None
    version = parse_tag(entry.get("tag_name"))
    if version is None or not isinstance(entry.get("assets"), list):
        return None
    assets = {a.name: a for a in (_asset(raw, source) for raw in entry["assets"]) if a is not None}
    package, manifest, signature = (assets.get(zip_name(version_text(version))), assets.get(MANIFEST_NAME),
                                    assets.get(SIGNATURE_NAME))
    if package is None or manifest is None or signature is None:
        return None
    page = _text(entry.get("html_url"))
    return Release(version, entry["tag_name"], _text(entry.get("name")), _text(entry.get("body")),
                   entry.get("prerelease") is True, page if url_allowed(page, source) else "", package, manifest, signature)


def select_update(entries, installed, source=GITHUB):
    """The highest complete release newer than `installed` (a version tuple), or None."""
    releases = (parse_release(entry, source) for entry in entries)
    return max((r for r in releases if r is not None and r.version > installed), key=lambda r: r.version, default=None)


def fetch_small_asset(asset, source=GITHUB):
    """A manifest or signature file. If the normal download route fails, GitHub's API route serves the same file."""
    if asset.size > MAX_SMALL_ASSET:
        raise UpdateError("bad_response", "A release file is much larger than expected.")
    try:
        return http_get(asset.download_url, source, "application/octet-stream", MAX_SMALL_ASSET)
    except UpdateError as error:
        if asset.api_url is None or error.kind not in ("timeout", "network", "api_error"):
            raise
    return http_get(asset.api_url, source, "application/octet-stream", MAX_SMALL_ASSET)
