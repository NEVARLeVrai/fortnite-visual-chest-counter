"""
Build the Windows release folder + zip.   (Developers only - end users never run this.)

    python packaging/build_release.py --python-embed python-3.13.17-embed-amd64.zip \
        --ffmpeg-zip ffmpeg-win64-lgpl.zip --ffmpeg-source ffmpeg-source-<commit>.tar.gz [--out build_out/vX] [--mingw C:\\MinGW\\bin]

Run it with the Python whose site-packages hold the exact numpy / scipy / PySide6-Essentials to ship (same minor version
as the embeddable runtime, e.g. a venv with `pip install numpy scipy PySide6-Essentials`). MinGW gcc + windres build the
launcher (on PATH, or --mingw).

What users get (v0.2.0):
  Fortnite Visual Chest Counter.exe   the ONLY thing to double-click. A ~70 KB launcher built here from
                                      packaging/launcher/launcher.c (reproducible: same source -> same bytes). It starts
                                      runtime\\python\\pythonw.exe -m fortnite_chest_counter.gui and exits.
  runtime/python/   the official python.org "embeddable" Python, UNMODIFIED except one added line in python313._pth
                    (every .exe/.dll/.pyd is Authenticode-signed by the Python Software Foundation / Microsoft; checked)
  runtime/lib/      our source code + numpy + scipy + the part of PySide6/shiboken6 the window needs. Every third-party file
                    is checked against its wheel's RECORD hash; the Qt files must also carry a valid signature (The Qt
                    Company); everything is precompiled
  runtime/ffmpeg/   unmodified LGPL FFmpeg build (its source archive is attached to the GitHub release)
User data (state/, logs/, config.toml) is never created by the build, so a release can never carry old counts.
runtime/PROVENANCE.txt records where every component came from, its hashes and which binaries are signed; SIGNATURES.txt
(next to the zip, not in it) lists every executable file and its signature - the input for code-signing decisions.
The zip is written with fixed timestamps and sorted entries, so the same inputs produce a byte-identical zip.
"""

import argparse
import base64
import compileall
import csv
import hashlib
import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from fortnite_chest_counter.version import APP_NAME, __version__     # noqa: E402
from fortnite_chest_counter.update.install_helper import PACKAGE_FILES   # noqa: E402
from fortnite_chest_counter.update.trust import TRUSTED_PUBLIC_KEYS      # noqa: E402

PACKAGE = "fortnite_chest_counter"
REQUIREMENTS = ROOT / "packaging" / "build-requirements.txt"
MIN_FREE_BYTES = 3 * 1024 ** 3
DEEPEST_PACKAGE_PATH = 110                                      # characters below the package folder, measured on v0.2.2 + a margin
MAX_WINDOWS_PATH = 259
TEMPLATE_FILES = ["chest/en/chest.npy", "chest/en/chest_small.npy", "chest/en/rare.npy", "chest/en/rare_small.npy",
                  "chest/en/sprite.npy", "chest/en/sprite_small.npy", "chest/pt/bau.npy", "chest/fr/coffre.npy", "chest/fr/esprit.npy", "chest/fr/normal_xp.npy", "chest/fr/rare_xp.npy",
                  "chest/fr/stack_exp.npy", "chest/fr/stack_rare.npy", "chest/fr/stack_x2.npy", "chest/fr/stack_x3.npy",
                  "chest/fr/stack_x4.npy", "chest/fr/stack_digit2_alt.npy", "chest/fr/stack_sprite.npy",
                  "container_opened.npy",
                  "ui/interaction_disc.npy", "ui/interaction_ring.npy"]                    # audio_fingerprint.npy is development-only
THIRD_PARTY = ["numpy", "scipy"]
# Livestream URL input (v0.2.1): Streamlink and its complete dependency closure on Windows / Python 3.13 (BSD / MIT /
# Apache-2.0, pycountry LGPL-2.1, certifi MPL-2.0). Verified against the wheel RECORD hashes like numpy; their licence
# files go to runtime/licenses/. Binaries among them (lxml, pycryptodome, cffi, charset-normalizer) are not Authenticode-signed.
STREAM_PACKAGES = ["streamlink", "attrs", "certifi", "cffi", "charset-normalizer", "h11", "idna", "isodate", "lxml",
                   "outcome", "pycountry", "pycparser", "pycryptodome", "PySocks", "requests", "sniffio", "sortedcontainers",
                   "trio", "trio-websocket", "urllib3", "websocket-client", "wsproto"]
