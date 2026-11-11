"""
The updater's first layer (v0.2.3): finding releases, choosing the right one and verifying its Ed25519 signature, against a
mock GitHub on this PC. Nothing here touches the real GitHub, and every key is generated fresh for the run.
Run:   python tests/test_update.py   (or pytest)
"""

import sys
from pathlib import Path as _P

ROOT = _P(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "src"))   # run without installing

import ast
import base64
import dataclasses
import hashlib
import inspect
import json
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from Crypto.PublicKey import ECC

import build_release
import sign_release as S
from fortnite_chest_counter.update import authenticity as A
from fortnite_chest_counter.update import check as C
from fortnite_chest_counter.update import releases as R
from fortnite_chest_counter.update import trust

PACKAGE = b"PK synthetic release package, not a real zip\n" * 40
PRIMARY, BACKUP, STRANGER = (ECC.generate(curve="ed25519") for _ in range(3))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hex_of(key):
    return S.public_key_hex(key)


def id_of(key):
    return A.key_id(bytes.fromhex(hex_of(key)))


def manifest_for(version="0.2.3", package=PACKAGE):
    return A.build_manifest(version, R.zip_name(version), sha(package), len(package))


def send(handler, status, body=b"", headers=()):
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def answer(status, body=b"", headers=()):
    return lambda handler: send(handler, status, body, headers)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        owner = self.server.owner
        owner.requests.append(self.path)
        try:
            owner.fault(self) if self.path in owner.faults else owner.serve(self)
        except OSError:
            pass                               # the client left, which several tests do on purpose


class MockGitHub:
    """The parts of the GitHub Releases API the updater uses. `faults` replaces the answer for one path."""

    def __init__(self, **source_options):
        self.releases, self.files, self.faults, self.requests = [], {}, {}, []
        self.source_options = source_options

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *args: None
        self.server.owner = self
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.source = R.ReleaseSource(self.url + "/releases", allow_loopback_http=True, **self.source_options)
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def fault(self, handler):
        self.faults[handler.path](handler)

    def serve(self, handler):
        if handler.path == "/releases":
            send(handler, 200, json.dumps(self.releases).encode("utf-8"))
        elif handler.path in self.files:
            send(handler, 200, self.files[handler.path])
        else:
            send(handler, 404, b"{}")

    def add(self, version="0.2.3", tag=None, draft=False, prerelease=True, keys=(PRIMARY,), manifest=None, signature=None,
            omit=(), package=PACKAGE):
        """Publish a release on the mock: valid and signed by PRIMARY, unless told otherwise. -> its API entry."""
        tag = tag or f"v{version}"
        name = R.zip_name(version)
        manifest = manifest_for(version, package) if manifest is None else manifest
        signature = S.sign_manifest(manifest, keys) if signature is None else signature
        assets = []
        for asset_name, body in ((name, package), (R.MANIFEST_NAME, manifest), (R.SIGNATURE_NAME, signature)):
            if asset_name in omit:
                continue
            download, api = f"/dl/{tag}/{asset_name}", f"/api/{tag}/{asset_name}"
            self.files[download] = self.files[api] = body
            assets.append({"name": asset_name, "size": len(body), "state": "uploaded", "digest": "sha256:" + sha(body),
                           "browser_download_url": self.url + download, "url": self.url + api})
        entry = {"tag_name": tag, "name": f"Fortnite Visual Chest Counter {tag} - Public Beta", "body": "What changed.",
                 "draft": draft, "prerelease": prerelease, "html_url": f"{self.url}/page/{tag}", "assets": assets}
        self.releases.append(entry)
        return entry


def check(github, installed="0.2.2", keys=None):
    return C.check_for_update(installed=installed, source=github.source,
                              public_keys=(hex_of(PRIMARY),) if keys is None else keys)


def assert_result(result, status, kind=None):
    assert result.status == status and result.kind == kind, result
    assert (result.update is not None) == (status == "available"), result     # only "available" ever carries an update


def assert_offers(result, version):
    assert_result(result, "available")
    assert result.update.release.version_text == version, result


