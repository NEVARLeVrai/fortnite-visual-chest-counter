"""
The release gates (v0.2.3): a build without both production keys is refused, nothing unsigned or incomplete can be published, and the
test tool for real packages stops at the first failure. Small synthetic files only; every key here is generated for the run.
Run:   python tests/test_update_release.py   (or pytest)
"""

import sys
from pathlib import Path as _P

ROOT = _P(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))   # run without installing

import ast
import contextlib
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from Crypto.PublicKey import ECC

import build_release as B
import check_release as C
import sign_release as S
import test_update_install as T
import test_update_prepare as M2
import update_e2e as E2E
from fortnite_chest_counter.update import authenticity as A
from fortnite_chest_counter.update import install_helper as H
from fortnite_chest_counter.update import releases as R
from fortnite_chest_counter.update import trust
from fortnite_chest_counter.version import RELEASE_CHANNEL, __version__

VERSION = M2.VERSION
PRIMARY, RECOVERY, STRANGER = (ECC.generate(curve="ed25519") for _ in range(3))
KEYS = (S.public_key_hex(PRIMARY), S.public_key_hex(RECOVERY))
TRUST = C.TRUST_FILE
LISTING = f"{H.RUNTIME}/{H.PACKAGE_FILES}"


def release_files(keys=KEYS):
    files = M2.program_files(VERSION)
    for module in C.REQUIRED_MODULES:
        files.setdefault(module, b"# module\n")
    files[TRUST] = f"TRUSTED_PUBLIC_KEYS = (\n    {keys[0]!r},   # primary\n    {keys[1] if len(keys) > 1 else keys[0]!r},   # recovery\n)\n".encode() if len(keys) > 1 else f"TRUSTED_PUBLIC_KEYS = ({keys[0]!r},)\n".encode()
    files[C.archive.LAUNCHER] = b"MZ" + VERSION.encode("utf-16-le")
    files[C.PROVENANCE] = "".join(f"{name:<16}: {version} from PyPI; 9 files verified against the wheel RECORD hashes\n" for name, version in B.pinned_packages().items()).encode()
    return with_listing(files)


def with_listing(files):
    files = {k: v for k, v in files.items() if k != LISTING}
    names = sorted(k[len(H.RUNTIME) + 1:] for k in files if k.startswith(H.RUNTIME + "/"))
    files[LISTING] = json.dumps({"format": 1, "version": VERSION, "files": names}).encode()
    return files


@contextlib.contextmanager
def release(files=None, signer=PRIMARY):
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out"
        out.mkdir()
        notes = Path(tmp) / "docs"
        notes.mkdir()
        (notes / f"RELEASE_NOTES_v{VERSION}.md").write_text("What changed.", encoding="utf-8")
        inputs = Path(tmp) / "inputs"
        inputs.mkdir()
        program = M2.program_files(VERSION)
        with zipfile.ZipFile(inputs / "python-3.13.17-embed-amd64.zip", "w") as official:
            for name in ("python.exe", "pythonw.exe"):
                official.writestr(name, program[f"runtime/python/{name}"])
            official.writestr("python313._pth", b"python313.zip\r\n.\r\n")
        source = inputs / "ffmpeg-source-abc123.tar.gz"
        source.write_bytes(b"the FFmpeg source")
        package = M2.build_zip(files=release_files() if files is None else files)
        path = out / R.zip_name(VERSION)
        path.write_bytes(package)
        S.sign_package(path, VERSION, [signer])
        (out / C.SUMS_NAME).write_text(f"{hashlib.sha256(package).hexdigest()}  {path.name}\n{hashlib.sha256(b'the FFmpeg source').hexdigest()}  {source.name}\n", encoding="utf-8", newline="\n")
        yield SimpleNamespace(out=out, zip=path, notes=notes, inputs=inputs, source=source)


def found(r, version=VERSION, keys=KEYS):
    return C.problems(r.out, version, expected_keys=keys, notes_folder=r.notes, inputs=r.inputs)


def assert_problem(r, text, **options):
    result = found(r, **options)
    assert any(text in problem for problem in result), (text, result)


