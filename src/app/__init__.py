from flask import Flask, render_template
import os
import sys
import logging

from sqlalchemy import inspect, text

# Bump on each packaged release; shown in the footer and used by update-check.
APP_VERSION = '1.0.1'


def _version_tuple(value):
    """'v1.2.3' -> (1, 2, 3) for release comparisons. Unparseable -> ()."""
    try:
        return tuple(int(part) for part in str(value).strip().lstrip('v').split('.'))
    except (TypeError, ValueError):
        return ()


def _resource_base():
    """Directory holding the bundled templates/static folders.

    Inside a PyInstaller bundle this is sys._MEIPASS; everywhere else it is
    the src/ tree next to this package.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return os.path.join(os.path.dirname(__file__), '..')


_resource_base_dir = _resource_base()

app = Flask(__name__,
            template_folder=os.path.join(_resource_base_dir, 'templates'),
            static_folder=os.path.join(_resource_base_dir, 'static'))

basedir = os.path.abspath(os.path.dirname(__file__))
# The DB location and secret key can be relocated via environment variables.
# The desktop launcher (src/desktop.py) uses this to keep data in a
# per-user application-support folder; tests use it to isolate a test DB.
db_path = os.environ.get('AUDIO_CONVERTER_DB_PATH') or os.path.join(basedir, '..', 'config.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# File uploads (adopt-a-file drops) stream to disk, but cap the total so a
# runaway post can't fill the volume. Breaches land on the 413 handler.
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024
# The worker thread writes to SQLite on a separate connection; raise the lock timeout
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'timeout': 15}}

# Stable secret key (env var wins; else a file next to the app; else random for dev)
secret_key = os.environ.get('AUDIO_CONVERTER_SECRET_KEY')
if not secret_key:
    secret_file = os.path.join(basedir, '..', '.secret_key')
    if os.path.exists(secret_file):
        with open(secret_file, 'r') as f:
            secret_key = f.read().strip()
if not secret_key:
    secret_key = os.urandom(24).hex()
    try:
        with open(secret_file, 'w') as f:
            f.write(secret_key)
        os.chmod(secret_file, 0o600)
    except Exception:
        pass
app.secret_key = secret_key
# The session cookie only carries the CSRF token and flash messages, but
# lock it down anyway: unreadable to JS, never sent cross-site.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


@app.after_request
def _security_headers(response):
    """Defense-in-depth headers for a local-only app opened in a browser."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    return response


@app.before_request
def _local_host_only():
    """Refuse requests addressed anywhere but this machine.

    The server binds 127.0.0.1, but a rebinding or forwarded Host could
    otherwise make the browser treat it as a foreign origin. Only
    loopback names (with any port) get through.
    """
    from flask import request as _request
    host = (_request.host or '').lower()
    if host.startswith('['):
        name = host.split(']')[0].lstrip('[')
    elif host.count(':') > 1:
        name = host  # malformed IPv6 literal: reject below
    else:
        name = host.split(':')[0]
    if name not in ('127.0.0.1', 'localhost', '::1'):
        return {'ok': False,
                'message': 'This app only answers on localhost.'}, 403


from app.models import db

db.init_app(app)
from app.models import ConversionHistory, UserSettings

# CSRF protection for all POST forms
from flask_wtf import CSRFProtect
csrf = CSRFProtect(app)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Lightweight schema setup / migration -------------------------------------
# SQLite ALTER TABLE can add columns, so instead of forcing users to delete
# their database on upgrade we add any missing columns on startup.
_SCHEMA_MIGRATIONS = {
    'conversion_history': [
        ('parent_id', 'INTEGER'),
        ('is_playlist', 'BOOLEAN DEFAULT 0'),
        ('playlist_title', 'VARCHAR(250)'),
        ('item_index', 'INTEGER DEFAULT 0'),
        ('item_count', 'INTEGER DEFAULT 1'),
        ('playlist_organization', 'VARCHAR(20) DEFAULT "folder"'),
        ('retry_attempts', 'INTEGER DEFAULT 0'),
        ('import_source', 'VARCHAR(20) DEFAULT ""'),
        ('expected_duration', 'FLOAT DEFAULT 0'),
        ('expected_title', 'VARCHAR(250) DEFAULT ""'),
        ('import_misses', 'TEXT DEFAULT "[]"'),
        ('subscription_id', 'INTEGER'),
        ('liked', 'BOOLEAN DEFAULT 0'),
        ('play_count', 'INTEGER DEFAULT 0'),
        ('last_played_at', 'DATETIME'),
        ('duration', 'FLOAT DEFAULT 0'),
        ('tag_title', 'VARCHAR(500) DEFAULT ""'),
        ('tag_artist', 'VARCHAR(500) DEFAULT ""'),
        ('tag_album', 'VARCHAR(500) DEFAULT ""'),
        ('rating', 'INTEGER DEFAULT 0'),
        ('cover_url', 'VARCHAR(1000) DEFAULT ""'),
        ('quality', 'VARCHAR(100) DEFAULT ""'),
        ('job_options', 'TEXT DEFAULT "{}"'),
    ],
    'user_settings': [
        ('skip_existing', 'BOOLEAN DEFAULT 1'),
        ('retry_count', 'INTEGER DEFAULT 3'),
        ('privacy_mode', 'BOOLEAN DEFAULT 1'),
        ('job_timeout', 'INTEGER DEFAULT 300'),
        ('bandwidth_limit', 'INTEGER DEFAULT 0'),
        ('proxy', 'VARCHAR(500) DEFAULT ""'),
        ('offpeak_limit', 'INTEGER DEFAULT 0'),
        ('offpeak_start', 'INTEGER DEFAULT 22'),
        ('offpeak_end', 'INTEGER DEFAULT 7'),
        ('worker_count', 'INTEGER DEFAULT 3'),
        ('subtitles', 'BOOLEAN DEFAULT 1'),
        ('sponsorblock', 'BOOLEAN DEFAULT 1'),
        ('normalize_audio', 'BOOLEAN DEFAULT 0'),
        ('auto_update_ytdlp', 'BOOLEAN DEFAULT 0'),
        ('default_format', 'VARCHAR(20) DEFAULT "flac"'),
        ('video_quality', 'VARCHAR(10) DEFAULT "1080p"'),
        ('video_crf', 'INTEGER DEFAULT 23'),
        ('video_preset', 'VARCHAR(20) DEFAULT "veryfast"'),
        ('default_video_format', 'VARCHAR(20) DEFAULT "video_mp4"'),
        ('finish_action', 'VARCHAR(10) DEFAULT "nothing"'),
        ('seen_version', 'VARCHAR(20) DEFAULT ""'),
        ('numbered_filenames', 'BOOLEAN DEFAULT 0'),
        ('nfo_files', 'BOOLEAN DEFAULT 0'),
        ('tidal_client_id', 'VARCHAR(200) DEFAULT ""'),
        ('tidal_client_secret', 'VARCHAR(200) DEFAULT ""'),
        ('tidal_access_token', 'VARCHAR(2000) DEFAULT ""'),
        ('tidal_refresh_token', 'VARCHAR(2000) DEFAULT ""'),
        ('tidal_expires_at', 'INTEGER DEFAULT 0'),
        ('desktop_notifications', 'BOOLEAN DEFAULT 1'),
        ('close_behavior', 'VARCHAR(10) DEFAULT "ask"'),
        ('tray_icon', 'BOOLEAN DEFAULT 1'),
    ],
}