DISCORD_PACKAGES = ["discord.py", "aiohappyeyeballs", "aiohttp", "aiosignal", "audioop-lts", "frozenlist",
                    "multidict", "propcache", "yarl"]
# The window toolkit: only what QtCore + QtGui + QtWidgets + the Windows platform plugin load (their PE import closure;
# the C/C++ runtime vcruntime140*.dll comes from runtime\python, which ships a newer one than PySide6).
QT_FILES = {
    "PySide6_Essentials": ["PySide6/__init__.py", "PySide6/_config.py", "PySide6/_git_pyside_version.py",
                           "PySide6/QtCore.pyd", "PySide6/QtGui.pyd", "PySide6/QtWidgets.pyd", "PySide6/pyside6.abi3.dll",
                           "PySide6/Qt6Core.dll", "PySide6/Qt6Gui.dll", "PySide6/Qt6Widgets.dll",
                           "PySide6/msvcp140.dll", "PySide6/msvcp140_1.dll", "PySide6/msvcp140_2.dll",
                           "PySide6/plugins/platforms/qwindows.dll"],
    "shiboken6": ["shiboken6/__init__.py", "shiboken6/_config.py", "shiboken6/_git_shiboken_module_version.py",
                  "shiboken6/Shiboken.pyd", "shiboken6/shiboken6.abi3.dll"],
}
QT_SIGNERS = ("The QT Company Oy", "Microsoft Windows")     # Qt files; msvcp140.dll is Microsoft's own C++ runtime
ZIP_TIME = (2026, 1, 1, 0, 0, 0)                                # fixed timestamps -> reproducible zip
LAUNCHER_DIR = ROOT / "packaging" / "launcher"
EXE_NAME = f"{APP_NAME}.exe"
PE_SUFFIXES = (".exe", ".dll", ".pyd")

RC_TEMPLATE = """#include <windows.h>
1 ICON "app.ico"
1 24 "launcher.manifest"
1 VERSIONINFO
FILEVERSION {major},{minor},{patch},0
PRODUCTVERSION {major},{minor},{patch},0
FILEFLAGSMASK 0x3f
FILEFLAGS 0x0
FILEOS 0x40004
FILETYPE 0x1
FILESUBTYPE 0x0
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904B0"
    BEGIN
      VALUE "CompanyName", "Gabriel Gomez"
      VALUE "FileDescription", "{app}"
      VALUE "FileVersion", "{version}"
      VALUE "InternalName", "launcher"
      VALUE "LegalCopyright", "Copyright (c) 2026 Gabriel Gomez. MIT License."
      VALUE "OriginalFilename", "{exe}"
      VALUE "ProductName", "{app}"
      VALUE "ProductVersion", "{version}"
      VALUE "Comments", "Open source: https://github.com/NEVARLeVrai/fortnite-visual-chest-counter - not affiliated with Epic Games."
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
"""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_update_keys(keys):
    """What is wrong with the update keys this build would ship. -> [problems]. A copy installed without them could never accept an
    update, and installed copies can only ever trust what they shipped with. Two keys: the primary and the offline recovery key."""
    problems, good = [], set()
    for key in keys:
        if isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key):
            good.add(key.lower())
        else:
            problems.append(f"not a 32-byte public key: {str(key)[:24]}")
    if len(good) < 2:
        problems.append(f"update/trust.py must hold both production public keys (primary and recovery); it has {len(good)}")
    return problems


def pinned_packages():
    """{package: version}: what a release is built from."""
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return dict(line.strip().split("==") for line in lines if "==" in line and not line.startswith("#"))


def package_version_problems():
    """Every Python package that differs from the versions releases are built from. -> [problems]."""
    wrong = []
    for name, wanted in pinned_packages().items():
        try:
            have = md.version(name)
        except md.PackageNotFoundError:
            have = "not installed"
        if have != wanted:
            wrong.append(f"{name} is {have}, needs {wanted}")
    return wrong


def check_prebuilt_launcher(path):
    """A launcher compiled elsewhere must be for this exact version."""
    if __version__.encode("utf-16-le") not in Path(path).read_bytes():
        raise SystemExit(f"{path} was not built for version {__version__}.")