# ---- the build

def test_the_build_needs_both_production_keys():
    assert B.check_update_keys(KEYS) == []
    assert B.check_update_keys((*KEYS, S.public_key_hex(STRANGER))) == [], "a third key is fine while keys are being changed"
    for keys in ((), (KEYS[0],), (KEYS[0], KEYS[0]), (KEYS[0], KEYS[0].upper())):
        assert any("both production public keys" in p for p in B.check_update_keys(keys)), keys
    for bad in ("not hex", "ab" * 31, "ab" * 33, None, 5):
        assert B.check_update_keys((*KEYS, bad)), bad


def test_the_key_check_comes_before_any_build_work():
    source = (ROOT / "packaging" / "build_release.py").read_text(encoding="utf-8")
    body = source[source.index("def build(out,"):]
    assert body.index("check_update_keys(TRUSTED_PUBLIC_KEYS)") < body.index("raise SystemExit") < body.index("shutil.rmtree(app_root")


def test_the_real_build_function_refuses_without_keys():
    original = B.TRUSTED_PUBLIC_KEYS
    B.TRUSTED_PUBLIC_KEYS = ()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                B.build(Path(tmp) / "out", "a", "b", "c")
            except SystemExit as error:
                assert "could never accept an update" in str(error) and not (Path(tmp) / "out").exists()
            else:
                raise AssertionError("it built without keys")
    finally:
        B.TRUSTED_PUBLIC_KEYS = original


def test_a_prebuilt_launcher_must_be_for_this_version():
    with tempfile.TemporaryDirectory() as tmp:
        good, bad = Path(tmp) / "good.exe", Path(tmp) / "bad.exe"
        good.write_bytes(b"MZ" + __version__.encode("utf-16-le"))
        bad.write_bytes(b"MZ" + "9.9.9".encode("utf-16-le"))
        B.check_prebuilt_launcher(good)
        try:
            B.check_prebuilt_launcher(bad)
        except SystemExit:
            return
        raise AssertionError("a launcher for another version was accepted")


def test_a_wrong_prebuilt_launcher_stops_the_build_before_any_work():
    original = B.TRUSTED_PUBLIC_KEYS
    B.TRUSTED_PUBLIC_KEYS = KEYS
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.exe"
            bad.write_bytes(b"MZ" + "9.9.9".encode("utf-16-le"))
            try:
                B.build(Path(tmp) / "out", "a", "b", "c", prebuilt_launcher=bad)
            except SystemExit as error:
                assert "was not built for version" in str(error) and not (Path(tmp) / "out").exists()
            else:
                raise AssertionError("it used a launcher for another version")
    finally:
        B.TRUSTED_PUBLIC_KEYS = original


def test_preflight_reports_and_builds_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "not-created"
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = B.preflight(str(Path(tmp) / "python-3.13.17-embed-amd64.zip"), str(Path(tmp) / "x.zip"), str(Path(tmp) / "y.tar.gz"), None, out, prebuilt_launcher=str(Path(tmp) / "none.exe"))
        text = printed.getvalue()
        assert code == 1 and "MISSING  embeddable Python zip" in text and "MISSING  prebuilt launcher" in text and not out.exists()
        assert "Smart App Control" in text and "cannot build" in text


def test_the_build_requirements_cover_every_shipped_package():
    pinned = {line.split("==")[0].lower().replace("_", "-") for line in (ROOT / "packaging" / "build-requirements.txt").read_text(encoding="utf-8").splitlines() if "==" in line}
    wanted = {name.lower().replace("_", "-") for name in [*B.THIRD_PARTY, *B.STREAM_PACKAGES, *B.QT_FILES]}
    assert wanted <= pinned, wanted - pinned


def test_the_pinned_packages_are_the_ones_in_use():
    assert B.package_version_problems() == []


