# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller bundle for the Audio Converter desktop app.

Build on each OS to produce that OS's artifact (helpers are fetched by the
per-OS build scripts before this runs):
  macOS  : dist/AudioConverter.app  (real .app bundle, goes in the DMG)
  Windows: dist/AudioConverter/      (AudioConverter.exe, wrapped by NSIS)
  Linux  : dist/AudioConverter/      (AudioConverter binary, tarred)

The bundle ships standalone yt-dlp/ffmpeg helper binaries (see the build
scripts) and serves templates/static from inside the bundle via
sys._MEIPASS. FFmpeg is expected on the system PATH (one `brew install
ffmpeg`); the app warns clearly when it is missing.
"""
import os
import sys

try:
    SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # Fall back to the working directory (the build script cds to the root).
    SPEC_DIR = os.getcwd()

helper_binaries = []
for name in ('yt-dlp', 'ffmpeg', 'ffprobe'):
    candidate = os.path.join(SPEC_DIR, 'build_helpers', name)
    if os.path.isfile(candidate):
        helper_binaries.append((candidate, '.'))

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, 'src', 'desktop.py')],
    pathex=[os.path.join(SPEC_DIR, 'src')],
    binaries=helper_binaries,
    datas=[
        (os.path.join(SPEC_DIR, 'src', 'templates'), 'templates'),
        (os.path.join(SPEC_DIR, 'src', 'static'), 'static'),
    ],
    hiddenimports=[
        'flask',
        'flask_sqlalchemy',
        'flask_wtf',
        'wtforms',
        'sqlalchemy',
        'sqlalchemy.sql.default_comparator',
        'jinja2',
        'markupsafe',
        'werkzeug',
        'click',
        'itsdangerous',
        'blinker',
        'requests',
        'webview',
        'pystray',
        'PIL',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'notebook', 'IPython'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioConverter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AudioConverter',
)

if sys.platform == 'darwin':
    # A real .app bundle for the DMG (in addition to the folder above).
    app = BUNDLE(
        coll,
        name='AudioConverter.app',
        icon=None,
        bundle_identifier='com.audioconverter.desktop',
        info_plist={
            'NSHighResolutionCapable': True,
            'CFBundleShortVersionString': '1.0.0',
            'LSMinimumSystemVersion': '11.0',
        },
    )