def expect(kind, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except R.UpdateError as error:
        assert error.kind == kind, f"got {error.kind}, wanted {kind}: {error}"
        return error
    raise AssertionError(f"expected an UpdateError of kind {kind}")


def asset_named(entry, name):
    return next(a for a in entry["assets"] if a["name"] == name)


def verified_parts(version="0.2.3", keys=(PRIMARY,)):
    """(Release, manifest bytes, signature bytes) as the discovery step would hand them to verification."""
    with MockGitHub() as github:
        entry = github.add(version, keys=keys)
        return (R.parse_release(entry, github.source), github.files[f"/dl/v{version}/{R.MANIFEST_NAME}"],
                github.files[f"/dl/v{version}/{R.SIGNATURE_NAME}"])


# ---- versions

def test_version_tags_are_parsed_strictly():
    assert R.parse_tag("v0.2.3") == (0, 2, 3) and R.parse_tag("v10.20.30") == (10, 20, 30) and R.parse_tag("v1.0.0") == (1, 0, 0)
    for bad in ("0.2.3", "V0.2.3", "v0.2", "v0.2.3.4", "v0.2.3-rc1", "v0.2.3rc1", "v0.2.3-beta", "v0.02.3", "v01.2.3", "v.2.3",
                "v0.2.3\n", " v0.2.3", "v0.2.3 ", "v٣.2.3", "v" + "9" * 5000 + ".0.0", "", None, 123, ["v0.2.3"]):
        assert R.parse_tag(bad) is None, repr(bad)
    assert R.parse_version("0.2.3") == (0, 2, 3) and R.parse_version("v0.2.3") is None


def test_versions_are_compared_as_numbers():
    assert R.parse_version("0.2.10") > R.parse_version("0.2.9") > R.parse_version("0.2.3")
    assert R.parse_version("0.10.0") > R.parse_version("0.9.9") and R.parse_version("1.0.0") > R.parse_version("0.99.99")


# ---- choosing a release

def test_no_newer_release_means_up_to_date():
    with MockGitHub() as github:
        github.add("0.2.2")
        github.add("0.2.1")
        assert_result(check(github), "up_to_date")
    with MockGitHub() as github:                                              # no releases at all
        assert_result(check(github), "up_to_date")


def test_a_newer_public_beta_is_offered_without_downloading_the_zip():
    with MockGitHub() as github:
        github.add("0.2.3", prerelease=True)
        result = check(github)
        assert_offers(result, "0.2.3")
        update = result.update
        assert update.signed_by == id_of(PRIMARY) and update.release.prerelease and update.release.notes == "What changed."
        assert update.manifest == A.Manifest("0.2.3", R.zip_name("0.2.3"), sha(PACKAGE), len(PACKAGE))
        assert not any(path.endswith(".zip") for path in github.requests), github.requests


def test_a_stable_release_is_offered_too():
    with MockGitHub() as github:
        github.add("0.3.0", prerelease=False)
        assert_offers(check(github), "0.3.0")


def test_the_highest_version_wins_in_any_order():
    with MockGitHub() as github:
        for version in ("0.2.9", "0.3.0", "0.2.10", "0.10.0", "0.2.3", "0.9.1"):
            github.add(version)
        assert_offers(check(github), "0.10.0")
    with MockGitHub() as github:
        for version in ("0.2.9", "0.2.10", "0.2.3"):
            github.add(version)
        assert_offers(check(github), "0.2.10")


def test_drafts_rc_builds_and_malformed_tags_are_ignored_even_when_higher():
    with MockGitHub() as github:
        github.add("9.0.0", draft=True)
        for tag in ("v9.0.0-rc1", "v9.0", "9.0.0", "release-9", "v9.0.0.1", "V9.0.0"):
            github.add("0.2.3", tag=tag)
        github.releases += [None, 5, "text", [], {}, {"tag_name": "v9.0.0"}, {"tag_name": "v9.0.0", "draft": False},
                            {"tag_name": "v9.0.0", "draft": False, "assets": "none"},
                            {"tag_name": "v9.0.0", "draft": None, "assets": []}]
        assert_result(check(github), "up_to_date")
        github.add("0.2.4")
        assert_offers(check(github), "0.2.4")


def test_older_and_equal_versions_are_never_offered():
    with MockGitHub() as github:
        for version in ("0.2.5", "0.2.4", "0.2.1", "0.1.1"):
            github.add(version)
        assert_result(check(github, installed="0.2.5"), "up_to_date")


def test_an_incomplete_release_is_skipped_for_the_next_complete_one():
    for missing in (R.zip_name("0.3.0"), R.MANIFEST_NAME, R.SIGNATURE_NAME):
        with MockGitHub() as github:
            github.add("0.3.0", omit=(missing,))
            assert_result(check(github), "up_to_date")
            github.add("0.2.3")
            assert_offers(check(github), "0.2.3")


def test_a_zip_named_for_another_version_makes_the_release_incomplete():
    with MockGitHub() as github:
        github.add("0.2.3", tag="v0.3.0")
        assert_result(check(github), "up_to_date")


def test_assets_still_uploading_or_with_a_bad_size_are_ignored():
    for change in ({"state": "starter"}, {"size": 0}, {"size": -1}, {"size": True}, {"size": "100"}, {"size": None},
                   {"browser_download_url": "ftp://example.com/x"}, {"browser_download_url": None}, {"name": None}):
        with MockGitHub() as github:
            entry = github.add("0.3.0")
            asset_named(entry, R.zip_name("0.3.0")).update(change)
            assert_result(check(github), "up_to_date")


def test_only_https_addresses_are_accepted():
    production, local = R.GITHUB, R.ReleaseSource("http://127.0.0.1:1/releases", allow_loopback_http=True)
    assert R.url_allowed("https://github.com/x", production) and R.url_allowed("https://github.com/x", local)
    for url in ("http://github.com/x", "http://127.0.0.1/x", "file:///c:/x", "ftp://github.com/x", "https://", "//github.com/x",
                "github.com/x", "http://[bad/x", "", None, 5):
        assert not R.url_allowed(url, production), repr(url)
    assert R.url_allowed("http://127.0.0.1:9/x", local) and R.url_allowed("http://localhost/x", local)
    assert not R.url_allowed("http://example.com/x", local) and not R.url_allowed("file:///c:/x", local)
    with MockGitHub() as github:
        entry = github.add("0.3.0")                                           # its addresses are plain http to this PC
        assert R.parse_release(entry, github.source) is not None and R.parse_release(entry, R.GITHUB) is None


# ---- network and API failures

def test_a_refused_connection_fails_cleanly():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = R.ReleaseSource(f"http://127.0.0.1:{port}/releases", allow_loopback_http=True)
    assert_result(C.check_for_update(installed="0.2.2", source=source, public_keys=()), "failed", "network")


def test_a_silent_server_times_out():
    with MockGitHub(timeout=0.2) as github:
        github.faults["/releases"] = lambda handler: time.sleep(1.0)
        assert_result(check(github), "failed", "timeout")


def test_a_trickling_server_hits_the_total_deadline():
    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100000")
        handler.end_headers()
        for _ in range(400):
            handler.wfile.write(b" ")
            time.sleep(0.02)

    with MockGitHub(timeout=3.0, total_timeout=0.3) as github:
        github.faults["/releases"] = trickle
        started = time.monotonic()
        assert_result(check(github), "failed", "timeout")
        assert time.monotonic() - started < 2.0


def test_http_errors_fail_cleanly():
    for status in (500, 502, 503, 404, 401):
        with MockGitHub() as github:
            github.faults["/releases"] = answer(status, b"nope")
            assert_result(check(github), "failed", "api_error")


def test_rate_limits_are_recognised():
    for status, headers in ((403, [("X-RateLimit-Remaining", "0")]), (403, [("Retry-After", "60")]), (429, [])):
        with MockGitHub() as github:
            github.faults["/releases"] = answer(status, b"{}", headers)
            assert_result(check(github), "failed", "rate_limited")
    with MockGitHub() as github:
        github.faults["/releases"] = answer(403, b"{}", [("X-RateLimit-Remaining", "12")])
        assert_result(check(github), "failed", "api_error")


def test_invalid_api_answers_fail_cleanly():
    for body in (b"not json", b'{"message": "hello"}', b"null", b'"text"', b"5", b"\xff\xfe\x00", b"", b"[" * 200000):
        with MockGitHub() as github:
            github.faults["/releases"] = answer(200, body)
            assert_result(check(github), "failed", "bad_response")


def test_an_oversized_answer_is_refused():
    with MockGitHub() as github:
        github.faults["/releases"] = answer(200, b"[" + b" " * R.MAX_API_BYTES + b"]")
        assert_result(check(github), "failed", "bad_response")


def test_a_cut_off_answer_is_noticed():
    def cut_off(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        handler.wfile.write(b"[]")

    with MockGitHub() as github:
        github.faults["/releases"] = cut_off
        assert_result(check(github), "failed", "network")


def test_redirects_may_not_leave_https():
    with MockGitHub() as github:
        github.faults["/releases"] = answer(302, b"", [("Location", "http://example.com/releases")])
        assert_result(check(github), "failed", "bad_redirect")
    with MockGitHub() as github:
        github.faults["/releases"] = answer(302, b"", [("Location", github.url + "/moved")])
        github.faults["/moved"] = answer(200, b"[]")
        assert_result(check(github), "up_to_date")


def test_failing_to_fetch_a_signing_file_is_a_failure_and_never_an_update():
    with MockGitHub() as github:
        github.add("0.2.3")
        for route in ("dl", "api"):
            github.faults[f"/{route}/v0.2.3/{R.SIGNATURE_NAME}"] = answer(500)
        assert_result(check(github), "failed", "api_error")


def test_the_second_download_route_is_used_when_the_first_fails():
    with MockGitHub() as github:
        github.add("0.2.3")
        github.faults[f"/dl/v0.2.3/{R.MANIFEST_NAME}"] = answer(500)
        assert_offers(check(github), "0.2.3")
        assert f"/api/v0.2.3/{R.MANIFEST_NAME}" in github.requests


def test_signing_files_have_a_size_limit():
    with MockGitHub() as github:
        entry = github.add("0.2.3")
        asset_named(entry, R.MANIFEST_NAME)["size"] = R.MAX_SMALL_ASSET + 1
        assert_result(check(github), "failed", "bad_response")
    with MockGitHub() as github:
        github.add("0.2.3")
        github.files[f"/dl/v0.2.3/{R.MANIFEST_NAME}"] = b"x" * (R.MAX_SMALL_ASSET + 1)
        assert_result(check(github), "failed", "bad_response")


def test_a_development_version_cannot_be_compared():
    with MockGitHub() as github:
        github.add("0.2.3")
        assert_result(check(github, installed="0.2.3-dev"), "failed", "bad_version")
        assert github.requests == []


# ---- staying out of the app window's way

def test_the_check_runs_in_the_background():
    def slow(handler):
        time.sleep(0.6)
        send(handler, 200, b"[]")

    with MockGitHub() as github:
        github.faults["/releases"] = slow
        started = time.monotonic()
        background = C.BackgroundCheck(installed="0.2.2", source=github.source, public_keys=()).start()
        assert time.monotonic() - started < 0.2 and background.result() is None
        deadline = time.monotonic() + 5
        while background.result() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert_result(background.result(), "up_to_date")


def test_a_bug_in_the_check_cannot_escape_the_thread():
    def broken(**options):
        raise RuntimeError("bug")

    original = C.check_for_update
    C.check_for_update = broken
    try:
        background = C.BackgroundCheck().start()
        deadline = time.monotonic() + 5
        while background.result() is None and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        C.check_for_update = original
    assert_result(background.result(), "failed", "unexpected")


# ---- the update server is fixed

def test_the_update_server_cannot_be_changed_from_outside():
    assert R.GITHUB.url.startswith("https://api.github.com/repos/NEVARLeVrai/fortnite-visual-chest-counter/releases")
    assert R.GITHUB.allow_loopback_http is False
    assert inspect.signature(C.check_for_update).parameters["source"].default is R.GITHUB
    folder = ROOT / "src" / "fortnite_chest_counter" / "update"
    for file in folder.glob("*.py"):
        text = file.read_text(encoding="utf-8")
        if file.name == "install_helper.py":                                  # a separate program: its arguments are only a folder and a pid
            for forbidden in ("os.environ", "getenv", "urllib", "http.client", "TRUSTED_PUBLIC_KEYS", "https://"):
                assert forbidden not in text, f"{file.name} uses {forbidden}"
            continue
        for forbidden in ("os.environ", "getenv", "sys.argv", "load_config", "import tomllib", "import settings"):
            assert forbidden not in text, f"{file.name} uses {forbidden}"


# ---- signatures and the manifest

def test_a_valid_signature_is_accepted():
    manifest = manifest_for()
    assert A.verify_signature(manifest, S.sign_manifest(manifest, [PRIMARY]), [hex_of(PRIMARY)]) == id_of(PRIMARY)


def test_the_manifest_has_one_exact_form():
    expected = ('{"format":1,"product":"fortnite-visual-chest-counter","sha256":"' + "a" * 64 +
                '","size":1234,"version":"0.2.3","zip":"Fortnite-Visual-Chest-Counter-v0.2.3.zip"}\n').encode("ascii")
    assert A.build_manifest("0.2.3", "Fortnite-Visual-Chest-Counter-v0.2.3.zip", "a" * 64, 1234) == expected
    manifest = manifest_for()
    assert S.sign_manifest(manifest, [PRIMARY]) == S.sign_manifest(manifest, [PRIMARY])         # Ed25519 is deterministic


def test_a_validly_signed_manifest_in_any_other_form_is_rejected():
    canonical = manifest_for()
    fields = json.loads(canonical)
    variants = [json.dumps(fields, indent=2).encode() + b"\n", json.dumps(fields, sort_keys=True).encode() + b"\n",
                canonical.rstrip(b"\n"), canonical + b"\n", b" " + canonical, b"\xef\xbb\xbf" + canonical,
                canonical[:-2] + b',"zip":"Fortnite-Visual-Chest-Counter-v0.3.0.zip"}\n']         # a repeated key
    for raw in variants:
        with MockGitHub() as github:
            github.add("0.2.3", manifest=raw)
            assert_result(check(github), "rejected", "manifest_invalid")


def test_every_manifest_field_is_validated():
    good = {"format": 1, "product": A.PRODUCT, "sha256": "a" * 64, "size": 10, "version": "0.2.3", "zip": R.zip_name("0.2.3")}
    assert A.parse_manifest(A._canonical(good)).version == "0.2.3"
    for change in ({"format": 2}, {"format": True}, {"format": "1"}, {"format": None}, {"product": "another-app"},
                   {"version": "0.2"}, {"version": "v0.2.3"}, {"version": "0.2.3-rc1"}, {"version": 3}, {"zip": "other.zip"},
                   {"zip": R.zip_name("0.2.4")}, {"sha256": "A" * 64}, {"sha256": "a" * 63}, {"sha256": 5}, {"size": 0},
                   {"size": -5}, {"size": True}, {"size": "10"}, {"size": 1.5}):
        expect("manifest_invalid", A.parse_manifest, A._canonical({**good, **change}))
    for missing in good:
        expect("manifest_invalid", A.parse_manifest, A._canonical({k: v for k, v in good.items() if k != missing}))
    for raw in (b"", b"[]", b"null", b"not json", "é".encode("utf-8"), b"[" * 200000):
        expect("manifest_invalid", A.parse_manifest, raw)


def test_later_fields_are_ignored_only_when_they_are_signed():
    fields = {**json.loads(manifest_for()), "new_field": "from a later version"}
    raw = A._canonical(fields)
    with MockGitHub() as github:
        github.add("0.2.3", manifest=raw)
        assert_offers(check(github), "0.2.3")
    with MockGitHub() as github:
        github.add("0.2.3", manifest=raw, signature=S.sign_manifest(manifest_for(), [PRIMARY]))
        assert_result(check(github), "rejected", "signature_invalid")


def test_a_tampered_manifest_fails_the_signature():
    original = manifest_for()
    signature = S.sign_manifest(original, [PRIMARY])
    other_hash = sha(b"something else")
    tampered = [A.build_manifest("0.2.3", R.zip_name("0.2.3"), other_hash, len(PACKAGE)),
                A.build_manifest("0.2.3", R.zip_name("0.2.3"), sha(PACKAGE), len(PACKAGE) + 1),
                original.replace(b'"format":1', b'"format":2'),
                original.replace(sha(PACKAGE).encode(), other_hash.encode())]
    for raw in tampered:
        with MockGitHub() as github:
            github.add("0.2.3", manifest=raw, signature=signature)
            assert_result(check(github), "rejected", "signature_invalid")
    with MockGitHub() as github:                                              # the same signature on a higher version
        github.add("0.3.0", manifest=manifest_for("0.3.0"), signature=signature)
        assert_result(check(github), "rejected", "signature_invalid")


def test_a_damaged_signature_file_is_rejected():
    manifest = manifest_for()
    good = json.loads(S.sign_manifest(manifest, [PRIMARY]))
    entry = good["signatures"][0]
    flipped = base64.b64decode(entry["signature"])
    flipped = base64.b64encode(bytes([flipped[0] ^ 1]) + flipped[1:]).decode()
    short = base64.b64encode(b"x" * 63).decode()
    damaged = [b"garbage", b"[]", b"{}", b"null", "é".encode("utf-8"), json.dumps({**good, "format": 2}).encode(),
               json.dumps({"format": 1}).encode(), json.dumps({"format": 1, "signatures": []}).encode(),
               json.dumps({"format": 1, "signatures": [entry] * (A.MAX_SIGNATURES + 1)}).encode(),
               json.dumps({"format": 1, "signatures": ["text"]}).encode(),
               json.dumps({"format": 1, "signatures": [{**entry, "key_id": "XYZ"}]}).encode(),
               json.dumps({"format": 1, "signatures": [{**entry, "signature": "@@not base64@@"}]}).encode(),
               json.dumps({"format": 1, "signatures": [{**entry, "signature": short}]}).encode(),
               json.dumps({"format": 1, "signatures": [{**entry, "signature": flipped}]}).encode()]
    for raw in damaged:
        with MockGitHub() as github:
            github.add("0.2.3", manifest=manifest, signature=raw)
            assert_result(check(github), "rejected", "signature_invalid")


def test_a_release_without_a_signature_is_never_offered():
    with MockGitHub() as github:
        github.add("0.2.3", omit=(R.SIGNATURE_NAME,))
        assert_result(check(github), "up_to_date")
    with MockGitHub() as github:
        github.add("0.2.3", signature=b"")                                    # an empty file is not a signature
        assert_result(check(github), "up_to_date")


def test_an_unknown_signing_key_is_rejected():
    with MockGitHub() as github:
        github.add("0.2.3", keys=(STRANGER,))
        assert_result(check(github), "rejected", "untrusted_key")


def test_a_stranger_cannot_borrow_a_trusted_key_id():
    manifest = manifest_for()
    forged = json.loads(S.sign_manifest(manifest, [STRANGER]))
    forged["signatures"][0]["key_id"] = id_of(PRIMARY)
    with MockGitHub() as github:
        github.add("0.2.3", manifest=manifest, signature=json.dumps(forged).encode())
        assert_result(check(github), "rejected", "signature_invalid")


def test_with_no_trusted_keys_nothing_is_offered():
    for keys in ((), ("not hex",), ("ab" * 31,), (None,)):
        with MockGitHub() as github:
            github.add("0.2.3")
            assert_result(check(github, keys=keys), "rejected", "no_trusted_keys")


def test_the_default_trust_list_is_the_shipped_one():
    original = A.TRUSTED_PUBLIC_KEYS
    try:
        with MockGitHub() as github:
            github.add("0.2.3")
            A.TRUSTED_PUBLIC_KEYS = ()
            assert_result(C.check_for_update(installed="0.2.2", source=github.source), "rejected", "no_trusted_keys")
            A.TRUSTED_PUBLIC_KEYS = (hex_of(PRIMARY),)
            assert_offers(C.check_for_update(installed="0.2.2", source=github.source), "0.2.3")
    finally:
        A.TRUSTED_PUBLIC_KEYS = original


def test_every_shipped_public_key_is_well_formed():
    assert A.TRUSTED_PUBLIC_KEYS is trust.TRUSTED_PUBLIC_KEYS
    for text in trust.TRUSTED_PUBLIC_KEYS:
        assert len(text) == 64 and len(bytes.fromhex(text)) == 32


def test_without_the_verifier_nothing_is_offered():
    saved = sys.modules.get("Crypto.Signature")
    sys.modules["Crypto.Signature"] = None                                    # makes the import fail
    try:
        manifest = manifest_for()
        signature = b'{"format":1,"signatures":[]}'
        expect("verifier_missing", A.verify_signature, manifest, signature, [hex_of(PRIMARY)])
    finally:
        sys.modules["Crypto.Signature"] = saved


def test_the_signed_manifest_must_match_what_github_lists():
    changes = (("size", len(PACKAGE) + 1), ("digest", "sha256:" + sha(b"another file")))
    for field, value in changes:
        with MockGitHub() as github:
            entry = github.add("0.2.3")
            asset_named(entry, R.zip_name("0.2.3"))[field] = value
            assert_result(check(github), "rejected", "mismatch")
    release, manifest, signature = verified_parts()
    renamed = dataclasses.replace(release, package=dataclasses.replace(release.package, name="other.zip"))
    expect("mismatch", A.verify_release, renamed, manifest, signature, (0, 2, 2), [hex_of(PRIMARY)])


def test_github_digest_is_optional_but_compared_when_it_can_be():
    for digest in (None, "sha512:abc", "not a digest", 5):
        with MockGitHub() as github:
            entry = github.add("0.2.3")
            asset_named(entry, R.zip_name("0.2.3"))["digest"] = digest
            assert_offers(check(github), "0.2.3")
    with MockGitHub() as github:
        entry = github.add("0.2.3")
        del asset_named(entry, R.zip_name("0.2.3"))["digest"]
        assert_offers(check(github), "0.2.3")


def test_an_old_signed_manifest_on_a_new_tag_is_rejected():
    with MockGitHub() as github:
        github.add("0.3.0", manifest=manifest_for("0.2.3"))
        assert_result(check(github), "rejected", "mismatch")


def test_downgrades_are_rejected_even_with_a_valid_signature():
    release, manifest, signature = verified_parts("0.2.1")
    expect("downgrade", A.verify_release, release, manifest, signature, (0, 2, 2), [hex_of(PRIMARY)])
    release, manifest, signature = verified_parts("0.2.2")
    expect("downgrade", A.verify_release, release, manifest, signature, (0, 2, 2), [hex_of(PRIMARY)])
    release, manifest, signature = verified_parts("0.2.3")
    assert A.verify_release(release, manifest, signature, (0, 2, 2), [hex_of(PRIMARY)]).manifest.version == "0.2.3"


# ---- trust comes from the installed app, never from a release

PLANTED_ASSETS = ("trust.py", "trusted_keys.json", "update-public-key.pub", "update-public-key.json")


def attacker_release(github, version="0.3.0"):
    """A release the attacker controls completely: signed with their key, with that key placed everywhere a release can
    carry data (the manifest, the signature file, extra release files, the API fields and the notes)."""
    planted = hex_of(STRANGER)
    manifest = A._canonical({**json.loads(manifest_for(version)), "public_key": planted, "signing_key": planted,
                             "trusted_keys": [planted]})
    signature = json.loads(S.sign_manifest(manifest, [STRANGER]))
    signature.update(public_key=planted, public_keys=[planted], trusted_keys=[planted])
    signature["signatures"][0]["public_key"] = planted
    entry = github.add(version, manifest=manifest, signature=json.dumps(signature).encode("ascii"))
    entry.update(public_key=planted, trusted_keys=[planted], body=f"Trust this key: {planted}")
    for name in PLANTED_ASSETS:
        path = f"/dl/v{version}/{name}"
        github.files[path] = planted.encode("ascii")
        entry["assets"].append({"name": name, "size": 64, "state": "uploaded", "browser_download_url": github.url + path,
                                "url": github.url + path})
    return entry


def test_a_release_cannot_bring_its_own_public_key():
    before = A.TRUSTED_PUBLIC_KEYS
    with MockGitHub() as github:
        attacker_release(github)
        assert_result(check(github), "rejected", "untrusted_key")
        for name in PLANTED_ASSETS:                                           # the planted files are not even fetched
            assert not any(name in path for path in github.requests), github.requests
    assert A.TRUSTED_PUBLIC_KEYS is before


def test_an_empty_trust_list_never_trusts_the_first_key_it_sees():
    with MockGitHub() as github:
        attacker_release(github)
        assert_result(check(github, keys=()), "rejected", "no_trusted_keys")


def test_a_properly_signed_release_cannot_add_keys_while_the_app_runs():
    planted = hex_of(STRANGER)
    original = A.TRUSTED_PUBLIC_KEYS
    A.TRUSTED_PUBLIC_KEYS = (hex_of(PRIMARY),)
    try:
        with MockGitHub() as github:
            github.add("0.2.3", manifest=A._canonical({**json.loads(manifest_for()), "trusted_keys": [planted]}))
            result = C.check_for_update(installed="0.2.2", source=github.source)
            assert_offers(result, "0.2.3")
            assert {f.name for f in dataclasses.fields(result.update.manifest)} == {"version", "zip", "sha256", "size"}
            assert planted not in repr(result.update)
        assert A.TRUSTED_PUBLIC_KEYS == (hex_of(PRIMARY),)
        with MockGitHub() as github:
            github.add("0.2.4", keys=(STRANGER,))
            assert_result(C.check_for_update(installed="0.2.3", source=github.source), "rejected", "untrusted_key")
    finally:
        A.TRUSTED_PUBLIC_KEYS = original


def test_only_installed_keys_are_ever_loaded():
    from Crypto.Signature import eddsa

    loaded, real_import, real_id = [], eddsa.import_public_key, A.key_id

    def recording(raw):
        loaded.append(bytes(raw))
        return real_import(raw)

    eddsa.import_public_key = recording
    A.key_id = lambda public_key: "0" * 16                                    # even if the attacker's key id collides
    try:
        with MockGitHub() as github:
            attacker_release(github)
            assert_result(check(github), "rejected", "signature_invalid")
    finally:
        eddsa.import_public_key, A.key_id = real_import, real_id
    assert loaded == [bytes.fromhex(hex_of(PRIMARY))]


def test_the_trust_list_is_fixed_in_the_installed_app():
    package = ROOT / "src" / "fortnite_chest_counter"
    tree = ast.parse((package / "update" / "trust.py").read_text(encoding="utf-8"))
    code = [node for node in tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    assert len(code) == 1 and isinstance(code[0], ast.Assign) and code[0].targets[0].id == "TRUSTED_PUBLIC_KEYS"
    assert isinstance(ast.literal_eval(code[0].value), tuple)                 # a literal: nothing is read, fetched or computed
    for file in package.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if name == "TRUSTED_PUBLIC_KEYS" and file.name != "trust.py":
                assert isinstance(node.ctx, ast.Load), f"{file.name} changes the trust list"
            if isinstance(node, ast.Call):
                assert all(k.arg != "public_keys" for k in node.keywords), f"{file.name} passes its own keys"


# ---- changing signing keys

def test_a_release_signed_by_old_and_new_keys_reaches_every_copy():
    both = (PRIMARY, BACKUP)
    for trusted, signer in ((PRIMARY, PRIMARY), (BACKUP, BACKUP)):
        with MockGitHub() as github:
            github.add("0.2.3", keys=both)
            result = check(github, keys=(hex_of(trusted),))
            assert_offers(result, "0.2.3")
            assert result.update.signed_by == id_of(signer)


def test_a_copy_that_trusts_two_keys_accepts_either():
    trusted = (hex_of(PRIMARY), hex_of(BACKUP))
    for signer in (PRIMARY, BACKUP):
        with MockGitHub() as github:
            github.add("0.2.3", keys=(signer,))
            result = check(github, keys=trusted)
            assert_offers(result, "0.2.3")
            assert result.update.signed_by == id_of(signer)


def test_a_copy_that_does_not_know_the_new_key_refuses_it():
    with MockGitHub() as github:
        github.add("0.2.3", keys=(BACKUP,))
        assert_result(check(github, keys=(hex_of(PRIMARY),)), "rejected", "untrusted_key")


def test_a_retired_key_stops_working_once_it_is_removed():
    with MockGitHub() as github:
        github.add("0.2.3", keys=(PRIMARY,))
        assert_result(check(github, keys=(hex_of(BACKUP),)), "rejected", "untrusted_key")


def test_one_good_signature_from_a_trusted_key_is_enough():
    manifest = manifest_for()
    good = json.loads(S.sign_manifest(manifest, [PRIMARY]))["signatures"][0]
    stranger = json.loads(S.sign_manifest(manifest, [STRANGER]))["signatures"][0]
    broken = json.loads(S.sign_manifest(manifest_for("0.2.4"), [BACKUP]))["signatures"][0]     # a trusted key, wrong bytes
    with MockGitHub() as github:
        github.add("0.2.3", manifest=manifest, signature=json.dumps({"format": 1, "signatures": [stranger, broken, good]}).encode())
        result = check(github, keys=(hex_of(PRIMARY), hex_of(BACKUP)))
        assert_offers(result, "0.2.3")
        assert result.update.signed_by == id_of(PRIMARY)


# ---- the downloaded zip against the manifest

def test_the_downloaded_zip_must_match_the_manifest():
    manifest = A.parse_manifest(manifest_for())
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "download.zip"
        file.write_bytes(PACKAGE)
        A.verify_package(file, manifest)
        file.write_bytes(PACKAGE[:-1] + b"?")                                 # same size, different content
        expect("mismatch", A.verify_package, file, manifest)
        file.write_bytes(PACKAGE + b"x")
        expect("mismatch", A.verify_package, file, manifest)
        expect("package_unreadable", A.verify_package, Path(tmp) / "missing.zip", manifest)


# ---- the signing tool and the private keys

def test_the_signing_tool_writes_files_the_app_accepts():
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / R.zip_name("0.2.3")
        package.write_bytes(PACKAGE)
        manifest_path, signature_path = S.sign_package(package, "0.2.3", [PRIMARY, BACKUP])
        assert manifest_path.name == R.MANIFEST_NAME and signature_path.name == R.SIGNATURE_NAME
        assert manifest_path.read_bytes() == manifest_for()
        with MockGitHub() as github:
            github.add("0.2.3", manifest=manifest_path.read_bytes(), signature=signature_path.read_bytes())
            result = check(github)
            assert_offers(result, "0.2.3")
            A.verify_package(package, result.update.manifest)


def test_the_signing_tool_refuses_the_wrong_input():
    def refused(*args):
        try:
            S.sign_package(*args)
        except SystemExit:
            return True
        return False

    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / R.zip_name("0.2.3")
        package.write_bytes(PACKAGE)
        assert refused(package, "0.2.4", [PRIMARY]) and refused(package, "0.2.3-rc1", [PRIMARY]) and refused(package, "0.2.3", [])
        assert not (Path(tmp) / R.MANIFEST_NAME).exists()


def _exits(function, *args):
    try:
        function(*args)
    except SystemExit:
        return True
    return False


def test_key_files_are_encrypted_and_never_overwritten():
    passphrase = "correct horse battery staple"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signing.pem"
        key = S.create_key(path, passphrase)
        before = path.read_bytes()
        assert b"ENCRYPTED" in before and hex_of(key).encode() not in before
        assert _exits(S.create_key, path, passphrase) and path.read_bytes() == before
        assert hex_of(S.load_key(path, passphrase)) == hex_of(key)
        assert _exits(S.load_key, path, "this is the wrong passphrase")
        assert _exits(S.create_key, Path(tmp) / "weak.pem", "short") and not (Path(tmp) / "weak.pem").exists()


def test_private_keys_are_refused_inside_the_repository():
    target = ROOT / "build_out" / "must-not-exist-signing.pem"
    assert _exits(S.create_key, target, "correct horse battery staple") and not target.exists()
    assert _exits(S.load_key, ROOT / "pyproject.toml", "correct horse battery staple")


def test_no_private_key_is_in_the_repository():
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                            capture_output=True, check=True).stdout.split(b"\0")
    marker = b"PRIVATE" + b" KEY-----"
    checked = 0
    for name in filter(None, listed):
        file = ROOT / name.decode("utf-8")
        if file.is_file():
            assert marker not in file.read_bytes(), f"{file} contains a private key"
            checked += 1
    assert checked > 100


def test_the_release_build_ships_the_signature_verifier():
    assert "pycryptodome" in build_release.STREAM_PACKAGES


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:
            failed += 1
            import traceback
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} update checks passed")
    sys.exit(1 if failed else 0)