def test_a_package_of_another_version_stops_the_build():
    original = B.REQUIREMENTS
    with tempfile.TemporaryDirectory() as tmp:
        B.REQUIREMENTS = Path(tmp) / "requirements.txt"
        B.REQUIREMENTS.write_text("# a comment with == in it\nnumpy==0.0.1\nno-such-package-here==1.0\n", encoding="utf-8")
        original_keys = B.TRUSTED_PUBLIC_KEYS
        B.TRUSTED_PUBLIC_KEYS = KEYS
        try:
            wrong = B.package_version_problems()
            assert len(wrong) == 2 and "numpy is" in wrong[0] and "needs 0.0.1" in wrong[0] and "not installed" in wrong[1], wrong
            try:
                B.build(Path(tmp) / "out", "a", "b", "c")
            except SystemExit as error:
                assert "differ from" in str(error) and not (Path(tmp) / "out").exists()
            else:
                raise AssertionError("it built with the wrong packages")
        finally:
            B.REQUIREMENTS, B.TRUSTED_PUBLIC_KEYS = original, original_keys


def test_preflight_reports_the_wrong_package():
    original = B.REQUIREMENTS
    with tempfile.TemporaryDirectory() as tmp:
        B.REQUIREMENTS = Path(tmp) / "requirements.txt"
        B.REQUIREMENTS.write_text("numpy==0.0.1\n", encoding="utf-8")
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                B.preflight(None, None, None, None, Path(tmp) / "x", prebuilt_launcher=str(Path(tmp) / "none.exe"))
        finally:
            B.REQUIREMENTS = original
    assert "MISSING  Python packages match" in printed.getvalue() and "needs 0.0.1" in printed.getvalue()


# ---- the check before publishing

def test_a_complete_signed_release_passes():
    with release() as r:
        assert found(r) == []
        assets = C.verified_assets(r.out, VERSION, expected_keys=KEYS, notes_folder=r.notes, inputs=r.inputs)
        assert [a.name for a in assets] == [R.zip_name(VERSION), R.MANIFEST_NAME, R.SIGNATURE_NAME, C.SUMS_NAME, r.source.name]


def test_a_release_signed_with_the_recovery_key_passes():
    with release(signer=RECOVERY) as r:
        assert found(r) == []


def test_publishing_gets_no_files_unless_everything_passes():
    with release() as r:
        (r.out / R.SIGNATURE_NAME).unlink()
        try:
            C.verified_assets(r.out, VERSION, expected_keys=KEYS, notes_folder=r.notes, inputs=r.inputs)
        except SystemExit as error:
            assert "must not be published" in str(error)
        else:
            raise AssertionError("it listed files for an unsigned release")


def test_required_files_must_exist():
    for name, text in ((R.MANIFEST_NAME, "is missing"), (R.SIGNATURE_NAME, "is missing"), (R.zip_name(VERSION), "is missing"), (C.SUMS_NAME, "is missing")):
        with release() as r:
            (r.out / name).unlink()
            assert_problem(r, text)


def test_release_notes_are_required():
    with release() as r:
        (r.notes / f"RELEASE_NOTES_v{VERSION}.md").unlink()
        assert_problem(r, "RELEASE_NOTES")
    with release() as r:
        (r.notes / f"RELEASE_NOTES_v{VERSION}.md").write_text("  \n")
        assert_problem(r, "RELEASE_NOTES")


def test_a_signature_from_an_unknown_key_fails():
    with release(signer=STRANGER) as r:
        assert_problem(r, "untrusted_key")


def test_a_damaged_signature_fails():
    with release() as r:
        data = json.loads((r.out / R.SIGNATURE_NAME).read_bytes())
        data["signatures"][0]["signature"] = data["signatures"][0]["signature"][:-4] + "AAA="
        (r.out / R.SIGNATURE_NAME).write_text(json.dumps(data))
        assert_problem(r, "signature_invalid")


def test_a_zip_changed_after_signing_fails():
    with release() as r:
        data = r.zip.read_bytes()
        r.zip.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        assert_problem(r, "mismatch")


def test_a_wrong_checksum_file_fails():
    with release() as r:
        (r.out / C.SUMS_NAME).write_text(f"{'0' * 64}  {r.zip.name}\n")
        assert_problem(r, "does not match the zip")


