#!/usr/bin/env python3
"""Sign a release SHA256SUMS.txt file with a minisign-compatible key.

Maintained alongside the codebase so contributors can produce signed
checksums without needing the `minisign` CLI on their PATH.

Usage:
    AUDIO_CONVERTER_SIGNING_KEY=./release-signing.key \
        python scripts/sign_release.py path/to/SHA256SUMS.txt

The key file is the 32-byte raw Ed25519 secret key (the same on-disk
format used by `minisign -G -p` before `cat ~/.minisign/minisign.key`
is un-base64'd). Set the env var to the file path; the script refuses
to accept the key on the command line so it never lands in shell
history.

Output is written next to the input as ``SHA256SUMS.txt.minisig`` in
the format minisign writes:

    untrusted comment: minisign signed message
    <base64(keynum || Ed25519 sig over keynum || b"trust" || file)>
    trusted_comment: Audio Converter release <version>
    <base64(keynum || Ed25519 sig)>     # duplicate, matches minisign
"""
import base64
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def _keynum_for(public_bytes):
    """minisign's per-key 8-byte identifier: last 8 bytes of SHA256(pub)."""
    import hashlib
    return hashlib.sha256(public_bytes).digest()[-8:]


def sign_sums(path, version):
    with open(path, 'rb') as f:
        data = f.read()
    key_path = os.environ.get('AUDIO_CONVERTER_SIGNING_KEY', '').strip()
    if not key_path:
        sys.stderr.write(
            'Set AUDIO_CONVERTER_SIGNING_KEY to the file holding the raw '
            '32-byte Ed25519 secret key.\n')
        sys.exit(2)
    with open(key_path, 'rb') as f:
        raw = f.read().strip()
    if len(raw) != 32:
        sys.stderr.write(
            f'Key file must be exactly 32 bytes; got {len(raw)}.\n')
        sys.exit(2)
    priv = Ed25519PrivateKey.from_private_bytes(raw)
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    keynum = _keynum_for(pub_bytes)
    sig = priv.sign(keynum + b'trust' + data)
    blob = base64.b64encode(keynum + sig).decode('ascii')
    out_path = path + '.minisig'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('untrusted comment: minisign signed message\n')
        f.write(blob + '\n')
        f.write(f'trusted_comment: Audio Converter release {version}\n')
        f.write(blob + '\n')
    sys.stdout.write(f'wrote {out_path}\n')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.stderr.write('usage: sign_release.py <SHA256SUMS.txt> [version]\n')
        sys.exit(1)
    sign_sums(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'unknown')