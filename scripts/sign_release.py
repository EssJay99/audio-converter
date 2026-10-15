#!/usr/bin/env python3
"""Sign a release SHA256SUMS.txt file with a minisign-compatible key.

Maintained alongside the codebase so contributors can produce signed
checksums without needing the `minisign` CLI on their PATH.

Usage:
    AUDIO_CONVERTER_SIGNING_KEY=./release-signing.key \
        python scripts/sign_release.py path/to/SHA256SUMS.txt

    python scripts/sign_release.py --generate
        Create a fresh keypair. Prints the public-key file contents to
        commit and the base64 private key to store as the
        AUDIO_CONVERTER_SIGNING_KEY_B64 secret. The private key is
        printed once — store it immediately and never commit it.

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
        raw = f.read()
    # Tolerate one trailing newline (e.g. key file written with echo),
    # but never strip the key material itself: bytes.strip() would eat a
    # legitimate trailing 0x0a byte and corrupt the key.
    if raw.endswith(b'\n'):
        raw = raw[:-1]
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
        sys.stderr.write('usage: sign_release.py <SHA256SUMS.txt> [version]\n'
                         '       sign_release.py --generate\n')
        sys.exit(1)
    if sys.argv[1] == '--generate':
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption())
        pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw)
        keynum = _keynum_for(pub).hex().upper()
        sys.stdout.write(
            '1. Commit this as src/release-signing-pubkey.txt:\n\n'
            f'untrusted comment: minisign public key {keynum}\n'
            f'{base64.b64encode(pub).decode("ascii")}\n\n'
            '2. Store this base64 line as the AUDIO_CONVERTER_SIGNING_KEY_B64\n'
            '   GitHub Actions secret (one line, no spaces):\n\n'
            f'{base64.b64encode(raw).decode("ascii")}\n')
        sys.exit(0)
    sign_sums(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'unknown')