def test_the_version_asked_for_must_be_the_one_signed():
    with release() as r:
        assert_problem(r, "is missing", version="0.2.4")


def test_the_ffmpeg_source_must_be_there_and_match():
    with release() as r:
        r.source.write_bytes(b"different source")
        assert_problem(r, "does not match the zip and the FFmpeg source")
    with release() as r:
        r.source.unlink()
        assert_problem(r, "exactly one FFmpeg source archive")
    with release() as r:
        (r.inputs / "ffmpeg-source-other.tar.gz").write_bytes(b"x")
        assert_problem(r, "exactly one FFmpeg source archive")


def test_the_launcher_must_be_for_this_version():
    with release(files={**release_files(), C.archive.LAUNCHER: b"MZ" + "9.9.9".encode("utf-16-le")}) as r:
        assert_problem(r, "launcher in the release was not built for this version")


def test_the_packages_inside_must_be_exactly_the_pinned_ones():
    record = release_files()[C.PROVENANCE].decode()
    first, version = next(iter(B.pinned_packages().items()))
    for changed in (record.replace(f": {version} from", ": 0.0.1 from", 1), record.split("\n", 1)[1], record + "extra-package   : 1.0 from PyPI\n"):
        with release(files={**release_files(), C.PROVENANCE: changed.encode()}) as r:
            assert_problem(r, "in the release is")
    with release(files=with_listing({k: v for k, v in release_files().items() if k != C.PROVENANCE})) as r:
        assert_problem(r, "does not contain runtime/PROVENANCE.txt")


def test_only_the_official_python_may_be_shipped():
    for change, text in (({"runtime/python/python.exe": b"someone else's python"}, "differs from the official Python"),
                         ({"runtime/python/python313._pth": b"python313.zip\n.\n..\\lib\n..\\evil\n"}, "changes the build does not make"),
                         ({"runtime/python/extra.dll": b"x"}, "is not part of the official Python")):
        with release(files=with_listing({**release_files(), **change})) as r:
            assert_problem(r, text)
    with release() as r:
        with zipfile.ZipFile(next(r.inputs.glob("python-*")), "a") as official:
            official.writestr("python313.zip", b"the standard library")
        assert_problem(r, "official Python file python313.zip is missing")
    with release() as r:
        next(r.inputs.glob("python-*")).unlink()
        assert_problem(r, "exactly one official Python zip")


def test_every_updater_module_is_on_the_required_list():
    source = {path.name for path in (ROOT / "src/fortnite_chest_counter/update").glob("*.py")}
    assert {"install_helper.py", "trust.py", "__init__.py"} <= source
    assert {f"{C.PACKAGE}/update/{name}" for name in source} | {f"{C.PACKAGE}/gui/update_manager.py"} == set(C.REQUIRED_MODULES)


def test_a_manifest_for_another_release_fails():
    with release() as r:
        manifest = A.build_manifest("9.9.9", R.zip_name("9.9.9"), A.file_sha256(r.zip), r.zip.stat().st_size)
        (r.out / R.MANIFEST_NAME).write_bytes(manifest)
        (r.out / R.SIGNATURE_NAME).write_bytes(S.sign_manifest(manifest, [PRIMARY]))
        assert_problem(r, "describes a different release")


def test_a_release_without_both_keys_inside_fails():
    with release(files=release_files(keys=(KEYS[0],))) as r:
        assert_problem(r, "both production public keys")


def test_a_release_with_different_keys_inside_fails():
    other = (S.public_key_hex(STRANGER), KEYS[1])
    with release(files=release_files(keys=other), signer=STRANGER) as r:
        assert_problem(r, "not the production keys")


def test_a_repository_without_keys_cannot_publish():
    with release() as r:
        assert_problem(r, "before release", keys=())


def test_an_invalid_key_inside_fails():
    bad = "ff" * 32
    with release(files=release_files(keys=(KEYS[0], bad))) as r:
        assert_problem(r, "not a valid public key", keys=(KEYS[0], bad))


def test_every_updater_module_must_be_inside():
    for module in C.REQUIRED_MODULES:
        files = with_listing({k: v for k, v in release_files().items() if k != module})
        with release(files=files) as r:
            assert_problem(r, "does not contain")


