from flask import Flask, render_template
import os
import sys
import logging

from sqlalchemy import inspect, text

# Bump on each packaged release; shown in the footer and used by update-check.
APP_VERSION = '1.0.1'


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
    except Exception:
        pass
app.secret_key = secret_key


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
    ],
    'user_settings': [
        ('skip_existing', 'BOOLEAN DEFAULT 1'),
        ('retry_count', 'INTEGER DEFAULT 3'),
        ('privacy_mode', 'BOOLEAN DEFAULT 1'),
        ('job_timeout', 'INTEGER DEFAULT 300'),
        ('bandwidth_limit', 'INTEGER DEFAULT 0'),
        ('proxy', 'VARCHAR(500) DEFAULT ""'),
        ('worker_count', 'INTEGER DEFAULT 3'),
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
    """Copy the database aside before migrations touch it. Keeps `keep`."""
    import datetime
    import shutil
    try:
        if not db_path or db_path == ':memory:' or not os.path.isfile(db_path):
            return None
        backup_dir = os.path.join(os.path.dirname(db_path), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = os.path.join(backup_dir, f'config-{stamp}.db')
        shutil.copy2(db_path, dest)
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
