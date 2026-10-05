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
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler

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


def _setup_logging(data_dir):
    """File logging with rotation, so diagnostics survive restarts.

    Frozen windowed bundles have no console at all; without this, errors
    vanish. 1 MB per file × 5 backups in <data_dir>/logs/app.log. Console
    output is preserved — this only adds the file handler.
    """
    try:
        log_dir = os.path.join(data_dir, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, 'app.log'),
            maxBytes=1024 * 1024, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s [%(name)s] %(message)s'))
        # Scrub secrets and tracking tokens out of every log line: bearer
        # tokens, API keys, query-string parameters, and any url-shaped
        # credential are replaced before the handler writes.
        handler.addFilter(_LogScrubber())
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
            root.addHandler(handler)

        def _log_uncaught(exc_type, exc_value, exc_tb):
            logging.getLogger('desktop').exception(
                'Uncaught exception', exc_info=(exc_type, exc_value, exc_tb))

        sys.excepthook = _log_uncaught
        return os.path.join(log_dir, 'app.log')
    except Exception:
        return None


class _LogScrubber(logging.Filter):
    """Redact tokens, keys, and long URLs from log records.

    Patterns:
    - `Authorization: Bearer ...` (or similar) → keep the scheme, drop the token.
    - `api_key=...`, `token=...`, `key=...` → drop the value.
    - `https://user:pass@host/` → keep the scheme + host, drop the credentials.
    - Trackers: `?si=`, `?token=`, `?key=` → drop everything past the public part.

    The goal is a diagnostic file the user can paste into a bug report
    without accidentally leaking credentials; the result still contains
    the URLs and messages that matter for debugging.
    """

    _PATTERNS = [
        # Authorization: Bearer / Token / Basic
        (re.compile(r'(?i)(authorization:\s*(?:Bearer|Token|Basic)\s+)\S+'),
                  r'\1[redacted]'),
        # api_key / token / key / secret as form/query values
        (re.compile(r'(?i)(api[_-]?key|token|access[_-]?token|refresh[_-]?token|'
                    r'client[_-]?secret|password|secret)=([^&\s,]+)'),
                  r'\1=[redacted]'),
        # user:pass in URLs
        (re.compile(r'(https?://)[^/\s:@]+:[^/\s:@]+@'),
                  r'\1[redacted]@'),
        # ?si=..., &token=... etc. — keep up to the unsafe query
        (re.compile(r'(\?)([^#\s]*)(?:si=|token=|key=|sig=)([^&\s#]+)'),
                  r'\1[query-redacted]'),
    ]
    _TRACKERS = ('si=', 'token=', 'key=', 'sig=')

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        scrubbed = msg
        for pattern, replacement in self._PATTERNS:
            scrubbed = pattern.sub(replacement, scrubbed)
        # Apply once more for `record.args` (the %-format args).
        try:
            if record.args:
                for i, arg in enumerate(record.args):
                    if isinstance(arg, str):
                        for pattern, replacement in self._PATTERNS:
                            arg = pattern.sub(replacement, arg)
                        record.args = list(record.args)
                        record.args[i] = arg
        except Exception:
            pass
        record.msg = scrubbed
        record.args = ()
        return True


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