def test_the_file_list_must_match_the_package():
    with release(files={k: v for k, v in release_files().items() if k != LISTING}) as r:
        assert_problem(r, H.PACKAGE_FILES)
    files = release_files()
    files[LISTING] = json.dumps({"format": 1, "version": VERSION, "files": ["wrong.txt"]}).encode()
    with release(files=files) as r:
        assert_problem(r, "does not match the files")


def test_private_keys_are_refused_next_to_the_release():
    for name, content in (("signing.pem", b"anything"), ("backup.key", b"anything"), ("notes.txt", C.PRIVATE_KEY_MARKER)):
        with release() as r:
            (r.out / name).write_bytes(content)
            assert_problem(r, "private key material next to the release")


def test_private_keys_are_refused_inside_the_release():
    for name, content in (("runtime/lib/oops.pem", b"anything"), ("runtime/lib/oops.txt", b"-----BEGIN " + C.PRIVATE_KEY_MARKER)):
        files = with_listing({**release_files(), name: content})
        with release(files=files) as r:
            assert_problem(r, "contains private key material")


def test_files_that_are_public_by_design_are_allowed():
    """The real package ships a public certificate bundle and the crypto library's own test vectors; a real release must not be blocked."""
    files = with_listing({**release_files(), "runtime/lib/certifi/cacert.pem": b"public certificates",
                          "runtime/lib/Crypto/SelfTest/PublicKey/test_import_RSA.py": b"-----BEGIN " + C.PRIVATE_KEY_MARKER})
    with release(files=files) as r:
        assert found(r) == []


def test_the_public_exceptions_are_narrow():
    for name in ("runtime/lib/certifi/signing.pem", "runtime/lib/Crypto/signing.pem", "runtime/lib/certifi/cacert.pem.key"):
        files = with_listing({**release_files(), name: b"anything"})
        with release(files=files) as r:
            assert_problem(r, "contains private key material")


def test_an_unsafe_package_cannot_be_published():
    files = release_files()
    with release(files=files) as r:
        name = R.zip_name(VERSION)
        package = M2.build_zip(files=files, extra=[("../evil.txt", b"x")])
        (r.out / name).write_bytes(package)
        S.sign_package(r.out / name, VERSION, [PRIMARY])
        (r.out / C.SUMS_NAME).write_text(f"{hashlib.sha256(package).hexdigest()}  {name}\n")
        assert_problem(r, "archive_unsafe")


# ---- the production trust list and version

def rejection(manifest, signature, keys):
    try:
        A.verify_signature(manifest, signature, keys)
    except R.UpdateError as error:
        return error.kind
    raise AssertionError("it was accepted")


def a_manifest():
    return A.build_manifest(VERSION, R.zip_name(VERSION), "0" * 64, 1)


def test_the_production_list_holds_two_valid_labelled_keys():
    keys = trust.TRUSTED_PUBLIC_KEYS
    assert len(keys) == 2 and B.check_update_keys(keys) == [] and C._check_keys_are_usable(keys) == []
    assert len({A.key_id(bytes.fromhex(key)) for key in keys}) == 2
    lines = (ROOT / "src/fortnite_chest_counter/update/trust.py").read_text(encoding="utf-8").splitlines()
    assert "# primary" in next(line for line in lines if keys[0] in line) and "# recovery" in next(line for line in lines if keys[1] in line)


def test_a_signature_from_either_listed_key_is_accepted():
    """The real private keys never come near a test, so stand-ins take their place in a list of the same shape."""
    manifest = a_manifest()
    for signer, public in ((PRIMARY, KEYS[0]), (RECOVERY, KEYS[1])):
        assert A.verify_signature(manifest, S.sign_manifest(manifest, [signer]), KEYS) == A.key_id(bytes.fromhex(public))


