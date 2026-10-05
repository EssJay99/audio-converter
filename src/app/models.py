from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import os

db = SQLAlchemy()


def utcnow():
    """Naive UTC now: same semantics as the old utcnow() without
    the deprecation. Stays naive so stored timestamps keep comparing."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def default_output_path():
    home = os.path.expanduser('~')
    return os.path.join(home, 'Audio-Converter', 'output')


def effective_output_path():
    """The user's configured output folder, falling back to the default."""
    settings = UserSettings.query.first()
    if settings and settings.output_path:
        return settings.output_path
    return default_output_path()


class UserSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    output_path = db.Column(db.String(500), nullable=False, default=default_output_path)
    wav_sample_rate = db.Column(db.String(10), nullable=False, default='auto')
    wav_bit_depth = db.Column(db.String(3), nullable=False, default='16')
    ogg_quality = db.Column(db.String(4), nullable=False, default='8')
    flac_compression = db.Column(db.String(2), nullable=False, default='5')
    mp3_bitrate = db.Column(db.Integer, nullable=False, default=192)
    m4a_bitrate = db.Column(db.Integer, nullable=False, default=192)
    opus_bitrate = db.Column(db.Integer, nullable=False, default=128)
    skip_existing = db.Column(db.Boolean, nullable=False, default=True)
    retry_count = db.Column(db.Integer, nullable=False, default=3)
    privacy_mode = db.Column(db.Boolean, nullable=False, default=True)
    # Queue/network tuning (0 bandwidth = unlimited; proxy '' = direct).
    job_timeout = db.Column(db.Integer, nullable=False, default=300)
    bandwidth_limit = db.Column(db.Integer, nullable=False, default=0)
    proxy = db.Column(db.String(500), nullable=False, default='')
    # Off-peak override: when the current hour falls in [start, end),
    # this cap replaces bandwidth_limit (0 = no override).
    offpeak_limit = db.Column(db.Integer, nullable=False, default=0)
    offpeak_start = db.Column(db.Integer, nullable=False, default=22)
    offpeak_end = db.Column(db.Integer, nullable=False, default=7)
    # Parallel downloads; applied live when settings are saved.
    worker_count = db.Column(db.Integer, nullable=False, default=3)
    subtitles = db.Column(db.Boolean, nullable=False, default=True)
    sponsorblock = db.Column(db.Boolean, nullable=False, default=True)
    normalize_audio = db.Column(db.Boolean, nullable=False, default=False)
    # Update the yt-dlp helper automatically when a newer release appears.
    auto_update_ytdlp = db.Column(db.Boolean, nullable=False, default=False)
    # Preselected format on the convert forms.
    default_format = db.Column(db.String(20), nullable=False, default='flac')
    # Video encoding defaults (per-submit quality lives on the job).
    video_crf = db.Column(db.Integer, nullable=False, default=23)
    video_preset = db.Column(db.String(20), nullable=False, default='veryfast')
    # Preselected format on the video convert form.
    default_video_format = db.Column(db.String(20), nullable=False, default='video_mp4')
    # Import tuning: preferred engine and strict matching.
    import_engine = db.Column(db.String(20), nullable=False, default='auto')
    import_strict = db.Column(db.Boolean, nullable=False, default=False)
    # Preferred subtitle language for theater auto-select + download order.
    pref_sub_lang = db.Column(db.String(10), nullable=False, default='en')
    # Auto-queue similar tracks when the play queue runs dry.
    radio_mode = db.Column(db.Boolean, nullable=False, default=False)
    # Delete failed rows older than N days (0 = keep).
    prune_failed_days = db.Column(db.Integer, nullable=False, default=0)
    # Keep the library under N GB, oldest-played first (0 = unlimited).
    storage_quota_gb = db.Column(db.Float, nullable=False, default=0)
    # What to do when the queue drains: 'nothing', 'sleep', or 'shutdown'.
    finish_action = db.Column(db.String(10), nullable=False, default='nothing')
    # App version last acknowledged in the What's-new dialog.
    seen_version = db.Column(db.String(20), nullable=False, default='')
    # The welcome tour has been shown (first launch only, never again).
    tour_seen = db.Column(db.Boolean, nullable=False, default=False)
    # Playlist children save as `NN - title` instead of plain titles.
    numbered_filenames = db.Column(db.Boolean, nullable=False, default=False)
    # Write a .nfo sidecar (title/artist/uploader/date/URL) per download.
    nfo_files = db.Column(db.Boolean, nullable=False, default=False)
    # Video download cap: '720p', '1080p', or 'best' (no cap).
    video_quality = db.Column(db.String(10), nullable=False, default='1080p')
    # Sound ceiling for video downloads: 'best' or a kbps cap.
    audio_quality = db.Column(db.String(10), nullable=False, default='best')
    # Tidal login (all optional). Client id/secret come from the user's own
    # free app at developer.tidal.com; tokens are filled in by the login flow.
    tidal_client_id = db.Column(db.String(200), nullable=False, default='')
    tidal_client_secret = db.Column(db.String(200), nullable=False, default='')
    tidal_access_token = db.Column(db.String(2000), nullable=False, default='')
    tidal_refresh_token = db.Column(db.String(2000), nullable=False, default='')
    tidal_expires_at = db.Column(db.Integer, nullable=False, default=0)
    # Desktop niceties.
    desktop_notifications = db.Column(db.Boolean, nullable=False, default=True)
    close_behavior = db.Column(db.String(10), nullable=False, default='ask')
    tray_icon = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, onupdate=utcnow)

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
    # Live download readout ("3.2MiB/s", "00:14"); meaningful only while
    # active, cleared when the job leaves the downloading state.
    dl_speed = db.Column(db.String(20), nullable=False, default='')
    dl_eta = db.Column(db.String(20), nullable=False, default='')
    created_at = db.Column(db.DateTime, default=utcnow)

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
    # Subscription this track belongs to (followed playlists/channels).
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscription.id'),
                               nullable=True)
    # Player library state.
    liked = db.Column(db.Boolean, nullable=False, default=False)
    play_count = db.Column(db.Integer, nullable=False, default=0)
    last_played_at = db.Column(db.DateTime, nullable=True)
    # Length in seconds, probed once at conversion time so listings, stats,
    # and playlists never re-spawn ffmpeg just to read it back.
    duration = db.Column(db.Float, nullable=False, default=0)
    # Embedded tags, stored at conversion time (same reason). Powers album /
    # artist views, top-artist stats, metadata search, and lyrics lookup.
    tag_title = db.Column(db.String(500), nullable=False, default='')
    tag_artist = db.Column(db.String(500), nullable=False, default='')
    tag_album = db.Column(db.String(500), nullable=False, default='')
    # Personal rating, 0 (unrated) through 5.
    rating = db.Column(db.Integer, nullable=False, default=0)
    # Source quality line ("FLAC · 44.1 kHz · stereo") parsed at completion.
    quality = db.Column(db.String(100), nullable=False, default='')
    # Per-submit option overrides (JSON): quality, subtitles, sponsorblock,
    # normalize, embed_subs, embed_cover. Absent keys follow Settings.
    job_options = db.Column(db.Text, nullable=False, default='{}')
    # Source cover-art URL (YouTube/SoundCloud thumbnail), kept so missing
    # artwork can be backfilled later without re-resolving the track.
    cover_url = db.Column(db.String(1000), nullable=False, default='')

    ACTIVE_STATUSES = ('pending', 'downloading', 'converting')

    def __repr__(self):
        return f'<ConversionHistory {self.id}>'


