# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller bundle for the Audio Converter desktop app (macOS).

Build with:  bash scripts/build_macos.sh
Output:      dist/AudioConverter/AudioConverter  (one-dir bundle)

The bundle ships the standalone yt-dlp binary (see the build script) and
serves templates/static from inside the bundle via sys._MEIPASS.
FFmpeg is expected on the system PATH (one `brew install ffmpeg`); the app
warns clearly when it is missing.
"""
import os

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