def test_the_production_list_rejects_unknown_keys_missing_signatures_and_forgeries():
    manifest, keys = a_manifest(), trust.TRUSTED_PUBLIC_KEYS
    assert rejection(manifest, S.sign_manifest(manifest, [STRANGER]), keys) == "untrusted_key"
    assert rejection(manifest, b"", keys) == "signature_invalid"
    assert rejection(manifest, A.build_signature_file([]), keys) == "signature_invalid"
    for key in keys:
        forged = A.build_signature_file([(A.key_id(bytes.fromhex(key)), S.eddsa.new(STRANGER, "rfc8032").sign(manifest))])
        assert rejection(manifest, forged, keys) == "signature_invalid", "a signature claiming a production key id must be checked against that key"


def test_a_changed_manifest_is_rejected():
    manifest = a_manifest()
    assert rejection(manifest.replace(b'"size":1', b'"size":2'), S.sign_manifest(manifest, [PRIMARY]), KEYS) == "signature_invalid"


def test_the_version_is_consistent_and_stable():
    assert R.parse_version(__version__) is not None and RELEASE_CHANNEL == "stable"
    assert f"**v{__version__} stable**" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"v{__version__} stable" in (ROOT / "packaging/README.html").read_text(encoding="utf-8")


def test_the_release_notes_explain_the_fork_trust_bootstrap():
    notes = (ROOT / "docs" / "releases" / f"RELEASE_NOTES_v{__version__}.md").read_text(encoding="utf-8")
    assert notes.startswith(f"## Fortnite Visual Chest Counter v{__version__} — Stable") and R.zip_name(__version__) in notes
    assert "first installation from this fork is manual" in notes.lower() and "in-app updater" in notes.lower()
    assert "experimental" in notes.lower() and "signing keys" in notes.lower()
    for forbidden in ("antivirus", "defender", "smart app control", "smartscreen", "windows protected"):
        assert forbidden not in notes.lower(), forbidden


# ---- the signing tool

def test_keys_are_refused_in_cloud_synced_folders():
    with tempfile.TemporaryDirectory() as tmp:
        for folder in ("OneDrive", "OneDrive - Company", "Dropbox", "Google Drive"):
            path = Path(tmp) / folder / "signing.pem"
            try:
                S.create_key(path, "correct horse battery staple")
            except SystemExit as error:
                assert "cloud-synced" in str(error) and not path.exists()
            else:
                raise AssertionError(folder)


def test_a_new_key_can_create_its_folder():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "keys" / "deeper" / "signing.pem"
        S.create_key(path, "correct horse battery staple")
        assert path.exists()


def test_checking_a_key_prints_only_public_information():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "signing.pem"
        key = S.create_key(path, "correct horse battery staple")
        text = S.describe_key(S.load_key(path, "correct horse battery staple"), (S.public_key_hex(key),))
        assert S.public_key_hex(key) in text and "In this copy's trust list: yes" in text and b"KEY" not in text.encode().replace(b"Key", b"").replace(b"key", b"")
        assert "In this copy's trust list: no" in S.describe_key(key, ())
        assert path.read_text() not in text


def test_the_key_commands_work_and_print_only_public_information():
    phrase = "correct horse battery staple"
    original = S.getpass.getpass
    S.getpass.getpass = lambda prompt="": phrase
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signing.pem"
            created, checked = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(created):
                S._keygen(SimpleNamespace(out=str(path)))
            with contextlib.redirect_stdout(checked):
                S._check_key(SimpleNamespace(key=str(path)))
            stored = path.read_text(encoding="ascii")
            public = S.public_key_hex(S.load_key(path, phrase))
    finally:
        S.getpass.getpass = original
    assert "ENCRYPTED" in stored and phrase not in stored
    body = "".join(stored.split("\n")[1:-2])
    for text in (created.getvalue(), checked.getvalue()):
        assert public in text and phrase not in text and "-----" not in text and body not in text and "signing.pem" not in text
    assert "In this copy's trust list: no" in checked.getvalue()