def _selftest_verifier():
    """Exercise the installer checksum verifier against a fixture release.

    Serves a fake binary + SHA256SUMS.txt over loopback HTTP and runs the
    exact function the self-updater uses: valid bytes pass, tampered bytes
    and a missing sums file must both raise. Returns (ok, detail).
    """
    import hashlib
    import http.server
    from app.routes.convert import _verify_installer_bytes

    binary = b'audio-converter-fixture-installer'
    digest = hashlib.sha256(binary).hexdigest()
    sums = f'{digest}  Fixture-Setup-0.0.0.exe\n{"0" * 64}  decoy.bin\n'
    blob = {'/setup.exe': binary, '/SHA256SUMS.txt': sums.encode()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = blob.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body if isinstance(body, bytes) else body.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{port}'
        release = {'assets': [
            {'name': 'Fixture-Setup-0.0.0.exe',
             'browser_download_url': base + '/setup.exe'},
            {'name': 'SHA256SUMS.txt',
             'browser_download_url': base + '/SHA256SUMS.txt'},
        ]}
        _verify_installer_bytes(release, 'Fixture-Setup-0.0.0.exe', digest)
        try:
            _verify_installer_bytes(release, 'Fixture-Setup-0.0.0.exe',
                                    '0' * 64)
        except ValueError as e:
            if 'mismatch' not in str(e):
                return False, f'wrong tamper error: {e}'
        else:
            return False, 'tampered bytes accepted'
        try:
            _verify_installer_bytes({'assets': []}, 'Fixture-Setup-0.0.0.exe',
                                    digest)
        except ValueError:
            pass
        else:
            return False, 'missing sums file accepted'
        return True, 'accept/reject/missing-sums all correct'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'
    finally:
        server.shutdown()


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
    try:
        os.chmod(data_dir, 0o700)
    except Exception:
        pass
    os.environ['AUDIO_CONVERTER_DB_PATH'] = os.path.join(data_dir, 'config.db')
    _log_path = _setup_logging(data_dir)

    secret_file = os.path.join(data_dir, '.secret_key')
    if os.path.isfile(secret_file):
        with open(secret_file, 'r') as f:
            secret_key = f.read().strip()
    else:
        secret_key = os.urandom(24).hex()
        try:
            with open(secret_file, 'w') as f:
                f.write(secret_key)
            os.chmod(secret_file, 0o600)
        except Exception:
            pass
    os.environ['AUDIO_CONVERTER_SECRET_KEY'] = secret_key

    from app import app

    with app.app_context():
        from app import _setup_db
        _setup_db()
        confirm_close = should_confirm_close(_close_behavior())
        from app.models import UserSettings
        _tray_enabled = True
        _settings = UserSettings.query.first()
        if _settings is not None and not _settings.tray_icon:
            _tray_enabled = False

    from app.routes.convert import check_ffmpeg
    if not check_ffmpeg():
        print('Warning: FFmpeg not found on this system. '
              'Conversions will fail until FFmpeg is installed.', file=sys.stderr)

    if args.self_test:
        from app.routes.convert import _find_ytdlp
        print('self-test: db ok, ffmpeg={}, ytdlp={}, templates={}'.format(
            check_ffmpeg(), bool(_find_ytdlp()),
            os.path.isdir(app.template_folder)))
        verifier_ok, verifier_detail = _selftest_verifier()
        print(f'self-test: checksum verifier: {"ok" if verifier_ok else "FAILED"} '
              f'({verifier_detail})')
        return 0 if verifier_ok else 1

    port = args.port or int(os.environ.get('PORT') or 0) or find_free_port(DEFAULT_PORT)
    thread, server, url = start_server(app, port)
    print('Serving {}'.format(url), file=sys.stderr)
    logging.getLogger('desktop').info(
        'Serving %s (data: %s, log: %s)', url, data_dir, _log_path)

    try:
        from app.routes.convert import _ensure_scheduler
        with app.app_context():
            _ensure_scheduler()
            from app.routes.convert import _resume_interrupted
            resumed = _resume_interrupted()
            if resumed:
                logging.getLogger('desktop').info(
                    'Requeued %d interrupted conversion(s)', resumed)
    except Exception as exc:
        print(f'Scheduler failed to start ({exc}); subscriptions will not auto-check.',
              file=sys.stderr)

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

    if _tray_enabled:
        try:
            from tray_icon import start_tray
            _tray = start_tray(lambda: webview.windows[0] if webview.windows else None)
            if _tray is None:
                print('Tray unavailable on this system; continuing without it.',
                      file=sys.stderr)
        except Exception as exc:
            print(f'Tray failed to start ({exc}); continuing without it.',
                  file=sys.stderr)

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
    sys.exit(main() or 0)