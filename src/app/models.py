from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os

db = SQLAlchemy()


def default_output_path():
    home = os.path.expanduser('~')
    return os.path.join(home, 'Audio-Converter', 'output')


class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    output_path = db.Column(db.String(500), nullable=False, default=default_output_path)
    wav_sample_rate = db.Column(db.String(10), nullable=False, default='auto')
    wav_bit_depth = db.Column(db.String(3), nullable=False, default='16')
    ogg_quality = db.Column(db.String(4), nullable=False, default='8')
    flac_compression = db.Column(db.String(2), nullable=False, default='5')
    skip_existing = db.Column(db.Boolean, nullable=False, default=True)
    retry_count = db.Column(db.Integer, nullable=False, default=3)
    privacy_mode = db.Column(db.Boolean, nullable=False, default=True)
    # Queue/network tuning (0 bandwidth = unlimited; proxy '' = direct).
    job_timeout = db.Column(db.Integer, nullable=False, default=300)
    bandwidth_limit = db.Column(db.Integer, nullable=False, default=0)
    proxy = db.Column(db.String(500), nullable=False, default='')
    # Parallel downloads; applied live when settings are saved.
    worker_count = db.Column(db.Integer, nullable=False, default=3)
    # Tidal login (all optional). Client id/secret come from the user's own
    # free app at developer.tidal.com; tokens are filled in by the login flow.
    tidal_client_id = db.Column(db.String(200), nullable=False, default='')
    tidal_client_secret = db.Column(db.String(200), nullable=False, default='')
    tidal_access_token = db.Column(db.String(2000), nullable=False, default='')
    tidal_refresh_token = db.Column(db.String(2000), nullable=False, default='')
    tidal_expires_at = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<UserSettings {self.id}>'


class ConversionHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    format = db.Column(db.String(50), nullable=False)
    output_path = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(50), nullable=False, default='pending')
    progress = db.Column(db.Integer, nullable=False, default=0)
    error = db.Column(db.String(1000), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Playlist support: a parent row (is_playlist=True) represents a whole
    # playlist; one child row (parent_id set) is created per track, and all
    # children save into a folder named after the playlist.
    parent_id = db.Column(db.Integer, db.ForeignKey('conversion_history.id'),
                          nullable=True)
    is_playlist = db.Column(db.Boolean, nullable=False, default=False)
    playlist_title = db.Column(db.String(250), nullable=True)
    item_index = db.Column(db.Integer, nullable=False, default=0)
    item_count = db.Column(db.Integer, nullable=False, default=1)
    playlist_organization = db.Column(db.String(20), nullable=False, default='folder')
    retry_attempts = db.Column(db.Integer, nullable=False, default=0)
    # For playlists imported from another service (Spotify/Apple Music): the
    # source service name, e.g. 'spotify'. Empty for native conversions.
    import_source = db.Column(db.String(20), nullable=False, default='')
    # Expected song identity for verification: source-stated duration in
    # seconds (0 = unknown) and 'Artist - Title' ('' = skip the title check).
    expected_duration = db.Column(db.Float, nullable=False, default=0)
    expected_title = db.Column(db.String(250), nullable=False, default='')
    # JSON list of {'artist','title'} import tracks that could not be matched,
    # so misses can be retried later without re-resolving the whole playlist.
    import_misses = db.Column(db.Text, nullable=False, default='[]')

    ACTIVE_STATUSES = ('pending', 'downloading', 'converting')

    def __repr__(self):
        return f'<ConversionHistory {self.id}>'