def test_signing_never_reports_the_key_file_or_passphrase():
    phrase, prompts = "correct horse battery staple", []
    original = S.getpass.getpass
    S.getpass.getpass = lambda prompt="": prompts.append(prompt) or phrase
    try:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "keys" / "secret-name.pem"
            S.create_key(key, phrase)
            package = Path(tmp) / R.zip_name(VERSION)
            package.write_bytes(M2.build_zip(files=release_files()))
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                S._sign(SimpleNamespace(key=[str(key)], zip=str(package), version=VERSION))
            written = (Path(tmp) / R.MANIFEST_NAME).read_text() + (Path(tmp) / R.SIGNATURE_NAME).read_text()
            for refusal in (lambda: S.load_key(key, "a wrong passphrase here"), lambda: S.create_key(key, phrase)):
                try:
                    refusal()
                except SystemExit as error:
                    written += str(error)
                else:
                    raise AssertionError("it was not refused")
    finally:
        S.getpass.getpass = original
    everything = printed.getvalue() + "".join(prompts) + written
    assert prompts and "secret-name" not in everything and phrase not in everything


def test_mismatched_passphrases_create_nothing():
    answers = iter(["correct horse battery staple", "another horse battery staple"])
    original = S.getpass.getpass
    S.getpass.getpass = lambda prompt="": next(answers)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signing.pem"
            try:
                S._keygen(SimpleNamespace(out=str(path)))
            except SystemExit:
                assert not path.exists()
            else:
                raise AssertionError("it created a key")
    finally:
        S.getpass.getpass = original


# ---- the tool for real packages

def test_scenarios_must_pass_in_order():
    with tempfile.TemporaryDirectory() as tmp:
        kit = Path(tmp)
        assert E2E.earlier_scenario_not_passed(kit, "healthy") is None
        assert E2E.earlier_scenario_not_passed(kit, "failed-start") == "healthy"
        report = kit / "runs" / "healthy" / "report.txt"
        report.parent.mkdir(parents=True)
        report.write_text("FAIL  healthy\nbroken", encoding="utf-8")
        assert E2E.earlier_scenario_not_passed(kit, "failed-start") == "healthy", "a failure stops everything after it"
        report.write_text("PASS  healthy\ngood", encoding="utf-8")
        assert E2E.earlier_scenario_not_passed(kit, "failed-start") is None
        assert E2E.earlier_scenario_not_passed(kit, "crash-after-swap") == "failed-start"
    assert list(E2E.SCENARIOS)[:2] == ["healthy", "failed-start"]


class FakeSession:
    def __init__(self, kit, name):
        self.folder = kit / "runs" / name

    def close(self):
        pass

    def install_log(self):
        return ""

    def prepare_log(self):
        return ""


@contextlib.contextmanager
def scenario_runner(blocked):
    """The real run() with real reports, around scenarios that only record that they ran."""
    ran = []
    saved = E2E.HERE, E2E.Session, E2E.other_counters, E2E.security_during, dict(E2E.SCENARIOS)
    with tempfile.TemporaryDirectory() as tmp:
        kit = Path(tmp) / "fcc-kit"
        kit.mkdir()
        (kit / "kit.json").write_text("{}", encoding="utf-8")
        E2E.HERE, E2E.Session = kit, FakeSession
        E2E.other_counters = lambda kit, programs: []
        E2E.security_during = lambda kit, started: ("On", list(blocked))
        E2E.SCENARIOS.update({name: (lambda session, name=name: ran.append(name) or "fine") for name in E2E.SCENARIOS})
        try:
            yield SimpleNamespace(kit=kit, ran=ran)
        finally:
            E2E.HERE, E2E.Session, E2E.other_counters, E2E.security_during = saved[:4]
            E2E.SCENARIOS.clear()
            E2E.SCENARIOS.update(saved[4])


def quietly(function, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*args)


def test_the_runner_refuses_a_scenario_out_of_order():
    with scenario_runner(blocked=[]) as r:
        assert quietly(E2E.run, "failed-start") == 2 and r.ran == []
        assert quietly(E2E.run, "healthy") == 0
        assert (r.kit / "runs/healthy/report.txt").read_text(encoding="utf-8").startswith("PASS"), "the runner must save the report the order check reads"
        assert quietly(E2E.run, "failed-start") == 0 and r.ran == ["healthy", "failed-start"]