def preflight(python_embed, ffmpeg_zip, ffmpeg_source, mingw, out, prebuilt_launcher=None):
    """Can this PC build a release? Builds nothing and changes nothing. -> 0 if everything needed is here."""
    rows = []

    def row(name, ok, detail=""):
        rows.append(ok)
        print(f"  {'ok     ' if ok else 'MISSING'}  {name}{f'   ({detail})' if detail else ''}")

    embed_version = re.search(r"python-(\d+)\.(\d+)\.", Path(python_embed or "").name)
    here = f"{sys.version_info.major}.{sys.version_info.minor}"
    row("Python version matches the embeddable runtime", bool(embed_version) and f"{embed_version[1]}.{embed_version[2]}" == here, f"running {sys.version.split()[0]}")
    for name, path in (("embeddable Python zip", python_embed), ("FFmpeg zip", ffmpeg_zip), ("FFmpeg source archive", ffmpeg_source)):
        row(name, bool(path) and Path(path).is_file(), str(path))
    wrong = package_version_problems()
    row("Python packages match build-requirements.txt", not wrong, "; ".join(wrong))
    if prebuilt_launcher:
        row("prebuilt launcher", Path(prebuilt_launcher).is_file(), str(prebuilt_launcher))
    else:
        env_path = (str(mingw) + os.pathsep if mingw else "") + os.environ.get("PATH", "")
        for tool in ("gcc", "windres"):
            found = shutil.which(tool, path=env_path)
            try:
                works = bool(found) and subprocess.run([found, "--version"], capture_output=True, timeout=30).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                works = False                  # Smart App Control refuses unsigned programs here: use --prebuilt-launcher
            row(f"{tool} runs on this PC", works, "if it is blocked, use --prebuilt-launcher" if not works else "")
    try:
        status = authenticode([Path(sys.base_prefix) / "python.exe"])
        row("Windows signature checks work", any(s == "Valid" for s, _ in status.values()))
    except (OSError, subprocess.SubprocessError):
        row("Windows signature checks work", False)
    keys = check_update_keys(TRUSTED_PUBLIC_KEYS)
    row("both production public keys are in update/trust.py", not keys, "; ".join(keys))
    anchor = next(p for p in (Path(out).resolve(), *Path(out).resolve().parents) if p.exists())
    row("at least 3 GB free where the build goes", shutil.disk_usage(anchor).free >= MIN_FREE_BYTES)
    length = len(str(Path(out).resolve())) + 1 + len(f"Fortnite-Visual-Chest-Counter-v{__version__}") + 1 + DEEPEST_PACKAGE_PATH
    row("the build folder path is short enough", length <= MAX_WINDOWS_PATH, f"{length} of {MAX_WINDOWS_PATH} characters")
    try:
        state = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-MpComputerStatus).SmartAppControlState"], capture_output=True, text=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        state = ""
    print(f"  info     Smart App Control: {state or 'unknown'}")
    print("This PC can build the release." if all(rows) else "This PC cannot build the release yet: see MISSING above.")
    return 0 if all(rows) else 1


def authenticode(paths):
    """{path: (status, signer)} via PowerShell Get-AuthenticodeSignature (reads signatures; runs nothing)."""
    script = ("$ErrorActionPreference='Stop'; foreach ($p in $input) { $s = Get-AuthenticodeSignature -LiteralPath $p; "
              "'{0}|{1}|{2}' -f $p, $s.Status, ($s.SignerCertificate.Subject -replace '^CN=([^,]+).*','$1') }")
    shell = shutil.which("pwsh") or "powershell"
    out = subprocess.run([shell, "-NoProfile", "-Command", script], input="\n".join(map(str, paths)),
                         capture_output=True, text=True, check=True).stdout
    return {Path(p): (status, signer) for p, status, signer in (l.split("|", 2) for l in out.splitlines() if l.count("|") == 2)}


def verify_record(dist):
    """Every file of an installed distribution must still match its wheel's RECORD hash. -> (dist, base, files checked)."""
    d = md.distribution(dist)
    base = Path(d.locate_file(""))
    checked = 0
    for row in csv.reader(d.read_text("RECORD").splitlines()):
        if len(row) < 2 or not row[1].startswith("sha256="):
            continue
        f = base / row[0]
        want = row[1].split("=", 1)[1]
        got = base64.urlsafe_b64encode(hashlib.sha256(f.read_bytes()).digest()).rstrip(b"=").decode()
        if got != want:
            raise SystemExit(f"{dist}: {row[0]} does not match its wheel RECORD - refusing to ship it")
        checked += 1
    return d, base, checked


