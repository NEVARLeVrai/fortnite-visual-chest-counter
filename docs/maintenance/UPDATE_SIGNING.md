# Update signing

Internal / maintainer note. User-facing docs do not need any of this.

The built-in updater installs only releases signed with a private key that never leaves the maintainer's control. This file
covers what that protects, where trust comes from, and how to create, store, use and replace the keys.

**Fork trust (v0.3.0):** the fork's primary and recovery public keys are in `TRUSTED_PUBLIC_KEYS` in
[`update/trust.py`](../../src/fortnite_chest_counter/update/trust.py). Their private halves never enter this repository, a build or a test
PC. Development and tests use temporary keys generated during each test run.

## What this protects

A checksum published next to a zip only proves the download is not corrupted: anyone who can change the zip on GitHub can
change the checksum too. A signature proves the release was made by whoever holds the private key, so write access to the
GitHub repository is not enough to get an update installed.

It does not:

- Make the launcher or any other Windows program Authenticode-signed. See [WINDOWS_TRUST.md](WINDOWS_TRUST.md).
- Protect a manual download. A copy installed by hand is only as trustworthy as the place it came from.

## Where trust comes from

The only keys that count are the literal list in `update/trust.py` **inside the installed app**.

- A release cannot supply a key. Key fields in the manifest or signature file, extra release files, API fields and release
  notes are ignored. The updater fetches only the manifest and its signature.
- A key id only selects among the keys already installed. The key bytes that verify a signature always come from the
  installed list, so a forged or colliding id gains nothing.
- There is no trust on first use. An empty list rejects everything.
- Verification changes nothing. The list is an immutable constant, and no setting, environment variable or file can extend it.

The list changes only when a new version of the app is installed, and that version must first pass verification by a key the
installed app already trusts. **The updater must verify a release using the installed app's list before unpacking or running
anything from the zip. Nothing from a new package may take part in deciding whether that package is trusted.**

The limit: whoever holds a trusted private key can sign a release that changes the list. That is why the keys are protected as
described below.

### First install

Install the first fork release (v0.3.0) from the fork's GitHub release page to establish its trust list. Upstream installations use a different update source and keys. Later releases from this fork must be signed with a key already trusted by the installed fork release.

## Files in each release

| Asset | Used by |
|---|---|
| `Fortnite-Visual-Chest-Counter-vX.Y.Z.zip` | The updater and manual installs |
| `update-manifest.json` | The updater: what was released |
| `update-manifest.json.sig` | The updater: the signature(s) of the manifest |
| `SHA256SUMS.txt` | People checking a manual download |
| `ffmpeg-source-<commit>.tar.gz` | The source the FFmpeg licence requires us to offer |

`update-manifest.json` is canonical JSON on one line, with sorted keys, no spaces and a final newline. The same inputs always
give the same bytes, and any other form is rejected:

```json
{"format":1,"product":"fortnite-visual-chest-counter","sha256":"<hex>","size":155573320,"version":"0.2.3","zip":"Fortnite-Visual-Chest-Counter-v0.2.3.zip"}
```

`update-manifest.json.sig` holds one or more Ed25519 signatures of those exact bytes. `key_id` is the first 16 hex characters of
the SHA-256 of the raw public key:

```json
{"format":1,"signatures":[{"key_id":"<16 hex>","signature":"<base64>"}]}
```

The updater verifies the signature first, then requires the manifest to match the release tag, the zip name and size GitHub
lists, GitHub's SHA-256 digest when it reports one, and finally the downloaded file. The version must be newer than the
installed one.

## Creating the keys

Create **two** keys and ship both public keys from the first signed release: a primary for signing releases and an offline
recovery key that is used only if the primary is lost or stolen. With only one key, losing it forces every user into another manual
install.

Run this on a PC you trust, for each key, with a path outside the repository:

```powershell
python packaging/sign_release.py keygen --out "<a folder outside the repository>\<primary key file>"
python packaging/sign_release.py keygen --out "<offline media>\<recovery key file>"
```