def test_a_windows_block_fails_the_scenario_and_stops_the_next():
    with scenario_runner(blocked=["C:/fcc-kit/x/python313.dll did not meet"]) as r:
        assert quietly(E2E.run, "healthy") == 1
        report = (r.kit / "runs/healthy/report.txt").read_text(encoding="utf-8")
        assert report.startswith("FAIL") and "python313.dll" in report and "Smart App Control: On" in report
        assert quietly(E2E.run, "failed-start") == 2 and r.ran == ["healthy"]


def test_windows_blocks_on_the_kit_are_recognised():
    output = ("STATE|On\nBLOCK|Code Integrity determined that a process (\\Device\\HarddiskVolume3\\fcc-kit\\runs\\healthy\\x\\.update\\helper\\python\\python313.dll) did not meet\n"
              "BLOCK|Code Integrity blocked \\Device\\HarddiskVolume3\\other\\thing.dll\n")
    state, blocked = E2E.summarize_security(output, Path("C:/fcc-kit"))
    assert state == "On" and len(blocked) == 1 and "helper" in blocked[0]
    assert E2E.summarize_security("", Path("C:/fcc-kit")) == ("unknown", [])
    assert E2E.summarize_security("STATE|Off\n", Path("C:/fcc-kit")) == ("Off", [])


def test_the_tool_never_changes_a_security_setting():
    source = (ROOT / "tests" / "update_e2e.py").read_text(encoding="utf-8")
    for forbidden in ("Set-MpPreference", "Add-MpPreference", "Set-ExecutionPolicy", "Unblock-File", "VerifiedAndReputablePolicyState"):
        assert forbidden not in source, forbidden


def test_the_obs_text_must_show_the_saved_counts():
    with tempfile.TemporaryDirectory() as tmp:
        session = SimpleNamespace(live=Path(tmp))
        path = Path(tmp) / "obs_output" / "chest_counter.txt"
        path.parent.mkdir()
        path.write_text("CERTAIN OPENED CHESTS: 0\nAPPROX. OPENED CHESTS: 0\n", encoding="utf-8")
        try:
            E2E.Session.assert_obs_text(session, seconds=0)
        except AssertionError:
            pass
        else:
            raise AssertionError("zeroed counts were accepted")
        path.write_text("CERTAIN OPENED CHESTS: 41\nAPPROX. OPENED CHESTS: 57\n", encoding="utf-8")
        E2E.Session.assert_obs_text(session, seconds=0)


def test_the_saved_data_in_the_scenarios_really_shows_those_counts():
    """The check above is only meaningful if the app turns the scenarios' saved file into these numbers."""
    from fortnite_chest_counter import counter, settings
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config.toml").write_bytes(E2E.USER_DATA["config.toml"])
        (Path(tmp) / "chest_state.json").write_bytes(E2E.USER_DATA["state/chest_state.json"])
        display = settings.load_config(Path(tmp) / "config.toml")[0]["display"]
        text = counter.overlay_text(display, counter.load_state(Path(tmp) / "chest_state.json"))
    assert "41" in text and "57" in text, text


def test_the_kit_gets_only_a_test_key():
    with tempfile.TemporaryDirectory() as tmp:
        base = T.tiny_build(Path(tmp) / "build")
        kit = Path(tmp) / "kit"
        with contextlib.redirect_stdout(io.StringIO()):
            E2E.make_kit(base, kit, max_path=400)
        trust = (kit / "base" / base.name / TRUST).read_text(encoding="utf-8")
        tree = ast.parse(trust)
        keys = ast.literal_eval(tree.body[-1].value)
        assert keys == (S.public_key_hex(E2E.ECC_import((kit / "test_key.pem").read_text())),)
        with zipfile.ZipFile(next((kit / "releases").rglob("*.zip"))) as z:
            assert all(not name.endswith(".pem") for name in z.namelist())


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}", flush=True)
        except Exception:
            failed += 1
            import traceback
            print(f"  FAIL  {name}", flush=True)
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} release gate checks passed")
    sys.exit(1 if failed else 0)