def apply_launcher_icon(target, icon):
    """Replace the known launcher icon resources with the current bundled artwork."""
    import ctypes
    from ctypes import wintypes
    import struct

    data = Path(icon).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    if reserved or kind != 1 or not count:
        raise ValueError("The launcher icon is not an ICO file.")
    directory = bytearray(struct.pack("<HHH", 0, 1, count))
    images = []
    for index in range(count):
        width, height, colors, reserved, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        image = data[offset:offset + size]
        if not size or len(image) != size:
            raise ValueError("The launcher icon contains an invalid image.")
        images.append(image)
        directory.extend(struct.pack("<BBBBHHIH", width, height, colors, reserved, planes, bits, size, index + 1))

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD)
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = (wintypes.HMODULE,)
    kernel.BeginUpdateResourceW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL)
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.UpdateResourceW.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                       wintypes.WORD, ctypes.c_void_p, wintypes.DWORD)
    kernel.UpdateResourceW.restype = wintypes.BOOL
    kernel.EndUpdateResourceW.argtypes = (wintypes.HANDLE, wintypes.BOOL)
    kernel.EndUpdateResourceW.restype = wintypes.BOOL
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.WORD, ctypes.c_ssize_t)
    kernel.EnumResourceLanguagesW.argtypes = (wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p,
                                              callback_type, ctypes.c_ssize_t)
    kernel.EnumResourceLanguagesW.restype = wintypes.BOOL
    languages = []
    callback = callback_type(lambda module, kind, name, language, parameter: languages.append(language) or True)
    module = kernel.LoadLibraryExW(str(Path(target).resolve()), None, 2)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.EnumResourceLanguagesW(module, 14, 1, callback, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.FreeLibrary(module)
    handle = kernel.BeginUpdateResourceW(str(Path(target).resolve()), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for language in languages:
            for index, image in enumerate(images, 1):
                buffer = ctypes.create_string_buffer(image)
                if not kernel.UpdateResourceW(handle, 3, index, language, buffer, len(image)):
                    raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(bytes(directory))
            if not kernel.UpdateResourceW(handle, 14, 1, language, buffer, len(directory)):
                raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        kernel.EndUpdateResourceW(handle, True)
        raise
    if not kernel.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def build_launcher(target, work, mingw=None, *, development=False):
    """Compile packaging/launcher (C + icon + version info + manifest) into `target`. Reproducible: no timestamps."""
    env = dict(os.environ)
    if mingw:
        env["PATH"] = str(mingw) + os.pathsep + env.get("PATH", "")
    tools = {t: shutil.which(t, path=env["PATH"]) for t in ("gcc", "windres")}
    missing = [t for t, p in tools.items() if not p]
    if missing:
        raise SystemExit(f"{', '.join(missing)} not found: install MinGW (gcc + binutils) or pass --mingw <its bin folder>")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for name in ("app.ico", "launcher.manifest"):
        shutil.copy2(LAUNCHER_DIR / name, work / name)
    major, minor, patch = (int(x) for x in (re.findall(r"\d+", __version__) + ["0", "0"])[:3])
    (work / "launcher.rc").write_text(RC_TEMPLATE.format(major=major, minor=minor, patch=patch, version=__version__,
                                                         app=APP_NAME, exe=EXE_NAME), encoding="ascii")
    subprocess.run([tools["windres"], "-i", "launcher.rc", "-O", "coff", "-o", "launcher.res"], cwd=work, env=env, check=True)
    defines = ["-DFCC_DEV_LAUNCHER"] if development else []
    subprocess.run([tools["gcc"], "-O2", "-s", "-mwindows", *defines, "-o", "launcher.exe",
                    str(LAUNCHER_DIR / "launcher.c"), "launcher.res", "-Wl,--no-insert-timestamp"],
                   cwd=work, env=env, check=True)
    shutil.copy2(work / "launcher.exe", target)
    version = subprocess.run([tools["gcc"], "--version"], capture_output=True, text=True, env=env).stdout.splitlines()[0]
    return version


def signature_report(app_root):
    """-> (summary lines for PROVENANCE, full listing) of every executable file in the package."""
    files = sorted(f for f in app_root.rglob("*") if f.suffix.lower() in PE_SUFFIXES)
    sigs = authenticode(files)
    by_signer, unsigned = {}, []
    listing = []
    for f in files:
        status, signer = sigs.get(f, ("?", ""))
        rel = f.relative_to(app_root).as_posix()
        listing.append(f"{'signed  ' if status == 'Valid' else 'UNSIGNED'}  {signer if status == 'Valid' else '-':<55s} {rel}")
        if status == "Valid":
            by_signer[signer] = by_signer.get(signer, 0) + 1
        else:
            unsigned.append(rel)
    groups = {}
    for rel in unsigned:
        parts = rel.split("/")
        key = rel if len(parts) <= 3 else "/".join(parts[:3]) + "/..."
        groups[key] = groups.get(key, 0) + 1
    summary = [f"Windows code signatures: {len(files)} executable files (.exe/.dll/.pyd)",
               *(f"  signed by {s}: {n}" for s, n in sorted(by_signer.items())),
               f"  not signed: {len(unsigned)}",
               *(f"      {n:4d}  {k}" for k, n in sorted(groups.items()))]
    return summary, listing


def write_package_files(runtime):
    """runtime/package-files.json: everything this release puts in runtime/. The updater uses it to notice anything else in there."""
    files = sorted(p.relative_to(runtime).as_posix() for p in runtime.rglob("*")
                   if p.is_file() and p != runtime / PACKAGE_FILES)
    (runtime / PACKAGE_FILES).write_text(json.dumps({"format": 1, "version": __version__, "files": files}, indent=0) + "\n",
                                         encoding="utf-8", newline="\n")


def audit(app_root):
    """Fail the build if development leftovers, old counts or unexpected programs could reach users."""
    problems = []
    bad_names = {"samples", "analysis_output", ".venv", "chest_count.txt", "chest_count_save.txt",
                 "audio_fingerprint.npy", "chest_state.json", "config.toml"}
    third_party = [app_root / "runtime" / "lib" / d for d in ("numpy", "numpy.libs", "scipy", "scipy.libs")]
    for f in app_root.rglob("*"):
        if any(t in f.parents for t in third_party):
            continue                     # verified against the wheels' RECORD (scipy legitimately ships a .npz table)
        if f.name in bad_names or f.suffix in (".mp4", ".wav", ".npz", ".jsonl"):
            problems.append(str(f.relative_to(app_root)))
    if any(f.suffix == ".json" for f in (app_root / "state").iterdir()):
        problems.append("state/ contains a saved counter")
    for name, expected in (("certain_chests.txt", "CERTAIN OPENED CHESTS: 0"), ("approximate_chests.txt", "APPROX. OPENED CHESTS: 0")):
        if (app_root / "obs_output" / name).read_text(encoding="utf-8").strip() != expected:
            problems.append(f"obs_output/{name} is not zero")
    exes = [f.relative_to(app_root) for f in app_root.rglob("*.exe")]
    allowed = {Path(EXE_NAME), Path("runtime/python/python.exe"), Path("runtime/python/pythonw.exe"),
               Path("runtime/ffmpeg/ffmpeg.exe")}
    if set(exes) - allowed:
        problems.append(f"unexpected executables: {sorted(map(str, set(exes) - allowed))}")
    scripts = [f.name for f in app_root.rglob("*") if f.suffix.lower() in (".bat", ".cmd", ".ps1", ".vbs", ".lnk")]
    if scripts:
        problems.append(f"script launchers are not shipped any more (v0.2.0 = one app): {scripts}")
    if problems:
        raise SystemExit("RELEASE AUDIT FAILED:\n  " + "\n  ".join(problems))
    print("Release audit passed: no samples, logs, saved counts, scripts or unexpected executables; OBS files start at 0.")


def build(out, python_embed, ffmpeg_zip, ffmpeg_source, mingw=None, prebuilt_launcher=None, replay_reader=None):
    key_problems = check_update_keys(TRUSTED_PUBLIC_KEYS)
    if key_problems:
        raise SystemExit("Refusing to build a release that could never accept an update:\n  " + "\n  ".join(key_problems))
    wrong_packages = package_version_problems()
    if wrong_packages:
        raise SystemExit("Refusing to build with packages that differ from packaging/build-requirements.txt:\n  " + "\n  ".join(wrong_packages))
    if prebuilt_launcher:
        check_prebuilt_launcher(prebuilt_launcher)
    app_root = out / f"Fortnite-Visual-Chest-Counter-v{__version__}"
    shutil.rmtree(app_root, ignore_errors=True)
    if app_root.exists():
        raise SystemExit(f"cannot clear {app_root} (a file in it is locked)")
    out.mkdir(parents=True, exist_ok=True)
    runtime = app_root / "runtime"
    provenance = [f"Fortnite Visual Chest Counter v{__version__} - provenance of the bundled components", ""]

    # ---- 1. official embeddable Python, unmodified (one line added to the ._pth so it finds runtime\lib)
    py = runtime / "python"
    with zipfile.ZipFile(python_embed) as z:
        z.extractall(py)
    pth = next(py.glob("python3*._pth"))
    ver = pth.stem.replace("python", "")
    build_ver = f"{sys.version_info.major}{sys.version_info.minor}"
    if ver != build_ver:
        raise SystemExit(f"embeddable Python is {ver} but numpy/scipy come from Python {build_ver}")
    pth.write_text(pth.read_text(encoding="utf-8").rstrip("\n") + "\n..\\lib\n", encoding="utf-8")
    binaries = sorted(p for p in py.iterdir() if p.suffix in PE_SUFFIXES)
    sigs = authenticode(binaries)
    unsigned = [p.name for p in binaries if sigs.get(p, ("?",))[0] != "Valid"]
    if unsigned:
        raise SystemExit(f"embeddable Python files without a valid signature: {unsigned}")
    signers = sorted({s for _, s in sigs.values()})
    provenance += [f"Python runtime  : {Path(python_embed).name} from https://www.python.org/ftp/python/",
                   f"                  archive SHA-256 {sha256(python_embed)}",
                   f"                  {len(binaries)} binaries, all with a valid Authenticode signature by: {', '.join(signers)}",
                   f"                  only change: '..\\lib' appended to {pth.name}", ""]

    # ---- 2. our code + numpy/scipy + the window toolkit, verified against their wheels, precompiled
    lib = runtime / "lib"
    lib.mkdir()
    shutil.copytree(ROOT / "src" / PACKAGE, lib / PACKAGE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "templates"))
    for rel in TEMPLATE_FILES:
        dst = lib / PACKAGE / "templates" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "src" / PACKAGE / "templates" / rel, dst)
    for dist in THIRD_PARTY + STREAM_PACKAGES + DISCORD_PACKAGES:
        d, base, checked = verify_record(dist)
        # the package's own folders only (RECORD also lists helper scripts outside site-packages, e.g. ../../Scripts/f2py.exe)
        tops = sorted({Path(f).parts[0] for f in d.files if not str(f).endswith(".pyc") and Path(f).parts[0] != ".."})
        for top in tops:
            src = base / top
            if src.is_dir():
                shutil.copytree(src, lib / top, ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc"))
            else:
                shutil.copy2(src, lib / top)
        provenance.append(f"{dist:<16}: {d.version} from PyPI; {checked} files verified against the wheel RECORD hashes")
    qt_binaries = []
    for dist, files in QT_FILES.items():
        d, base, checked = verify_record(dist)
        for rel in files:
            dst = lib / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / rel, dst)
            if dst.suffix in PE_SUFFIXES:
                qt_binaries.append(dst)
        provenance.append(f"{dist:<16}: {d.version} from PyPI; {checked} files verified against the wheel RECORD hashes; "
                          f"{len(files)} shipped (QtCore/QtGui/QtWidgets + Windows platform plugin)")
    sigs = authenticode(qt_binaries)
    unsigned = [p.name for p in qt_binaries if sigs.get(p, ("?", ""))[0] != "Valid" or sigs[p][1] not in QT_SIGNERS]
    if unsigned:
        raise SystemExit(f"Qt files without a valid signature by {' or '.join(QT_SIGNERS)}: {unsigned}")
    provenance.append(f"                  all {len(qt_binaries)} Qt binaries carry a valid Authenticode signature "
                      f"({', '.join(sorted({sigs[p][1] for p in qt_binaries}))})")
    # stripdir/prependdir: the .pyc files record "lib\..." instead of this build machine's folder (reproducible, no local paths)
    compileall.compile_dir(str(lib), quiet=1, stripdir=str(lib), prependdir="lib",
                           invalidation_mode=compileall.py_compile.PycInvalidationMode.UNCHECKED_HASH)
    provenance.append(f"our code        : {PACKAGE} v{__version__}, plain Python source (open source, MIT)")
    provenance.append("")

    # ---- 3. bundled ffmpeg, identified without running it
    ff_dir = runtime / "ffmpeg"
    ff_dir.mkdir()
    with zipfile.ZipFile(ffmpeg_zip) as z:
        for info in z.infolist():
            base = info.filename.rsplit("/", 1)[-1]
            if base == "ffmpeg.exe":
                (ff_dir / "ffmpeg.exe").write_bytes(z.read(info))
            elif base in ("LICENSE.txt", "LICENSE") and info.filename.count("/") <= 1:
                (ff_dir / base).write_bytes(z.read(info))
    exe_bytes = (ff_dir / "ffmpeg.exe").read_bytes()
    found = re.search(rb"N-\d+-g([0-9a-f]{7,40})-\d{8}", exe_bytes)
    if not found:
        raise SystemExit("cannot identify the FFmpeg build")
    build_id, short = found.group(0).decode(), found.group(1).decode()
    with tarfile.open(ffmpeg_source) as tar:
        top = tar.next().name.split("/")[0]
    if not top.startswith(f"FFmpeg-{short}"):
        raise SystemExit(f"{ffmpeg_source} is the source of {top}, not of the bundled build {build_id}")
    commit = top.split("-", 1)[1]
    notice = (ROOT / "packaging" / "FFMPEG-NOTICE.txt").read_text(encoding="utf-8").replace("{commit}", commit).replace("{short}", short)
    (ff_dir / "FFMPEG-NOTICE.txt").write_text(notice + f"\nBundled build          : {build_id}\n"
                                              f"ffmpeg.exe SHA-256     : {hashlib.sha256(exe_bytes).hexdigest()}\n", encoding="utf-8")
    provenance += [f"FFmpeg          : {build_id} (LGPL, https://github.com/BtbN/FFmpeg-Builds), source commit {commit}",
                   f"                  ffmpeg.exe SHA-256 {hashlib.sha256(exe_bytes).hexdigest()}", ""]

    # ---- 4. licence texts
    lic = runtime / "licenses"
    lic.mkdir()
    shutil.copy2(py / "LICENSE.txt", lic / "Python-LICENSE.txt")
    for dist, name in (("numpy", "NumPy-LICENSE.txt"), ("scipy", "SciPy-LICENSE.txt")):
        d = md.distribution(dist)
        files = [f for f in d.files if re.search(r"dist-info/(licenses/)?LICENSE(\.txt)?$", str(f))]
        if not files:
            raise SystemExit(f"missing licence text for {dist}")
        shutil.copy2(d.locate_file(files[0]), lic / name)
    for dist in STREAM_PACKAGES + DISCORD_PACKAGES:
        d = md.distribution(dist)
        files = [f for f in d.files if re.search(r"dist-info/(licenses/.+|LICEN[CS]E[^/]*|COPYING[^/]*|NOTICE[^/]*)$", str(f))]
        if not files:
            raise SystemExit(f"missing licence text for {dist}")
        for f in files:
            shutil.copy2(d.locate_file(f), lic / f"{d.metadata['Name']}-{Path(str(f)).name}")
    qt_version = md.version("PySide6_Essentials")
    (lic / "Qt-NOTICE.txt").write_text((ROOT / "packaging" / "QT-NOTICE.txt").read_text(encoding="utf-8")
                                       .replace("{version}", qt_version), encoding="utf-8")
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        shutil.copy2(ROOT / "packaging" / "licenses" / name, lic / name)

    # ---- 5. the launcher + user-facing files
    if prebuilt_launcher:
        shutil.copy2(prebuilt_launcher, app_root / EXE_NAME)
        apply_launcher_icon(app_root / EXE_NAME, LAUNCHER_DIR / "app.ico")
        gcc = "a prebuilt launcher with the current app icon resources"
    else:
        gcc = build_launcher(app_root / EXE_NAME, out / "_launcher-build", mingw)
    provenance += [f"launcher        : {EXE_NAME} built from packaging/launcher/launcher.c with {gcc}",
                   f"                  SHA-256 {sha256(app_root / EXE_NAME)} (reproducible: no timestamps)", ""]
    shutil.copy2(ROOT / "packaging" / "README.html", app_root / "README.html")
    shutil.copy2(ROOT / "config.example.toml", app_root / "config.example.toml")
    shutil.copy2(ROOT / "packaging" / "LICENSE-NOTICE.txt", app_root / "LICENSE-NOTICE.txt")
    shutil.copy2(ROOT / "LICENSE", app_root / "LICENSE.txt")
    for d, text in (("state", "Your counter lives here. Keep this folder when updating.\n"),
                    ("logs", "Session logs. Send logs/diagnostics.txt (and any crash_*.txt) when you ask for help.\n")):
        (app_root / d).mkdir()
        (app_root / d / "README.txt").write_text(text, encoding="utf-8")
    obs = app_root / "obs_output"
    obs.mkdir()
    (obs / "certain_chests.txt").write_text("CERTAIN OPENED CHESTS: 0\n", encoding="utf-8")
    (obs / "approximate_chests.txt").write_text("APPROX. OPENED CHESTS: 0\n", encoding="utf-8")
    (obs / "chest_status.txt").write_text("0 certain • ~0 estimated\n", encoding="utf-8")
    (obs / "chest_counter.txt").write_text("CERTAIN OPENED CHESTS: 0\nAPPROX. OPENED CHESTS: 0\n", encoding="utf-8")
    (obs / "last_chest.txt").write_text("NO CHESTS YET\n", encoding="utf-8")

    if replay_reader:
        reader = runtime / "replay-reader"
        required = ("ChestCounter.dll", "ChestCounter.deps.json", "ChestCounter.runtimeconfig.json", "LICENSE")
        if not all((replay_reader / filename).is_file() for filename in required):
            raise SystemExit("The replay reader folder must include the .NET build and its upstream license.")
        shutil.copytree(replay_reader, reader,
                        ignore=shutil.ignore_patterns("*.exe", "*.pdb", "*.replay", "data.json"))

    summary, listing = signature_report(app_root)
    provenance += summary
    (runtime / "PROVENANCE.txt").write_text("\n".join(provenance) + "\n", encoding="utf-8")
    (out / "SIGNATURES.txt").write_text(f"{APP_NAME} v{__version__} - Authenticode signature of every executable file\n\n"
                                        + "\n".join(listing) + "\n", encoding="utf-8")
    write_package_files(runtime)

    audit(app_root)

    # ---- 6. reproducible zip + checksums
    zip_path = out / f"{app_root.name}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(p for p in app_root.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(str(Path(app_root.name) / f.relative_to(app_root)).replace("\\", "/"), ZIP_TIME)
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info, f.read_bytes(), compresslevel=9)
    assets = [zip_path, Path(ffmpeg_source)]
    (out / "SHA256SUMS.txt").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in assets), encoding="utf-8", newline="\n")
    print("\n".join(provenance))
    print(f"Built {app_root}\n      {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)\n      {out / 'SHA256SUMS.txt'}"
          f"\n      {out / 'SIGNATURES.txt'}")
    return app_root, zip_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-embed", help="python-3.X.Y-embed-amd64.zip from python.org (same 3.X as this Python)")
    ap.add_argument("--ffmpeg-zip", help="a win64 LGPL ffmpeg build zip (see packaging/FFMPEG-NOTICE.txt)")
    ap.add_argument("--ffmpeg-source", help="the FFmpeg source archive of the exact bundled commit")
    ap.add_argument("--mingw", help="folder holding MinGW gcc.exe + windres.exe if they are not on PATH")
    ap.add_argument("--out", default=str(ROOT / "build_out"))
    ap.add_argument("--preflight", action="store_true", help="only report whether this PC can build the release")
    ap.add_argument("--launcher-only", action="store_true", help="only compile the launcher (seconds of work), to use with --prebuilt-launcher elsewhere")
    ap.add_argument("--development-launcher", action="store_true", help="with --launcher-only, launch the checkout's .venv")
    ap.add_argument("--prebuilt-launcher", help="use this launcher instead of compiling one, on a PC that cannot run the compiler")
    ap.add_argument("--replay-reader", type=Path, help="LED-N .NET reader build with dependency licenses")
    a = ap.parse_args()
    if a.development_launcher and not a.launcher_only:
        ap.error("--development-launcher requires --launcher-only")
    if a.preflight:
        sys.exit(preflight(a.python_embed, a.ffmpeg_zip, a.ffmpeg_source, a.mingw, a.out, a.prebuilt_launcher))
    if a.launcher_only:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        work = ROOT / "build_out" / "_development-launcher-build" if a.development_launcher else Path(a.out) / "_launcher-build"
        build_launcher(Path(a.out) / EXE_NAME, work, a.mingw, development=a.development_launcher)
        print(f"Built {Path(a.out) / EXE_NAME} for v{__version__}\nSHA-256 {sha256(Path(a.out) / EXE_NAME)}")
        sys.exit(0)
    if not (a.python_embed and a.ffmpeg_zip and a.ffmpeg_source):
        ap.error("--python-embed, --ffmpeg-zip and --ffmpeg-source are required to build")
    build(Path(a.out), Path(a.python_embed), Path(a.ffmpeg_zip), Path(a.ffmpeg_source), a.mingw, a.prebuilt_launcher, a.replay_reader)
