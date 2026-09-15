# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the AudioConverter desktop app.

Build on each OS to produce that OS's artifact:
  macOS  : dist/AudioConverter.app   (drag to /Applications)
  Windows: dist/AudioConverter/      (AudioConverter.exe)
  Linux  : dist/AudioConverter/      (AudioConverter binary)

Requires: pip install pyinstaller   (installs pyinstaller-hooks-contrib,
which supplies the yt-dlp / flask / sqlalchemy collection hooks)
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = os.path.abspath(os.path.dirname(__file__))
SRC = os.path.join(ROOT, 'src')

datas = [
    (os.path.join(SRC, 'templates'), 'templates'),
    (os.path.join(SRC, 'static'), 'static'),
]
# Data embedded inside the app package itself (if any)
datas += collect_data_files('app')

hiddenimports = collect_submodules('app')
# Flask + SQLAlchemy + yt-dlp are covered by pyinstaller-hooks-contrib,
# but be explicit about the SQLite dialect used at runtime.
hiddenimports += collect_submodules('sqlalchemy.dialects.sqlite')
hiddenimports += collect_submodules('yt_dlp')

a = Analysis(
    [os.path.join(SRC, 'desktop.py')],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'tests'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioConverter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='AudioConverter.app',
        icon=None,
        bundle_identifier='com.audioconverter.desktop',
        info_plist={
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'LSMinimumSystemVersion': '11.0',
        },
    )
else:
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='AudioConverter',
    )