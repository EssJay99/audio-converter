"""System tray icon for the Audio Converter desktop app.

Thread-safety contract (load-bearing): the tray lives on its own thread
while the pywebview window owns the GUI thread. Calling window methods
(hide/show/destroy excluded — see quit_app) across threads can crash the
toolkit and take downloads down with it, so this module NEVER touches the
window except through quit_app's guarded destroy-then-exit path. Everything
else is limited to inherently thread-safe operations: a threading.Event,
read-only settings lookups, and spawning the OS file manager.

Any failure here (missing backend, no display, stray exception) degrades to
"no tray icon" and the app runs exactly as before.
"""
import os
import subprocess
import sys


def icon_path():
    """Absolute path of the bundled tray PNG, or None when missing."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        base = os.path.join(sys._MEIPASS, 'static', 'img')
    else:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'static', 'img')
    candidate = os.path.join(base, 'tray.png')
    return candidate if os.path.isfile(candidate) else None


def load_icon_image(path=None):
    """PIL image for the tray, or None when Pillow/the file is unavailable."""
    from PIL import Image
    target = path or icon_path()
    if not target:
        return None
    try:
        image = Image.open(target)
        image.load()
        return image
    except Exception:
        return None


def is_paused():
    from app.routes.convert import _queue_paused
    return _queue_paused.is_set()


def toggle_pause():
    """Flip the global download pause. Returns True when now paused."""
    from app.routes.convert import _queue_paused
    if _queue_paused.is_set():
        _queue_paused.clear()
        return False
    _queue_paused.set()
    return True


def output_folder():
    """The user's configured output folder, or '' when unknown."""
    try:
        from app import app as flask_app
        from app.models import UserSettings
        with flask_app.app_context():
            settings = UserSettings.query.first()
            if settings and settings.output_path:
                return settings.output_path
    except Exception:
        pass
    return ''


def open_output_folder():
    """Reveal the output folder in the OS file manager. Returns bool."""
    folder = output_folder()
    if not folder or not os.path.isdir(folder):
        return False
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        elif sys.platform.startswith('win'):
            subprocess.Popen(['explorer', folder])
        else:
            subprocess.Popen(['xdg-open', folder])
        return True
    except Exception:
        return False


def quit_app(get_window):
    """Quit via the normal window path, guaranteeing process exit.

    window.destroy() lets pywebview unwind cleanly (server shutdown runs
    afterwards in main). If the toolkit refuses the cross-thread call,
    os._exit is the backstop: SQLite WAL recovers on next launch and temp
    files are cleaned then, so nothing is lost beyond the current second.
    """
    try:
        window = get_window() if callable(get_window) else get_window
        if window is not None:
            window.destroy()
            return 'destroy'
    except Exception:
        pass
    os._exit(0)
    return 'exit'  # pragma: no cover - os._exit never returns


def menu_spec(paused):
    """Plain-data menu description (no toolkit dependency; unit-tested)."""
    return [
        {'label': 'Resume downloads' if paused else 'Pause downloads',
         'action': 'toggle_pause'},
        {'label': 'Open output folder', 'action': 'open_folder'},
        {'label': 'Quit', 'action': 'quit'},
    ]


def build_menu(pystray_module, callbacks):
    """Map menu_spec() onto real pystray menu items."""
    items = []
    for entry in menu_spec(callbacks['is_paused']()):
        action = entry['action']
        if action == 'toggle_pause':
            items.append(pystray_module.MenuItem(
                lambda item: ('Resume downloads'
                              if callbacks['is_paused']() else 'Pause downloads'),
                callbacks['toggle_pause']))
        elif action == 'open_folder':
            items.append(pystray_module.MenuItem(
                entry['label'], callbacks['open_folder']))
        elif action == 'quit':
            items.append(pystray_module.MenuItem(
                entry['label'], callbacks['quit']))
    return pystray_module.Menu(*items)


def start_tray(get_window, pystray_module=None):
    """Show the tray icon on its own thread. Returns the icon or None.

    None means "running traysless" — missing dependency, no display, or any
    backend error — and is always a safe outcome.
    """
    try:
        if pystray_module is None:
            import pystray as pystray_module
        image = load_icon_image()
        if image is None:
            return None

        def _toggle(icon, _item):
            toggle_pause()

        def _folder(icon, _item):
            open_output_folder()

        def _quit(icon, _item):
            try:
                icon.stop()
            except Exception:
                pass
            quit_app(get_window)

        menu = build_menu(pystray_module, {
            'is_paused': is_paused,
            'toggle_pause': _toggle,
            'open_folder': _folder,
            'quit': _quit,
        })
        icon = pystray_module.Icon('AudioConverter', image,
                                   'Audio Converter', menu)
        icon.run_detached()
        return icon
    except Exception:
        return None