class Subscription(db.Model):
    """A followed playlist/channel: re-checked periodically for new tracks."""
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    format = db.Column(db.String(50), nullable=False)
    output_path = db.Column(db.String(500), nullable=False)
    organization = db.Column(db.String(20), nullable=False, default='folder')
    playlist_title = db.Column(db.String(250), nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('conversion_history.id'),
                          nullable=True)
    interval_hours = db.Column(db.Integer, nullable=False, default=24)
    active = db.Column(db.Boolean, nullable=False, default=True)
    # Follow filters: skip shorts, minimum seconds (0 = off), comma
    # separated title terms (include matches any, exclude matches none).
    min_duration = db.Column(db.Integer, nullable=False, default=0)
    skip_shorts = db.Column(db.Boolean, nullable=False, default=False)
    title_include = db.Column(db.String(500), nullable=False, default='')
    title_exclude = db.Column(db.String(500), nullable=False, default='')
    last_checked = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<Subscription {self.id}>'


class PlayerPlaylist(db.Model):
    """A user-built playlist for the Player tab (not a conversion batch)."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<PlayerPlaylist {self.id}>'


class PlayerPlaylistItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    playlist_id = db.Column(db.Integer, db.ForeignKey('player_playlist.id'),
                            nullable=False)
    conversion_id = db.Column(db.Integer,
                              db.ForeignKey('conversion_history.id'),
                              nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f'<PlayerPlaylistItem {self.id}>'


class Notice(db.Model):
    """Persistent copy of desktop notifications for the in-app bell."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False, default='')
    body = db.Column(db.String(500), nullable=False, default='')
    read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f'<Notice {self.id}>'