The tool asks for a passphrase of at least 16 characters, encrypts the file, never overwrites an existing key and refuses a path
inside this repository or a cloud-synced folder. It prints each public key: add both to `TRUSTED_PUBLIC_KEYS`.

To prove later that a key file and its passphrase still work, without signing anything:

```powershell
python packaging/sign_release.py check-key --key "<key file>"
```

It prints only the key id, the public key, and whether this copy of the code trusts it. None of these tools reports where a key file
is kept, so their output can be shared safely.

## Storing the keys

- Keep private keys out of this repository, build folders, GitHub secrets, CI and cloud-synced folders.
- Use a different passphrase for each key, kept in a password manager.
- Keep at least one encrypted copy of each key on separate offline media. Keep the recovery key **only** offline.

## Signing a release

1. Build the release zip (see below). The build refuses to start unless `TRUSTED_PUBLIC_KEYS` holds both production keys and every
   Python package matches `packaging/build-requirements.txt`. There is no option to skip either check.
2. Sign it: `python packaging/sign_release.py sign --version X.Y.Z --zip <the zip> --key <primary key file>`
3. Check it: `python packaging/check_release.py --out <the folder holding the zip> --inputs <the folder holding the build inputs>`
4. Upload all five files it lists to a **draft** release, then publish it, so a half-uploaded release is never visible.

Sign with the primary key. The recovery key stays offline unless the primary is lost or stolen.

`check_release.py` is the last gate. Nothing may be published unless it passes: the signature verifies with the trust list inside
the zip, the manifest matches the zip and the exact version, the package passes the same inspection the updater applies, every updater
module is present, `package-files.json` is exact, the launcher is for this version, the shipped packages are exactly the pinned ones,
the embedded Python is the official one (apart from the single line the build adds to its `._pth` file), the zip's trust list matches
this repository's, `SHA256SUMS.txt` matches both the zip and the FFmpeg source archive, the release notes exist, and no private key is
in or beside the release. A publish step must take its file list from `check_release.verified_assets()`, which raises instead of
returning a list for a release that fails.

Two locations inside the package are exempt from the private-key scan because they are public by design: `certifi/cacert.pem` (the
certificate bundle) and `Crypto/SelfTest/` (the crypto library's published test keys).

## Building on another PC

Signing stays on the PC holding the keys. **The private keys never go to a build or test PC.** The build needs only the public keys,
which are already in the source.

1. On the build PC: `python packaging/build_release.py --preflight --python-embed <zip> --ffmpeg-zip <zip> --ffmpeg-source <archive> --out <short path>`.
   It builds and changes nothing, and reports each requirement, including whether `gcc` and `windres` can run there.
2. The compiler programs are not Authenticode-signed, so a PC with Smart App Control on may refuse them. In that case, compile only
   the launcher on the release PC, which takes seconds: `python packaging/build_release.py --launcher-only --out <folder>`. Then add
   `--prebuilt-launcher <that exe>` on the build PC, for the preflight and the build. A launcher for another version is refused.
3. Build on the build PC, bring the zip back, and sign and check it on the PC holding the keys.

## Changing keys

**Planned change.** Create the new key. Ship a release whose trust list holds the old and new keys, signed by the old key. Keep
signing with both (repeat `--key`) for as long as older copies may still need updating. Remove the old key from the list only
in a release that is still signed by it as well.

**Primary lost, recovery key safe.** Sign the next release with the recovery key; every copy already trusts it. In that release, add a
new key to the trust list and remove the lost one.

**Primary stolen.** Treat the GitHub account as at risk too: change its password and tokens. Publish a release signed by the
recovery key that removes the stolen key and adds a new one. Installed copies cannot be reached remotely, so each keeps trusting
the stolen key until it installs that release. A thief would also need a way to publish to this repository.

**Every key lost.** No installed copy can verify a new release. Create new keys and tell users to install the next version by
hand, which establishes a new trust list, the same way as the first install.
