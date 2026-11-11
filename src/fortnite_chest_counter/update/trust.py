"""
The Ed25519 public keys this copy of the app trusts to sign updates: raw 32-byte keys, as hex. See docs/maintenance/UPDATE_SIGNING.md.

Public keys are not secret. The matching private keys never enter this repository or a release.

An update must carry a valid signature from one of these keys. This list changes only by installing a release that a key already
listed has signed: nothing in a release, a setting or a file can add to it.
"""

TRUSTED_PUBLIC_KEYS = (
    "5afc6037bc21cf372817a9b6ad3bfbc3b64bef5d0c4c3b9894d7f9f63d197432",  # primary fork key
    "9e329396cbe277eda711efa8947c8af98e54a42b8a86ddd8fe6da3ac58968136",  # recovery fork key
)
