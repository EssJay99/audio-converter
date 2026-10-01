#!/usr/bin/env python3
"""Verify a published release the way the app's self-updater will see it.

Usage:
    python scripts/verify_release.py v1.2.3
    python scripts/verify_release.py v1.2.3 --repo EssJay99/audio-converter

Downloads every asset of the tag's GitHub Release (or checks local files
with --dir), verifies each installer against SHA256SUMS.txt, and confirms
the app finds exactly one macOS (.dmg) and one Windows (.exe) installer —
the same selection logic the in-app updater uses. Exit nonzero on any
failure; CI runs this right after publishing.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request

SUMS_NAME = 'SHA256SUMS.txt'
INSTALLER_SUFFIXES = ('.dmg', '.exe', '.tar.gz')


def _api(url):
    req = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json',
                      'User-Agent': 'AudioConverter-verify/1.0'})
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _download(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'AudioConverter-verify/1.0'})
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, 'wb') as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def _parse_sums(text):
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sums[parts[1].lstrip('*')] = parts[0].lower()
    return sums


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_local(directory):
    """Check installer files already on disk against a sibling sums file."""
    names = sorted(os.listdir(directory))
    sums_path = os.path.join(directory, SUMS_NAME)
    if not os.path.isfile(sums_path):
        return [f'missing {SUMS_NAME} in {directory}']
    with open(sums_path, encoding='utf-8') as f:
        sums = _parse_sums(f.read())
    errors = []
    installers = [n for n in names if n.endswith(INSTALLER_SUFFIXES)]
    for suffix in ('.dmg', '.exe'):
        matches = [n for n in installers if n.endswith(suffix)]
        if len(matches) != 1:
            errors.append(f'expected exactly one {suffix} asset, found: {matches}')
    for name in installers:
        expected = sums.get(name)
        if not expected:
            errors.append(f'{name}: no checksum entry')
            continue
        actual = _sha256(os.path.join(directory, name))
        if actual != expected:
            errors.append(f'{name}: CHECKSUM MISMATCH')
        else:
            print(f'  ok  {name}')
    return errors


def verify_tag(repo, tag):
    release = _api(f'https://api.github.com/repos/{repo}/releases/tags/{tag}')
    assets = {a.get('name'): a.get('browser_download_url')
              for a in release.get('assets') or []}
    print(f"release {tag}: {sorted(assets)}")
    if SUMS_NAME not in assets:
        return [f'release has no {SUMS_NAME} asset']
    tmp = tempfile.mkdtemp(prefix='verify-release-')
    errors = []
    try:
        sums_path = os.path.join(tmp, SUMS_NAME)
        _download(assets[SUMS_NAME], sums_path)
        with open(sums_path, encoding='utf-8') as f:
            sums = _parse_sums(f.read())
        for suffix in ('.dmg', '.exe'):
            matches = [n for n in assets if n.endswith(suffix)]
            if len(matches) != 1:
                errors.append(f'expected exactly one {suffix} asset, found: {matches}')
        for name, url in sorted(assets.items()):
            if not name.endswith(INSTALLER_SUFFIXES):
                continue
            dest = os.path.join(tmp, name)
            print(f'downloading {name} ...')
            _download(url, dest)
            expected = sums.get(name)
            if not expected:
                errors.append(f'{name}: no checksum entry')
            elif _sha256(dest) != expected:
                errors.append(f'{name}: CHECKSUM MISMATCH')
            else:
                print(f'  ok  {name} ({os.path.getsize(dest)} bytes)')
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return errors


def main():
    parser = argparse.ArgumentParser(description='Verify release installer checksums.')
    parser.add_argument('tag', nargs='?', help='release tag, e.g. v1.2.3')
    parser.add_argument('--repo', default='EssJay99/audio-converter')
    parser.add_argument('--dir', help='verify local files instead of a tag')
    args = parser.parse_args()
    if args.dir:
        errors = verify_local(args.dir)
    elif args.tag:
        errors = verify_tag(args.repo, args.tag)
    else:
        parser.error('give a tag or --dir')
    if errors:
        print('FAILED:')
        for error in errors:
            print(' -', error)
        return 1
    print('All installer checksums verified.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