# Indexes for the hot query paths (status scans, playlist children,
# newest-first listing). Created idempotently alongside the migrations.
_SCHEMA_INDEXES = {
    'conversion_history': ['status', 'parent_id', 'created_at', 'url'],
}


def _backup_database(db_path, keep=5):
    """Copy the database aside before migrations touch it. Keeps `keep`.

    Uses the SQLite backup API rather than a file copy: with WAL mode the
    live database spans two files, and a plain copy can catch an
    inconsistent snapshot.
    """
    import datetime
    try:
        if not db_path or db_path == ':memory:' or not os.path.isfile(db_path):
            return None
        backup_dir = os.path.join(os.path.dirname(db_path), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = os.path.join(backup_dir, f'config-{stamp}.db')
        import sqlite3
        src = sqlite3.connect('file:{}?mode=ro'.format(db_path), uri=True,
                              timeout=15)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        existing = sorted(f for f in os.listdir(backup_dir) if f.endswith('.db'))
        for stale in existing[:-keep]:
            try:
                os.remove(os.path.join(backup_dir, stale))
            except OSError:
                pass
        logger.info('Backed up database to %s', dest)
        return dest
    except Exception as exc:
        logger.warning('Could not back up database: %s', exc)
        return None


def _setup_db():
    """Create missing tables/columns so both fresh and old DBs work."""
    from app.models import db

    db.create_all()
    try:
        with db.engine.begin() as conn:
            # WAL mode lets the parallel download workers write concurrently
            # instead of serializing on the database lock.
            conn.execute(text('PRAGMA journal_mode=WAL'))
            # Refresh the query planner's statistics; cheap, recommended
            # periodically for databases whose content churns.
            conn.execute(text('PRAGMA optimize'))
    except Exception as exc:
        logger.warning('Could not enable WAL mode: %s', exc)
    # Figure out up front whether any migration is pending so at most one
    # backup is taken per startup, before anything is altered.
    try:
        pending = False
        for table, columns in _SCHEMA_MIGRATIONS.items():
            try:
                have = {c['name'] for c in inspect(db.engine).get_columns(table)}
            except Exception:
                continue
            if any(name not in have for name, _ddl in columns):
                pending = True
                break
        if pending:
            try:
                _backup_database(db.engine.url.database)
            except Exception as exc:
                logger.warning('Pre-migration backup failed: %s', exc)
    except Exception as exc:
        logger.warning('Could not check pending migrations: %s', exc)
    for table, columns in _SCHEMA_MIGRATIONS.items():
        try:
            existing = {c['name'] for c in inspect(db.engine).get_columns(table)}
        except Exception:
            continue
        for name, ddl in columns:
            if name in existing:
                continue
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
                logger.info('Added %s.%s column', table, name)
            except Exception as exc:
                logger.warning('Could not add %s.%s: %s', table, name, exc)
    for table, columns in _SCHEMA_INDEXES.items():
        try:
            existing = {i['name'] for i in inspect(db.engine).get_indexes(table)}
        except Exception:
            continue
        for column in columns:
            name = f'idx_{table}_{column}'
            if name in existing:
                continue
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(
                        f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})'))
                logger.info('Added index %s', name)
            except Exception as exc:
                logger.warning('Could not add index %s: %s', name, exc)


@app.errorhandler(404)
def not_found(error):
    return render_template('error_404.html', message="Page Not Found"), 404


@app.errorhandler(500)
def internal_error(error):
    return render_template('error_500.html', message="Internal Server Error"), 500


@app.errorhandler(413)
def too_large(error):
    # Only file uploads can hit this; they always expect JSON.
    return {'ok': False,
            'message': 'File too large (1 GB limit).'}, 413


@app.template_filter('basename')
def basename_filter(path):
    return os.path.basename(path) if path else ''


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from app.routes import register_blueprints
register_blueprints(app)

if __name__ == "__main__":
    _setup_db()
    app.run(debug=True, host="127.0.0.1", port=5000)
