#!/usr/bin/env python3
"""Desktop launcher for the YouTube/SoundCloud Audio Converter.

Runs the Flask web app inside a native window via pywebview
(WebView2 on Windows, WKWebView on macOS, WebKitGTK on Linux).

The Flask app (~/Library/Application Support/AudioConverter on macOS,
%APPDATA%\\AudioConverter on Windows, ~/.local/share/AudioConverter on
Linux) keeps its SQLite database and secret key so the app is relocatable.

A native folder picker is exposed to the frontend as
`window.pywebview.api.pick_directory()` so the settings/convert forms can
choose an output folder without typing a path.

Usage:
    python desktop.py              # open the desktop window
    python desktop.py --browser    # fall back to the system web browser
    python desktop.py --port 5011  # use a specific port instead of auto-selecting
"""

import argparse
import os
import socket
import sys
import threading
import time
import urllib.request

APP_NAME = 'AudioConverter'
DEFAULT_HOST = '127.0.0.1'
# Stable default port so browser-persisted state (player queue, volume,
# dark mode) survives app restarts: localStorage is scoped to the origin,
# which includes the port. Falls back to a free port when taken.
DEFAULT_PORT = 57600


def get_data_dir():
    """Cross-platform user-writable folder for the settings DB and secret key."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(base, APP_NAME)
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', APP_NAME)
    xdg = os.environ.get('XDG_DATA_HOME')
    base = xdg or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, APP_NAME)


def find_free_port(preferred=None):
    """Ask the OS for a free TCP port on 127.0.0.1.

    Tries `preferred` first so restarts keep serving the same origin;
    falls back to any free port when it is taken.
    """
    for candidate in ([preferred] if preferred else []) + [0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((DEFAULT_HOST, candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError('Could not find a free TCP port')


def start_server(app, port, host=DEFAULT_HOST):
    """Run the Flask app in a background thread. Returns (thread, server, url)."""
    from werkzeug.serving import make_server

    server = make_server(host, port, app, threaded=True)
    url = 'http://{}:{}/'.format(host, port)

    thread = threading.Thread(target=server.serve_forever, name='flask-server', daemon=True)
    thread.start()

    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)
            return thread, server, url
        except Exception:
            time.sleep(0.1)

    raise RuntimeError('Could not reach the local server at ' + url)


def _wait_until_stopped(thread):
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _pick_directory():
    """Exposed to the frontend as pywebview.api.pick_directory()."""
    try:
        import webview
    except Exception:
        return None
    window = webview.windows[0] if webview.windows else None
    if window is None:
        return None
    try:
        result = window.create_file_dialog(webview.FileDialog.FOLDER)
    except AttributeError:
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
    if result:
        return str(result[0])
    return None


def should_confirm_close(behavior):
    """Whether closing the window should ask first. Anything but an
    explicit 'quit' choice confirms, so downloads are never lost silently."""
    return behavior != 'quit'


def _close_behavior():
    """The user's close choice from Settings ('ask' unless set to 'quit')."""
    try:
        from app.models import UserSettings
        settings = UserSettings.query.first()
        if settings and settings.close_behavior:
            return settings.close_behavior
    except Exception:
        pass
    return 'ask'


def main():
    parser = argparse.ArgumentParser(description='Launch the Audio Converter desktop app.')
    parser.add_argument('--port', type=int, default=None,
                        help='HTTP port to use (default: auto-select a free port)')
    parser.add_argument('--browser', action='store_true',
                        help='Open the system web browser instead of a desktop window')
    parser.add_argument('--self-test', action='store_true',
                        help='Verify the bundled app boots (DB + routes) then exit')
    args = parser.parse_args()

    if getattr(sys, 'frozen', False):
        # Prefer helper binaries shipped in the bundle (yt-dlp, ffmpeg).
        # PyInstaller one-dir layouts nest them inside _internal/.
        bundle_dir = os.path.dirname(sys.executable)
        search = [bundle_dir, os.path.join(bundle_dir, '_internal')]
        os.environ['PATH'] = os.pathsep.join(
            search + [os.environ.get('PATH', '')])
        # Windowed bundles have no console when double-clicked; never crash on print.
        if sys.stdout is None:
            sys.stdout = open(os.devnull, 'w')
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w')

    # Set up writable, relocatable storage BEFORE importing the app so Flask-
    # SQLAlchemy's engine picks up the right database URI (it is pinned at
    # init_app time).
    data_dir = get_data_dir()
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    os.environ['AUDIO_CONVERTER_DB_PATH'] = os.path.join(data_dir, 'config.db')

    secret_file = os.path.join(data_dir, '.secret_key')
    if os.path.isfile(secret_file):
        with open(secret_file, 'r') as f:
            secret_key = f.read().strip()
    else:
        secret_key = os.urandom(24).hex()
        try:
            with open(secret_file, 'w') as f:
                f.write(secret_key)
        except Exception:
            pass
    os.environ['AUDIO_CONVERTER_SECRET_KEY'] = secret_key

    from app import app

    with app.app_context():
        from app import _setup_db
        _setup_db()
        confirm_close = should_confirm_close(_close_behavior())

    from app.routes.convert import check_ffmpeg
    if not check_ffmpeg():
        print('Warning: FFmpeg not found on this system. '
              'Conversions will fail until FFmpeg is installed.', file=sys.stderr)

    if args.self_test:
        from app.routes.convert import _find_ytdlp
        print('self-test: db ok, ffmpeg={}, ytdlp={}, templates={}'.format(
            check_ffmpeg(), bool(_find_ytdlp()),
            os.path.isdir(app.template_folder)))
        return

    port = args.port or int(os.environ.get('PORT') or 0) or find_free_port(DEFAULT_PORT)
    thread, server, url = start_server(app, port)
    print('Serving {}'.format(url), file=sys.stderr)

    if args.browser:
        import webbrowser
        webbrowser.open(url)
        _wait_until_stopped(thread)
        server.shutdown()
        return

    try:
        import webview
    except Exception as exc:
        import webbrowser
        print('Desktop webview unavailable ({}); opening the system browser instead.'.format(exc),
              file=sys.stderr)
        webbrowser.open(url)
        _wait_until_stopped(thread)
        server.shutdown()
        return

    # Some backends require stdout/stderr to exist even when frozen
    if getattr(sys, 'frozen', False) and sys.platform == 'win32':
        sys.stdout = sys.stderr = open(os.devnull, 'w')

    window = webview.create_window(
        'YouTube/SoundCloud Audio Converter',
        url,
        width=1120,
        height=760,
        min_size=(900, 600),
        # Closing the window stops downloads; ask first unless the user
        # chose instant quit in Settings.
        confirm_close=confirm_close,
    )
    window.expose(_pick_directory)
    webview.start()
    server.shutdown()


if __name__ == '__main__':
    main()