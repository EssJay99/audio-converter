import json
import os
import queue
import re
import difflib
import shutil
import subprocess
import sys
import time
import tempfile
import threading
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from flask import (Blueprint, render_template, request, flash, redirect,
                   url_for, jsonify, send_file, abort)
from app.models import db, ConversionHistory, UserSettings

bp = Blueprint('convert', __name__)

VALID_FORMATS = {
    'flac': {'ext': 'flac', 'label': 'FLAC',
             'base_args': ['-c:a', 'flac']},
    'alac': {'ext': 'm4a', 'label': 'ALAC',
             'base_args': ['-c:a', 'alac']},
    'wav': {'ext': 'wav', 'label': 'WAV',
            'base_args': ['-c:a', 'pcm_s16le']},
    'ogg_vorbis': {'ext': 'ogg', 'label': 'OGG Vorbis',
                   'base_args': ['-c:a', 'vorbis', '-q:a', '8', '-strict', 'experimental']},
}

LABEL_TO_KEY = {v['label']: k for k, v in VALID_FORMATS.items()}

# Formats that support embedded cover art
COVER_FORMATS = ('flac', 'alac')

# ---------------------------------------------------------------- queue ----

_conversion_queue = queue.Queue()
# Parallel download workers: several tracks convert at once instead of one
# at a time. SQLite stays safe via WAL mode (see _setup_db) plus the longer
# lock timeout in the engine options.
WORKER_COUNT = 3
WORKER_MIN, WORKER_MAX = 1, 8
_workers_started = False
_worker_lock = threading.Lock()
# Target pool size (mirrors the worker_count setting; changed live on save).
_desired_workers = WORKER_COUNT
# Currently living worker threads; guarded by _pool_lock.
_pool_lock = threading.Lock()
_active_workers = 0
# Set while the queue is paused; workers idle instead of picking up jobs.
_queue_paused = threading.Event()


def _clamp_workers(value, default=WORKER_COUNT):
    try:
        return max(WORKER_MIN, min(WORKER_MAX, int(value)))
    except (TypeError, ValueError):
        return default


def _ensure_worker():
    global _workers_started
    with _worker_lock:
        if not _workers_started:
            try:
                settings = UserSettings.query.first()
                if settings and settings.worker_count:
                    global _desired_workers
                    _desired_workers = _clamp_workers(settings.worker_count)
            except Exception:
                pass
            _spawn_workers(_desired_workers)
            _workers_started = True
        else:
            _spawn_workers(_desired_workers)


def _spawn_workers(count):
    """Start worker threads up to `count` living ones."""
    with _pool_lock:
        living = _active_workers
    missing = count - living
    for index in range(max(0, missing)):
        t = threading.Thread(target=_worker_loop, daemon=True,
                             name=f'audio-worker-{index}')
        t.start()


def set_worker_count(count):
    """Change the pool size live: grows immediately, shrinks as workers idle."""
    global _desired_workers
    _desired_workers = _clamp_workers(count)
    _ensure_worker()
    return _desired_workers


def _worker_loop():
    from app import app as flask_app

    global _active_workers
    with _pool_lock:
        _active_workers += 1
    try:

        while True:
            with _pool_lock:
                if _active_workers > _desired_workers:
                    # Pool was shrunk in Settings; exit after the current job.
                    return
            if _queue_paused.is_set():
                time.sleep(1)
                continue
            try:
                job_id = _conversion_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                with flask_app.app_context():
                    job = db.session.get(ConversionHistory, job_id)
                    if job is None:
                        continue

                    if job.is_playlist:
                        _expand_playlist_job(job)
                        continue

                    if job.status == 'skipped':
                        # Skipped by the user while queued: pass over without
                        # downloading and keep the playlist progress accurate.
                        if job.parent_id:
                            _refresh_playlist_parent(job.parent_id)
                        continue

                    fmt_key = LABEL_TO_KEY.get(job.format)
                    if os.path.isdir(job.output_path):
                        output_dir = job.output_path
                    else:
                        output_dir = os.path.dirname(job.output_path) or job.output_path

                    # Fetch user queue settings once per worker loop iteration
                    _settings = UserSettings.query.first()
                    max_retries = _settings.retry_count if _settings else 3
                    # Per-job timeout to prevent any single track from blocking the queue indefinitely
                    job_timeout = _settings.job_timeout if _settings else 300
                    job_start = None

                    _job_update(job, status='downloading', progress=2)
                    try:
                        job_start = time.time()
                        result = download_and_convert(job.url, fmt_key, output_dir, job)
                        job_elapsed = time.time() - job_start
                        if job_elapsed > job_timeout:
                            raise TimeoutError(f'Job exceeded {job_timeout}s timeout')
                        # Re-read: the user may have skipped this track mid-download.
                        # Terminal updates below are conditional so a concurrent
                        # skip always wins instead of being overwritten.
                        db.session.commit()
                        db.session.refresh(job)
                        if job.status == 'skipped':
                            if result.get('success'):
                                _cleanup(result.get('filepath'))
                            continue
                        if result.get('duplicate'):
                            _job_finish(job, status='skipped', progress=100, error='Already exists on disk',
                                        output_path=result['filepath'])
                        elif result['success']:
                            if not _job_finish(job, status='completed', progress=100,
                                               error=None, output_path=result['filepath']):
                                _cleanup(result['filepath'])
                        else:
                            # Download/conversion failed — check retry budget,
                            # unless the failure can never succeed on retry.
                            error_text = result.get('error') or 'Conversion failed'
                            permanent = 'disk space' in error_text.lower()
                            if job.retry_attempts < max_retries and not permanent:
                                attempts = job.retry_attempts + 1
                                if _job_finish(job, status='pending', progress=0,
                                                retry_attempts=attempts,
                                                error=f'Retry {attempts}/{max_retries}: {result["error"]}'):
                                    _conversion_queue.put(job.id)
                            else:
                                _job_finish(job, status='failed', error=
                                            f'Max retries ({max_retries}) exceeded: {result["error"]}')
                    except TimeoutError as te:
                        # Job exceeded overall time budget — treat as permanent failure
                        _job_finish(job, status='failed', error=f'Timeout: {str(te)}')
                    except Exception as e:
                        # Network/Unexpected error — check retry budget, but only if we haven't exceeded timeout
                        db.session.commit()
                        db.session.refresh(job)
                        if job.status == 'skipped':
                            continue
                        job_elapsed = time.time() - job_start if job_start else 0
                        permanent_errors = ['invalid url', 'video unavailable', 'private video', 'audio format not supported']
                        is_permanent = any(p_err in str(e).lower() for p_err in permanent_errors) or job_elapsed >= job_timeout
                        if job.retry_attempts < max_retries and not is_permanent:
                            attempts = job.retry_attempts + 1
                            if _job_finish(job, status='pending', progress=0,
                                            retry_attempts=attempts,
                                            error=f'Retry {attempts}/{max_retries}: Internal error — will retry'):
                                _conversion_queue.put(job.id)
                        else:
                            _job_finish(job, status='failed', error=
                                        f'Max retries ({max_retries}) exceeded: {str(e) if str(e) else "Permanent error"}')
                    finally:
                        if job.parent_id:
                            _refresh_playlist_parent(job.parent_id)
            except Exception:
                pass
            finally:
                _conversion_queue.task_done()


    finally:
        with _pool_lock:
            _active_workers -= 1


def _expand_playlist_job(job):
    """Turn a playlist job into one pending child row per track.

    All tracks are saved into a folder named after the playlist, created
    under the user's chosen output path. Children are enqueued so the same
    worker machinery (download, convert, progress, per-file records) handles
    every track. Playlists imported from another service (Spotify/Apple
    Music) first have each track resolved to a YouTube URL via search, then
    follow the exact same single-folder flow.
    """
    organization = job.playlist_organization or 'folder'
    fmt_key = LABEL_TO_KEY.get(job.format)
    if fmt_key is None:
        _job_update(job, status='failed', error='Unknown format')
        return

    if os.path.isdir(job.output_path):
        base_dir = job.output_path
    else:
        base_dir = os.path.dirname(job.output_path) or job.output_path

    import_source = ''
    if _is_streaming_playlist_url(job.url):
        try:
            result = resolve_import_urls(job.url)
        except Exception as e:
            _job_update(job, status='failed', error=f'Could not import playlist: {str(e)}')
            return

        if not result.get('success'):
            _job_update(job, status='failed', error=result.get('error') or 'Could not import playlist')
            return

        import_source = result.get('source') or ''
        title = result['title']
        import_note = ''
        try:
            misses_json = json.dumps(result.get('misses') or [])
        except (TypeError, ValueError):
            misses_json = '[]'
        if result.get('total') and result.get('found') is not None and result['found'] < result['total']:
            import_note = (f"Found {result['found']} of {result['total']} tracks online; "
                           f"{result['total'] - result['found']} could not be matched yet")
    else:
        try:
            result = resolve_playlist(job.url)
        except Exception as e:
            _job_update(job, status='failed', error=f'Could not read playlist: {str(e)}')
            return

        if not result.get('success'):
            _job_update(job, status='failed', error=result.get('error') or 'Could not read playlist')
            return

        title = result['title']
        import_note = ''
        misses_json = '[]'

    if organization == 'flat':
        # Save individual tracks directly in the output folder, no subfolder
        folder = job.output_path
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            _job_update(job, status='failed', error=f'Cannot create output directory: {str(e)}')
            return
    else:
        # organization == 'folder': use a playlist-named folder
        folder = unique_folder(base_dir, sanitize_filename(title))
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            _job_update(job, status='failed', error=f'Cannot create playlist folder: {str(e)}')
            return

    urls = result['urls']
    # Imported tracks carry their expected identity for post-download
    # verification; native playlist URLs carry none.
    expectations = {item['url']: item for item in result.get('items', [])}
    # Filter out tracks that already have a completed conversion for this playlist,
    # to avoid re-downloading existing files. This supports incremental playlist updates.
    existing_track_indices = set()
    if job.parent_id:
        # Check existing children: if a child with this item_index is already completed,
        # we skip re-downloading it.
        children = db.session.query(ConversionHistory).filter_by(parent_id=job.id,
                                                                 status='completed').all()
        for child in children:
            existing_track_indices.add(child.item_index)

    # Remove already-downloaded tracks from the expansion
    filtered_urls = []
    for index, url in enumerate(urls):
        if index not in existing_track_indices:
            filtered_urls.append(url)
    urls = filtered_urls

    _job_update(job, status='downloading', progress=0, output_path=folder,
                playlist_title=title, item_count=len(urls),
                import_source=import_source, import_misses=misses_json if import_source else '[]',
                error=import_note or None)

    children = []
    for index, url in enumerate(urls):
        expected = expectations.get(url, {})
        children.append(ConversionHistory(
            url=url,
            format=job.format,
            output_path=folder,
            status='pending',
            progress=0,
            parent_id=job.id,
            is_playlist=False,
            playlist_title=title,
            item_index=index,
            item_count=len(urls),
            expected_title=expected.get('expected_title', ''),
            expected_duration=expected.get('expected_duration', 0),
        ))
    db.session.add_all(children)
    db.session.commit()
    for child in children:
        _conversion_queue.put(child.id)


def _refresh_playlist_parent(parent_id):
    """Recompute a playlist parent's status/progress from its children."""
    parent = db.session.get(ConversionHistory, parent_id)
    if parent is None:
        return

    children = db.session.query(ConversionHistory).filter_by(parent_id=parent_id).all()
    total = len(children) or parent.item_count or 1
    finished = sum(1 for c in children if c.status in ('completed', 'failed', 'skipped'))
    failed = sum(1 for c in children if c.status == 'failed')
    skipped = [c for c in children if c.status == 'skipped']
    dup_skipped = sum(1 for c in skipped if (c.error or '') != 'Skipped by user')
    user_skipped = len(skipped) - dup_skipped

    parent.item_count = total
    parent.progress = min(100, round(100 * finished / total))
    if finished >= total:
        if failed == total:
            parent.status = 'failed'
            parent.error = f'All {failed} track(s) failed to convert'
        else:
            parent.status = 'completed'
            parts = []
            if failed:
                parts.append(f'{failed} of {total} track(s) failed')
            if dup_skipped:
                parts.append(f'{dup_skipped} already existed')
            if user_skipped:
                parts.append(f'{user_skipped} skipped')
            parent.error = '; '.join(parts) if parts else None
    db.session.commit()


def _job_update(job, **fields):
    for key, value in fields.items():
        setattr(job, key, value)
    db.session.commit()


def _job_finish(job, **fields):
    """Apply a terminal update only if the job is still actively converting.

    The write is a single conditional UPDATE, so a user skip landing at the
    same moment wins instead of being overwritten. Returns True when the
    update was applied, False when the job had already moved on.
    """
    count = db.session.query(ConversionHistory).filter(
        ConversionHistory.id == job.id,
        ConversionHistory.status.in_(
            list(ConversionHistory.ACTIVE_STATUSES)),
    ).update(fields, synchronize_session=False)
    db.session.commit()
    return count > 0


# ----------------------------------------------------------------- routes --

@bp.route('/convert')
def convert_page():
    """Show the conversion page."""
    user_settings = UserSettings.query.first()
    default_output_path = get_default_output_path()
    if user_settings:
        default_output_path = user_settings.output_path
    return render_template('convert.html',
                           default_output_path=default_output_path,
                           request_path='/convert')


@bp.route('/convert', methods=['POST'])
def convert():
    """Queue a conversion job."""
    raw_url = request.form.get('url', '').strip()
    # Privacy mode strips tracking identifiers from pasted links; with it
    # off the URL goes through untouched for maximum compatibility.
    url = sanitize_url(raw_url) if _privacy_on() else raw_url
    format_type = request.form.get('format', '').strip()
    output_path = request.form.get('output_path', '').strip()
    organization = request.form.get('organization', '').strip() or 'folder'
    if organization == 'auto':
        # Retired option kept for old rows; behaves like 'folder'.
        organization = 'folder'

    if not url or not format_type:
        flash('Please provide both a URL and a format', 'error')
        return redirect(url_for('home.index'))

    if not output_path:
        output_path = get_default_output_path()

    if not is_valid_format(format_type):
        flash('Invalid format selected. Use "flac", "alac", "wav", or "ogg_vorbis"', 'error')
        return redirect(url_for('home.index'))

    try:
        os.makedirs(output_path, exist_ok=True)
        if not os.access(output_path, os.W_OK):
            flash(f'Output directory is not writable: {output_path}', 'error')
            return redirect(url_for('home.index'))
    except Exception as e:
        flash(f'Cannot create output directory: {str(e)}', 'error')
        return redirect(url_for('home.index'))

    is_import = _is_streaming_playlist_url(url)
    if is_import or _is_collection_url(url):
        # Check if a playlist with this URL already exists and is in a resumable state
        existing = db.session.query(ConversionHistory).filter(
            ConversionHistory.url == url,
            ConversionHistory.is_playlist == True,
            ConversionHistory.status.in_(['pending', 'downloading', 'converting'])
        ).first()

        if existing:
            # Resume existing playlist - update its output_path if needed
            if existing.output_path != output_path:
                existing.output_path = output_path
                db.session.commit()
            history_id = existing.id
            flash('Resuming existing playlist from last session.', 'info')
        else:
            history_id = queue_playlist(url, format_type, output_path, organization)
            if is_import:
                flash('Playlist import queued. Tracks will be found on YouTube and saved into one folder.', 'info')
            else:
                flash('Collection queued. Each track will be saved according to your organization preference.', 'info')
        return redirect(url_for('home.index'))

    history = ConversionHistory(
        url=url,
        format=VALID_FORMATS[format_type]['label'],
        output_path=output_path,
        status='pending',
        progress=0,
    )
    db.session.add(history)
    db.session.commit()

    _conversion_queue.put(history.id)
    _ensure_worker()

    flash('Conversion queued. Track progress on the home page.', 'info')
    return redirect(url_for('home.index'))


def queue_playlist(url, format_type, output_path, organization='folder'):
    """Create the parent playlist row and hand it to the worker for expansion."""
    organization = organization or 'folder'
    history = ConversionHistory(
        url=url,
        format=VALID_FORMATS[format_type]['label'],
        output_path=output_path,
        status='pending',
        progress=0,
        is_playlist=True,
        item_count=0,
        playlist_organization=organization,
    )
    db.session.add(history)
    db.session.commit()

    _conversion_queue.put(history.id)
    _ensure_worker()
    return history.id


@bp.route('/convert/status', methods=['GET'])
def convert_status():
    """Get conversion status from database."""
    url = request.args.get('url', '')

    if url:
        history = db.session.query(ConversionHistory).filter_by(url=url) \
            .order_by(ConversionHistory.created_at.desc()).first()

        if history:
            return jsonify(_serialize(history))

    return jsonify({'status': 'not_found', 'message': 'No conversion found for this URL'}), 404


@bp.route('/convert/recent', methods=['GET'])
def recent_conversions():
    """Get list of recent conversions for display."""
    limit = request.args.get('limit', 10, type=int)
    if limit > 100:
        limit = 100

    history = db.session.query(ConversionHistory) \
        .order_by(ConversionHistory.created_at.desc()).limit(limit).all()

    return jsonify([_serialize(item) for item in history])


@bp.route('/download/<int:conversion_id>')
def download_file(conversion_id):
    """Stream a completed/skipped conversion to the browser."""
    history = db.session.get(ConversionHistory, conversion_id)

    if not history or history.status not in ('completed', 'skipped'):
        abort(404)
    if not os.path.isfile(history.output_path):
        abort(404)

    return send_file(
        history.output_path,
        as_attachment=True,
        download_name=os.path.basename(history.output_path),
    )


@bp.route('/audio/<int:conversion_id>')
def audio_stream(conversion_id):
    """Stream a converted file with HTTP Range support so the in-app player can seek."""
    history = db.session.get(ConversionHistory, conversion_id)

    if not history or history.status not in ('completed', 'skipped'):
        abort(404)
    if not os.path.isfile(history.output_path):
        abort(404)

    return send_file(history.output_path, conditional=True)


@bp.route('/api/track/<int:conversion_id>')
def api_track(conversion_id):
    """Return a serialized conversion plus ffprobe metadata for the player."""
    history = db.session.get(ConversionHistory, conversion_id)

    if not history or history.status not in ('completed', 'skipped'):
        return jsonify({'ok': False, 'message': 'No playable file'}), 404
    if not os.path.isfile(history.output_path):
        return jsonify({'ok': False, 'message': 'File no longer exists on disk'}), 404

    return jsonify({
        'ok': True,
        'item': _serialize(history),
        'meta': _probe_metadata(history.output_path),
    })


def _playable_file_or_404(conversion_id):
    """Fetch a finished conversion whose file is still on disk, or None."""
    history = db.session.get(ConversionHistory, conversion_id)
    if not history or history.status not in ('completed', 'skipped'):
        return None
    if not history.output_path or not os.path.isfile(history.output_path):
        return None
    return history


@bp.route('/api/rename/<int:conversion_id>', methods=['POST'])
def rename_file(conversion_id):
    """Rename a converted file (name only; it stays in its folder)."""
    history = _playable_file_or_404(conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'No playable file'}), 404

    payload = request.get_json(silent=True) or {}
    if not str(payload.get('name') or '').strip():
        return jsonify({'ok': False, 'message': 'Provide a new file name'}), 400
    name = sanitize_filename(str(payload.get('name')))[:100]
    if not name:
        return jsonify({'ok': False, 'message': 'Provide a new file name'}), 400

    directory = os.path.dirname(history.output_path)
    _base, ext = os.path.splitext(os.path.basename(history.output_path))
    target = os.path.join(directory, name + ext.lower())
    if os.path.abspath(target) == os.path.abspath(history.output_path):
        return jsonify({'ok': True, 'filename': os.path.basename(target)})
    if os.path.exists(target):
        return jsonify({'ok': False,
                        'message': 'A file with that name already exists'}), 400
    try:
        os.rename(history.output_path, target)
    except OSError as e:
        return jsonify({'ok': False, 'message': f'Could not rename: {str(e)}'}), 500
    history.output_path = target
    db.session.commit()
    return jsonify({'ok': True, 'filename': os.path.basename(target),
                    'output_path': target})


@bp.route('/api/metadata/<int:conversion_id>', methods=['POST'])
def edit_metadata(conversion_id):
    """Rewrite a converted file's embedded title/artist/album tags."""
    history = _playable_file_or_404(conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'No playable file'}), 404

    payload = request.get_json(silent=True) or {}
    tags = {key: str(payload.get(key) or '').strip()[:100]
            for key in ('title', 'artist', 'album')}
    if not any(tags.values()):
        return jsonify({'ok': False, 'message': 'Provide at least one tag'}), 400

    fd, tmp_path = tempfile.mkstemp(
        suffix=os.path.splitext(history.output_path)[1] or '.tmp')
    os.close(fd)
    try:
        args = ['ffmpeg', '-y', '-i', history.output_path, '-map', '0',
                '-c', 'copy']
        for key, value in tags.items():
            if value:
                args += ['-metadata', f'{key}={value}']
        args.append(tmp_path)
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=120)
        if result.returncode != 0 or not os.path.exists(tmp_path):
            return jsonify({'ok': False,
                            'message': 'Could not write tags to this file'}), 500
        os.replace(tmp_path, history.output_path)
    except Exception as e:
        _cleanup(tmp_path)
        return jsonify({'ok': False, 'message': str(e)}), 500
    return jsonify({'ok': True, 'meta': _probe_metadata(history.output_path)})


@bp.route('/api/cover/<int:conversion_id>')
def cover_art(conversion_id):
    """Serve a converted file's cover art (cached per file).

    Prefers art embedded in the file; falls back to the `<track>.cover.jpg`
    sidecar written for formats whose containers cannot hold pictures.
    """
    history = _playable_file_or_404(conversion_id)
    if not history:
        abort(404)

    cache_dir = os.path.join(tempfile.gettempdir(), 'audio-converter-covers')
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        abort(404)
    cached = os.path.join(cache_dir, f'{history.id}.jpg')
    try:
        fresh = (os.path.isfile(cached) and
                 os.path.getmtime(cached) >= os.path.getmtime(history.output_path))
    except OSError:
        fresh = False
    if not fresh:
        result = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-i', history.output_path,
             '-an', '-vcodec', 'copy', cached],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not os.path.isfile(cached):
            _cleanup(cached)
    if not os.path.isfile(cached):
        sidecar = os.path.splitext(history.output_path)[0] + '.cover.jpg'
        if os.path.isfile(sidecar):
            return send_file(sidecar, mimetype='image/jpeg')
        abort(404)
    return send_file(cached, mimetype='image/jpeg')


# ------------------------------------------------------ conversion engine --

def download_and_convert(url, format_type, output_path, job=None):
    """Download audio and convert it to the requested lossless format."""
    meta = extract_metadata(url)
    title = meta.get('title') or _source_name(url)

    # Remember the source's stated duration so the finished file can be
    # checked for truncation (native conversions learn it here; imported
    # tracks already carry it from the playlist extraction).
    if job is not None and not getattr(job, 'expected_duration', 0):
        try:
            if meta.get('duration'):
                _job_update(job, expected_duration=float(meta['duration']))
        except Exception:
            pass

    fmt = VALID_FORMATS[format_type]
    target_name = sanitize_filename(title) + '.' + fmt['ext']

    if job and _should_skip_duplicates():
        dup = find_duplicate(output_path, target_name)
        if dup:
            return {'success': False, 'duplicate': True, 'filepath': dup, 'filename': os.path.basename(dup)}

    if job and not _disk_ok(output_path):
        return {'success': False, 'error': 'Not enough free disk space (200 MB needed)'}

    output_file = unique_file_path(output_path, target_name)

    stem = os.path.splitext(os.path.basename(output_file))[0]
    temp_audio = os.path.join(output_path, f".{stem}_{job.id if job else 'x'}.tmp")
    cover_file = None

    try:
        if not check_ffmpeg():
            return {'success': False, 'error': 'FFmpeg not found! Install it first.'}

        _job_update(job, status='downloading', progress=5) if job else None

        downloaded_from = url
        dl = download_audio(url, temp_audio, job)
        if not dl.get('success'):
            # The same recording almost always lives on the other platform,
            # so hunt it there (and among sibling uploads) before giving up.
            expected_title = ''
            if job is not None:
                expected_title = getattr(job, 'expected_title', '') or ''
            if not expected_title:
                expected_title = meta.get('title') or ''
            for alternate in find_alternate_sources(url, meta, expected_title):
                alt_meta = extract_metadata(alternate)
                if expected_title and not _titles_match(
                        expected_title, alt_meta.get('title', '')):
                    continue
                if job is not None:
                    _job_update(job, status='downloading', progress=5)
                dl = download_audio(alternate, temp_audio, job)
                if dl.get('success'):
                    downloaded_from = alternate
                    if alt_meta.get('title'):
                        meta = alt_meta
                    break
        if not dl.get('success'):
            return dl
        if not os.path.exists(temp_audio):
            return {'success': False, 'error': 'Downloaded file not found'}

        cover_file = _fetch_thumbnail(meta.get('thumbnail'))

        _job_update(job, status='converting', progress=95) if job else None

        result = convert_audio_file(temp_audio, format_type, output_file, meta, cover_file)
        if not result.get('success'):
            return result

        # FLAC/ALAC carry the art inside the file; for OGG/WAV the art is
        # saved next to the track where players (and our own cover endpoint)
        # look for it.
        if cover_file and format_type not in COVER_FORMATS:
            sidecar = os.path.splitext(output_file)[0] + '.cover.jpg'
            try:
                shutil.copyfile(cover_file, sidecar)
            except OSError:
                pass

        expected_duration = getattr(job, 'expected_duration', 0) if job else 0
        expected_title = getattr(job, 'expected_title', '') if job else ''
        ok, reason = _verify_output(output_file,
                                    expected_duration=expected_duration or None,
                                    expected_title=expected_title or None)
        if not ok:
            _cleanup(output_file)
            return {'success': False, 'error': reason}

        return {'success': True, 'filepath': output_file, 'filename': os.path.basename(output_file),
                'via': downloaded_from}

    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Conversion timed out'}
    except FileNotFoundError:
        return {'success': False, 'error': 'yt-dlp not found! Install it: pip install yt-dlp'}
    except Exception as e:
        return {'success': False, 'error': f'Conversion failed: {str(e)}'}
    finally:
        _cleanup(temp_audio)
        _cleanup(cover_file)


def extract_metadata(url):
    """Fetch title/artist/album/duration/thumbnail metadata via yt-dlp."""
    meta = {}
    result = _run_ytdlp(
        ['--no-playlist', '--skip-download', '--no-warnings', '--dump-single-json', url],
        url, timeout=60
    )

    if result.returncode == 0 and result.stdout.strip():
        try:
            info = json.loads(result.stdout)
            meta['title'] = sanitize_filename(str(info.get('title') or ''))[:100]
            meta['artist'] = str(info.get('artist') or info.get('uploader') or
                                 info.get('creator') or '').strip()[:100]
            meta['album'] = str(info.get('album') or '').strip()[:100]
            try:
                meta['duration'] = float(info.get('duration') or 0)
            except (TypeError, ValueError):
                meta['duration'] = 0
            upload_date = str(info.get('upload_date') or '')
            meta['date'] = upload_date[:4]
            thumbnail = str(info.get('thumbnail') or '').strip()
            meta['thumbnail'] = thumbnail if thumbnail else ''
        except json.JSONDecodeError:
            pass

    return meta


def _source_name(url):
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'YouTube Audio'
    if 'soundcloud.com' in url:
        return 'SoundCloud Audio'
    return 'Audio'


# Query params that carry no playback meaning but do carry tracking identity
# (share tokens, ad clicks, campaign tags, player UI state). These are stripped
# before a URL is stored or requested so pasted share links don't hand
# YouTube/SoundCloud extra identifiers.
_TRACKING_PARAMS = frozenset({
    'si', 'feature', 'pp', 'fbclid', 'gclid', 'gclsrc', 'msclkid',
    'igshid', 'mc_cid', 'mc_eid', 'vero_id', 'ref', 'ref_src',
})

# Query params that actually address content and must be preserved.
_YT_KEEP_PARAMS = frozenset({'v', 'list', 'index', 't', 'start', 'end'})
_SC_KEEP_PARAMS = frozenset()


def sanitize_url(url):
    """Strip tracking/ad identifiers from a pasted URL, keeping content address.

    Safe to apply to stored URLs too: the same input always produces the same
    output, so resume/duplicate matching stays consistent.
    """
    raw = (url or '').strip()
    if not raw:
        return raw
    try:
        parts = urlparse(raw)
    except Exception:
        return raw
    host = (parts.netloc or '').lower()
    if 'youtu.be' in host:
        keep = {'t', 'start', 'end'}
    elif 'youtube.com' in host or 'music.youtube.com' in host:
        keep = _YT_KEEP_PARAMS
    elif 'soundcloud.com' in host:
        keep = _SC_KEEP_PARAMS
    else:
        keep = set()
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except Exception:
        return raw
    cleaned = [(k, v) for k, v in pairs
               if k in keep and not k.startswith('utm_') and k not in _TRACKING_PARAMS]
    return urlunparse((parts.scheme, parts.netloc, parts.path,
                       parts.params, urlencode(cleaned), ''))


def _fetch_thumbnail(url):
    """Download a cover image to a temp file, or return None.

    Uses a fresh, cookieless session with minimal headers so the image CDN
    gets no cookies, no referer, and no browser-like fingerprint.
    """
    if not url:
        return None
    if not _privacy_on():
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and resp.content:
                fd, path = tempfile.mkstemp(suffix='.jpg')
                with os.fdopen(fd, 'wb') as f:
                    f.write(resp.content)
                return path
        except Exception:
            pass
        return None
    try:
        session = requests.Session()
        session.cookies.clear()
        proxies = _http_proxies()
        resp = session.get(url, timeout=15, proxies=proxies, headers={
            'Accept': 'image/*',
            'User-Agent': 'AudioConverter/1.0',
        })
        if resp.status_code == 200 and resp.content:
            fd, path = tempfile.mkstemp(suffix='.jpg')
            with os.fdopen(fd, 'wb') as f:
                f.write(resp.content)
            return path
    except Exception:
        pass
    return None


def convert_audio_file(input_file, format_type, output_file, meta, cover_file):
    """Convert audio to the target format with metadata and cover art."""
    args = ['ffmpeg', '-y']
    if cover_file and format_type in COVER_FORMATS:
        args += ['-i', input_file, '-i', cover_file]
    else:
        args += ['-i', input_file]

    args += ['-map', '0:a']
    if cover_file and format_type in COVER_FORMATS:
        args += ['-map', '1:v', '-c:v', 'copy', '-disposition:v', 'attached_pic']

    args += _format_args(format_type)

    for key in ('title', 'artist', 'album', 'date'):
        value = meta.get(key)
        if value:
            args += ['-metadata', f'{key}={value}']

    args.append(output_file)
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)

    if result.returncode == 0 and os.path.exists(output_file):
        return {'success': True}
    else:
        error_msg = result.stderr.strip()[-500:] if result.stderr else 'FFmpeg returned non-zero exit code'
        return {'success': False, 'error': f'FFmpeg conversion failed: {error_msg}'}


def _format_args(format_type):
    """Build the ffmpeg audio codec args from the user's format settings."""
    settings = UserSettings.query.first()
    fmt = VALID_FORMATS[format_type]
    args = list(fmt['base_args'])

    if format_type == 'flac':
        level = str(getattr(settings, 'flac_compression', None) or 5)
        args = ['-c:a', 'flac', '-compression_level', level]
    elif format_type == 'wav':
        depth = str(getattr(settings, 'wav_bit_depth', None) or 16)
        rate = str(getattr(settings, 'wav_sample_rate', None) or 'auto')
        codec = {'16': 'pcm_s16le', '24': 'pcm_s24le', '32': 'pcm_s32le'}.get(depth, 'pcm_s16le')
        args = ['-c:a', codec]
        if rate and rate != 'auto':
            args += ['-ar', rate]
    elif format_type == 'ogg_vorbis':
        quality = str(getattr(settings, 'ogg_quality', None) or 8)
        args = ['-c:a', 'vorbis', '-q:a', quality, '-strict', 'experimental']

    return args


def unique_file_path(directory, filename):
    """Return a file path that doesn't already exist, appending (2), (3), etc."""
    name, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{name} ({counter}){ext}")
        counter += 1
    return candidate


def unique_folder(directory, name):
    """Return a folder path that doesn't already exist, appending (2), (3)."""
    candidate = os.path.join(directory, name)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{name} ({counter})")
        counter += 1
    return candidate


def sanitize_filename(name):
    """Sanitize a filename to be filesystem-safe."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.rstrip('.')
    return name or 'audio'


# ------------------------------------------------------------- yt-dlp run --

def _find_ytdlp():
    exe = shutil.which('yt-dlp')
    if exe:
        return exe

    bin_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(bin_dir, 'yt-dlp')
    if os.path.exists(candidate):
        return candidate

    return None


def _ytdlp_command():
    exe = _find_ytdlp()
    if exe:
        return [exe]
    return [sys.executable, '-m', 'yt_dlp']


# Flags prepended to every yt-dlp invocation so downloads never touch
# cookies, a persistent cache, watch history, or metadata sidecars.
_YTDLP_PRIVACY_FLAGS = [
    '--no-cookies',
    '--no-cache-dir',
    '--no-mtime',
    '--no-write-info-json',
    '--no-write-playlist-metafiles',
    '--no-mark-watched',
]


def _privacy_on():
    """Master privacy switch (default ON). OFF trades tracking resistance
    for maximum compatibility with stubborn videos."""
    try:
        settings = UserSettings.query.first()
        if settings is None:
            return True
        return bool(getattr(settings, 'privacy_mode', True))
    except Exception:
        return True


def _active_privacy_flags():
    return list(_YTDLP_PRIVACY_FLAGS) if _privacy_on() else []

# Fail fast instead of downloading into a full disk.
MIN_FREE_BYTES = 200 * 1024 * 1024


def _get_proxy():
    """Configured proxy URL, or '' for a direct connection. Context-safe."""
    try:
        settings = UserSettings.query.first()
        return (getattr(settings, 'proxy', '') or '').strip()
    except Exception:
        return ''


def _http_proxies():
    """Proxy mapping for requests, or None for a direct connection."""
    proxy = _get_proxy()
    return {'http': proxy, 'https': proxy} if proxy else None


def _ytdlp_net_args():
    """Proxy + speed-limit flags for yt-dlp. Safe without an app context."""
    args = []
    proxy = _get_proxy()
    if proxy:
        args += ['--proxy', proxy]
    try:
        settings = UserSettings.query.first()
        limit = int(getattr(settings, 'bandwidth_limit', 0) or 0)
    except Exception:
        limit = 0
    if limit > 0:
        args += ['--limit-rate', f'{limit}K']
    return args


def _disk_ok(directory):
    """True when the disk holding `directory` has room for a conversion."""
    try:
        return shutil.disk_usage(directory).free >= MIN_FREE_BYTES
    except OSError:
        # Path problems surface as their own errors elsewhere.
        return True


def _is_youtube(url):
    return 'youtube.com' in url or 'youtu.be' in url


def _run_ytdlp(args, url, timeout):
    """Run yt-dlp with privacy-hardened defaults.

    Every invocation gets flags that avoid persistent identifiers and
    needless data leakage:
    - ``--no-cookies`` / ``--no-cache-dir``: never read or write browser
      cookies or a cache dir that could fingerprint this machine.
    - ``--no-mtime``: don't stamp files with server-provided timestamps.
    - ``--no-write-info-json`` / ``--no-write-playlist-metafiles``: don't
      litter metadata sidecars next to downloads.
    - ``--no-mark-watched``: never report watch history back.
    """
    privacy = _active_privacy_flags() + _ytdlp_net_args()
    attempts = [privacy + list(args)]
    if _is_youtube(url) and '--extractor-args' not in args:
        attempts.append(privacy + [
            '--extractor-args',
            'youtube:player_client=android_vr,tv,web_embedded',
            '--extractor-args',
            'youtube:player_skip=html5',
        ] + list(args))

    last_result = None
    for cmd_args in attempts:
        result = subprocess.run(_ytdlp_command() + cmd_args,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result
        last_result = result

    return last_result


def download_audio(url, temp_audio, job=None):
    """Download best-quality audio, reporting live progress. Returns success dict."""
    base = (_active_privacy_flags() + _ytdlp_net_args()
            + ['--no-playlist', '-f', 'bestaudio/best', '--newline', '-o', temp_audio])
    cmd = _ytdlp_command() + base + [url]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in iter(proc.stdout.readline, ''):
            match = re.search(r'(?:^|\s)\[download\]', line)
            if match and '%' in line:
                pct_match = re.search(r'(\d+(?:\.\d+)?)%', line)
                if pct_match and job:
                    pct = min(90, 5 + float(pct_match.group(1)) * 0.85)
                    _job_update(job, progress=int(pct))
        proc.wait(timeout=300)
    except KeyboardInterrupt:
        proc.kill()
        raise
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise

    if proc.returncode != 0 and _is_youtube(url):
        fallback = (_active_privacy_flags() + _ytdlp_net_args()
                    + ['--no-playlist', '-f', 'bestaudio/best', '-o', temp_audio,
                       '--extractor-args', 'youtube:player_client=android_vr,tv,web_embedded', url])
        retry = subprocess.run(_ytdlp_command() + fallback,
                               capture_output=True, text=True, timeout=300)
        if retry.returncode == 0:
            if os.path.exists(temp_audio):
                return {'success': True}
            return {'success': False, 'error': 'No audio file was downloaded'}

    if proc.returncode != 0:
        return {'success': False, 'error': 'yt-dlp download failed'}

    if os.path.exists(temp_audio):
        return {'success': True}

    return {'success': False, 'error': 'No audio file was downloaded'}


# -------------------------------------------------------------- helpers ----

def check_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ffmpeg_version():
    try:
        res = subprocess.run(['ffmpeg', '-version'], capture_output=True,
                             text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            return res.stdout.strip().split('\n')[0][:120]
    except Exception:
        pass
    return ''


def _ytdlp_version():
    exe = _find_ytdlp()
    if not exe:
        return ''
    try:
        res = subprocess.run([exe, '--version'], capture_output=True,
                             text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().split()[0][:40]
    except Exception:
        pass
    return ''


def _latest_ytdlp_release():
    """Latest upstream yt-dlp release: {'version', 'assets': {name: url}}."""
    try:
        resp = requests.get(
            'https://api.github.com/yt-dlp/yt-dlp/releases/latest',
            timeout=15, headers={'Accept': 'application/vnd.github+json',
                                 'User-Agent': 'AudioConverter/1.0'})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    assets = {}
    for asset in data.get('assets') or []:
        name = asset.get('name')
        url = asset.get('browser_download_url')
        if name and url:
            assets[name] = url
    version = str(data.get('tag_name') or '').strip()
    if not version:
        return None
    return {'version': version, 'assets': assets}


def _version_tuple(value):
    try:
        return tuple(int(part) for part in
                     str(value).strip().lstrip('v').split('.')[:4])
    except (TypeError, ValueError):
        return ()


@bp.route('/api/helpers')
def api_helpers():
    """Installed helper versions plus whether a newer yt-dlp release exists."""
    current = _ytdlp_version()
    latest = _latest_ytdlp_release()
    update_available = bool(
        latest and current and _version_tuple(latest['version']) > _version_tuple(current))
    return jsonify({
        'ok': True,
        'ffmpeg': _ffmpeg_version(),
        'ytdlp': {'current': current,
                  'latest': latest['version'] if latest else None,
                  'update_available': update_available},
    })


@bp.route('/api/helpers/update-ytdlp', methods=['POST'])
def api_update_ytdlp():
    """Replace the yt-dlp helper binary with the latest release build.

    Only touches the standalone binary (bundled app or a PATH install that
    is a real file we can write); pip-based installs are left alone with
    guidance instead of a risky in-place pip upgrade.
    """
    exe = _find_ytdlp()
    if not exe or not os.path.isfile(exe):
        return jsonify({'ok': False,
                        'message': 'No standalone yt-dlp binary found to update'}), 400
    try:
        if not os.access(exe, os.W_OK):
            raise OSError('not writable')
    except OSError:
        return jsonify({'ok': False,
                        'message': 'yt-dlp is managed by pip here; run: pip install -U yt-dlp'}), 400

    if sys.platform == 'darwin':
        asset = 'yt-dlp_macos'
    elif sys.platform.startswith('win'):
        asset = 'yt-dlp.exe'
    else:
        asset = 'yt-dlp_linux'

    latest = _latest_ytdlp_release()
    if not latest or asset not in latest.get('assets', {}):
        return jsonify({'ok': False,
                        'message': 'Could not find a fresh yt-dlp build'}), 502
    try:
        session = requests.Session()
        session.cookies.clear()
        resp = session.get(latest['assets'][asset], timeout=300,
                           headers={'User-Agent': 'AudioConverter/1.0'})
        if resp.status_code != 200 or not resp.content:
            raise ValueError('download failed')
        fd, tmp_path = tempfile.mkstemp(prefix='ytdlp-update-')
        with os.fdopen(fd, 'wb') as f:
            f.write(resp.content)
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, exe)
    except Exception as e:
        _cleanup(locals().get('tmp_path', ''))
        return jsonify({'ok': False, 'message': f'Update failed: {str(e)}'}), 500
    return jsonify({'ok': True, 'version': latest['version'],
                    'message': f"yt-dlp updated to {latest['version']}."})


def is_valid_format(format_type):
    return format_type.lower() in VALID_FORMATS


def get_default_output_path():
    home = os.path.expanduser('~')
    return os.path.join(home, 'Audio-Converter', 'output')


def _serialize(item):
    saved_path = item.output_path if item.status in ('completed', 'skipped') else ''
    try:
        missed = len(json.loads(item.import_misses or '[]'))
    except (ValueError, TypeError):
        missed = 0
    return {
        'id': item.id,
        'url': item.url[:100] + ('...' if len(item.url) > 100 else ''),
        'format': item.format,
        'status': item.status,
        'progress': item.progress,
        'output_path': item.output_path,
        # The full path the finished file was saved to, its file name, and
        # whether that file still exists on disk (it may have been moved).
        'filename': os.path.basename(saved_path) if saved_path else '',
        'file_exists': bool(saved_path and os.path.isfile(saved_path)),
        # Playlist grouping. Parent rows have is_playlist=True and no file;
        # child rows point at their parent and carry their track index.
        'parent_id': item.parent_id,
        'is_playlist': bool(item.is_playlist),
        'playlist_title': item.playlist_title or '',
        'import_source': item.import_source or '',
        'missed_count': missed,
        'item_index': item.item_index,
        'item_count': item.item_count,
        'retry_attempts': item.retry_attempts,
        'error': item.error or '',
        'created_at': str(item.created_at) if item.created_at else '',
    }


@bp.route('/api/reveal/<int:conversion_id>')
def reveal_file(conversion_id):
    """Reveal the saved file or playlist folder in the OS file manager."""
    history = db.session.get(ConversionHistory, conversion_id)

    if not history:
        return jsonify({'ok': False, 'message': 'Conversion is not complete'}), 404
    if history.status != 'completed' and not (history.status == 'skipped' and os.path.isfile(history.output_path or '')):
        return jsonify({'ok': False, 'message': 'Conversion is not complete'}), 404
    path = history.output_path or ''
    if not (os.path.isfile(path) or os.path.isdir(path)):
        return jsonify({'ok': False, 'message': 'File no longer exists on disk'}), 404

    try:
        if sys.platform == 'darwin':
            cmd = ['open', path] if os.path.isdir(path) else ['open', '-R', path]
        elif sys.platform.startswith('win'):
            cmd = ['explorer', path] if os.path.isdir(path) else ['explorer', '/select,', path]
        else:
            cmd = ['xdg-open', path if os.path.isdir(path) else os.path.dirname(path)]
        subprocess.Popen(cmd)
        return jsonify({'ok': True, 'message': 'Opened in your file manager'})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500


@bp.route('/api/queue/pause')
def api_queue_pause():
    """Pause the download queue; workers idle until resumed."""
    _queue_paused.set()
    return jsonify({'ok': True, 'paused': True})


@bp.route('/api/queue/resume')
def api_queue_resume():
    """Resume a paused download queue."""
    _queue_paused.clear()
    return jsonify({'ok': True, 'paused': False})


@bp.route('/api/queue/status')
def api_queue_status():
    """Queue state for the toolbar: paused flag plus waiting jobs."""
    return jsonify({'ok': True, 'paused': _queue_paused.is_set(),
                    'pending': _conversion_queue.qsize()})


@bp.route('/api/skip/<int:conversion_id>')
def skip_job(conversion_id):
    """Skip a queued/active track, or every remaining track of a playlist.

    A skipped track counts as finished for its playlist's progress, and the
    worker will not (re)download it: jobs already in the queue are left for
    the worker to pass over, and an in-flight download is discarded when it
    finishes.
    """
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404

    if history.is_playlist:
        children = db.session.query(ConversionHistory).filter_by(
            parent_id=history.id).all()
        active = [c for c in children
                  if c.status in ConversionHistory.ACTIVE_STATUSES]
        if not active:
            return jsonify({'ok': False,
                            'message': 'Playlist has no active tracks to skip'}), 400
        for child in active:
            child.status = 'skipped'
            child.progress = 100
            child.error = 'Skipped by user'
        db.session.commit()
        _refresh_playlist_parent(history.id)
        return jsonify({'ok': True, 'skipped': len(active)})

    if history.status not in ConversionHistory.ACTIVE_STATUSES:
        return jsonify({'ok': False,
                        'message': 'Only queued or active conversions can be skipped'}), 400

    history.status = 'skipped'
    history.progress = 100
    history.error = 'Skipped by user'
    db.session.commit()
    if history.parent_id:
        _refresh_playlist_parent(history.parent_id)
    return jsonify({'ok': True, 'skipped': 1})


@bp.route('/api/delete-playlist/<int:parent_id>', methods=['POST'])
def delete_playlist(parent_id):
    """Delete a whole playlist: every finished track file, all child rows,
    then the parent row. Active tracks are left alone and reported."""
    parent = db.session.get(ConversionHistory, parent_id)
    if not parent or not parent.is_playlist:
        return jsonify({'ok': False, 'message': 'Playlist not found'}), 404

    children = db.session.query(ConversionHistory).filter_by(
        parent_id=parent.id).all()
    active = [c for c in children
              if c.status in ConversionHistory.ACTIVE_STATUSES]
    if active:
        return jsonify({'ok': False,
                        'message': f'{len(active)} track(s) still converting — '
                                   'skip or wait for them first'}), 400

    removed_files = 0
    folder = parent.output_path or ''
    for child in children:
        path = child.output_path or ''
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                removed_files += 1
            except OSError:
                pass
        db.session.delete(child)
    db.session.delete(parent)
    db.session.commit()

    # Remove the playlist folder itself when we emptied it.
    try:
        if folder and os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass
    return jsonify({'ok': True, 'removed_tracks': len(children),
                    'removed_files': removed_files})


@bp.route('/api/delete/<int:conversion_id>', methods=['POST'])
def delete_job(conversion_id):
    """Delete a finished track's file from disk and drop its history row."""
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404
    if history.is_playlist:
        return jsonify({'ok': False,
                        'message': 'Delete individual tracks, not whole playlists'}), 400
    if history.status in ConversionHistory.ACTIVE_STATUSES:
        return jsonify({'ok': False,
                        'message': 'Skip the track first before deleting it'}), 400

    removed = False
    path = history.output_path or ''
    if path and os.path.isfile(path):
        try:
            os.remove(path)
            removed = True
        except OSError as e:
            return jsonify({'ok': False,
                            'message': f'Could not delete file: {str(e)}'}), 500

    parent_id = history.parent_id
    db.session.delete(history)
    db.session.commit()
    if parent_id:
        _refresh_playlist_parent(parent_id)
    return jsonify({'ok': True, 'removed_file': removed})


def _readable_bytes(num):
    """Human-readable byte count for the storage readout."""
    value = float(num or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024


@bp.route('/api/storage')
def api_storage():
    """Total disk space used by converted files still on disk."""
    total = 0
    count = 0
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).all()
    for row in rows:
        path = row.output_path or ''
        if path and os.path.isfile(path):
            try:
                total += os.path.getsize(path)
                count += 1
            except OSError:
                pass
    return jsonify({'ok': True, 'bytes': total, 'files': count,
                    'readable': _readable_bytes(total)})


@bp.route('/api/retry/<int:conversion_id>', methods=['POST'])
def retry_job(conversion_id):
    """Re-queue a failed track (or every failed track of a playlist) from scratch."""
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404

    if history.is_playlist:
        failed = db.session.query(ConversionHistory).filter_by(
            parent_id=history.id, status='failed').all()
        if not failed:
            return jsonify({'ok': False,
                            'message': 'Playlist has no failed tracks to retry'}), 400
        for child in failed:
            child.status = 'pending'
            child.progress = 0
            child.retry_attempts = 0
            child.error = None
            _conversion_queue.put(child.id)
        db.session.commit()
        history.status = 'downloading'
        db.session.commit()
        _ensure_worker()
        _refresh_playlist_parent(history.id)
        return jsonify({'ok': True, 'retried': len(failed)})

    if history.status != 'failed':
        return jsonify({'ok': False,
                        'message': 'Only failed conversions can be retried'}), 400

    history.status = 'pending'
    history.progress = 0
    history.retry_attempts = 0
    history.error = None
    db.session.commit()
    _conversion_queue.put(history.id)
    _ensure_worker()
    if history.parent_id:
        _refresh_playlist_parent(history.parent_id)
    return jsonify({'ok': True, 'retried': 1})


@bp.route('/api/retry-misses/<int:parent_id>', methods=['POST'])
def retry_misses(parent_id):
    """Re-search an imported playlist's unmatched tracks and queue the hits.

    Only tracks that still have no child row are enqueued, so retrying never
    duplicates tracks that were already found.
    """
    parent = db.session.get(ConversionHistory, parent_id)
    if not parent or not parent.is_playlist:
        return jsonify({'ok': False, 'message': 'Playlist not found'}), 404
    try:
        misses = json.loads(parent.import_misses or '[]')
    except ValueError:
        misses = []
    if not misses:
        return jsonify({'ok': False, 'message': 'No unmatched tracks to retry'}), 400

    folder = parent.output_path or ''
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Cannot use folder: {str(e)}'}), 400

    existing_urls = {c.url for c in
                     db.session.query(ConversionHistory).filter_by(parent_id=parent.id).all()}
    existing = db.session.query(ConversionHistory).filter_by(
        parent_id=parent.id).order_by(ConversionHistory.item_index.desc()).first()
    next_index = (existing.item_index + 1) if existing else 0

    queued = 0
    remaining = []
    for track in misses:
        if not isinstance(track, dict) or not track.get('title'):
            continue
        try:
            expected_duration = float(track.get('duration') or 0)
        except (TypeError, ValueError):
            expected_duration = 0.0
        expected_title = (f"{track.get('artist')} - {track['title']}"
                          if track.get('artist') else track['title'])
        url = search_track_url(expected_title,
                               expected_duration=expected_duration or None)
        if url and url not in existing_urls:
            db.session.add(ConversionHistory(
                url=url, format=parent.format, output_path=folder,
                status='pending', progress=0, parent_id=parent.id,
                is_playlist=False, playlist_title=parent.playlist_title,
                item_index=next_index, item_count=parent.item_count,
                expected_title=expected_title,
                expected_duration=expected_duration,
            ))
            existing_urls.add(url)
            next_index += 1
            queued += 1
        else:
            remaining.append(track)

    parent.import_misses = json.dumps(remaining)
    if queued:
        parent.status = 'downloading'
    db.session.commit()
    for child in db.session.query(ConversionHistory).filter_by(
            parent_id=parent.id, status='pending').all():
        _conversion_queue.put(child.id)
    _ensure_worker()
    _refresh_playlist_parent(parent.id)
    return jsonify({'ok': True, 'queued': queued, 'remaining': len(remaining)})


@bp.route('/api/verify-files', methods=['POST'])
def api_verify_files():
    """Verify all downloaded files for integrity.
    
    Checks each completed/skipped conversion's output file via ffprobe.
    If a file is corrupted or invalid, it is deleted and the job is
    re-queued for re-download. Returns counts of checked/repaired/errors.
    """
    results = verify_all_files()
    return jsonify({
        'ok': True,
        'checked': results['checked'],
        'repaired': results['repaired'],
        'errors': results['errors'],
        'message': f'Checked {results["checked"]} files, repaired {results["repaired"]}, {results["errors"]} errors.',
    })


# ------------------------------------------------------ playlist helpers ----

PLAYLIST_MAX_ITEMS = 200


def _is_collection_url(url):
    """Heuristic for links that describe a whole collection (playlist, channel, or profile) rather than one track."""
    u = (url or '').strip().lower()
    # YouTube / Music playlists
    if 'music.youtube.com/playlist' in u or 'youtube.com/playlist' in u:
        return True
    if ('youtube.com' in u or 'youtu.be' in u or 'music.youtube.com' in u) and 'list=' in u:
        return True
    # SoundCloud sets
    if 'soundcloud.com' in u and '/sets/' in u:
        return True
    # YouTube channels / user pages / handles
    if any(seg in u for seg in ('youtube.com/channel/', 'youtube.com/user/',
                                'youtube.com/c/', 'music.youtube.com/channel/')):
        return True
    if re.search(r'(?:^|\.)(?:youtube\.com|m\.youtube\.com)/@', u):
        return True
    # SoundCloud user profiles (single path segment: soundcloud.com/<user>)
    if 'soundcloud.com' in u:
        try:
            path = urlparse(u).path.strip('/')
            segs = [s for s in path.split('/') if s]
            if len(segs) == 1 and segs[0] not in ('sets', 'settings', 'messages', 'discover'):
                return True
        except Exception:
            pass
    return False


def resolve_playlist(url, max_items=PLAYLIST_MAX_ITEMS):
    """List the track URLs in a playlist without downloading each one.

    Uses yt-dlp's fast flat-playlist mode. Returns either
    {'success': True, 'title': str, 'urls': [...]} or
    {'success': False, 'error': str}.
    """
    result = _run_ytdlp(
        ['--no-warnings', '--flat-playlist', '--dump-single-json', url],
        url, timeout=120,
    )
    if result is None or result.returncode != 0:
        tail = ''
        if result:
            tail = (result.stderr or result.stdout or '')[-300:]
        return {'success': False, 'error': f'yt-dlp could not read the playlist: {tail}'.strip()}

    try:
        info = json.loads(result.stdout)
    except ValueError:
        return {'success': False, 'error': 'yt-dlp returned unexpected data'}

    title = str(info.get('title') or 'Playlist').strip() or 'Playlist'
    urls = []
    for entry in info.get('entries') or []:
        if not entry or not isinstance(entry, dict):
            continue
        track_url = entry.get('webpage_url') or entry.get('url')
        if not track_url:
            video_id = entry.get('id')
            if video_id and _is_youtube(url):
                track_url = f'https://www.youtube.com/watch?v={video_id}'
        if track_url:
            track_url = sanitize_url(track_url)
            urls.append(track_url)
        if len(urls) >= max_items:
            break

    if not urls:
        return {'success': False, 'error': 'Playlist appears to be empty or private'}

    return {'success': True, 'title': title, 'urls': urls}


# --------------------------------------------- streaming playlist import ----
# Paste a Spotify / Apple Music playlist (or album) link and the app reads the
# track listing straight from the public page — no login, no API key — then
# finds each song on YouTube and converts it into one folder, exactly like a
# native playlist. Tidal only serves track listings to logged-in clients, so
# Tidal links fail with a message saying so instead of failing silently.

# Cap: every imported track costs one YouTube search (~1s each), so imports
# are capped lower than native (fast flat-listing) playlists.
IMPORT_MAX_TRACKS = 50

_BROWSER_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
               'AppleWebKit/537.36 (KHTML, like Gecko) '
               'Chrome/126.0 Safari/537.36')


def _streaming_service(url):
    """Return 'spotify', 'apple', 'tidal', or None for a pasted URL."""
    try:
        host = (urlparse(url).netloc or '').lower()
    except Exception:
        return None
    if 'open.spotify.com' in host:
        return 'spotify'
    if 'music.apple.com' in host:
        return 'apple'
    if 'tidal.com' in host:
        return 'tidal'
    return None


def _is_streaming_playlist_url(url):
    """True for an importable Spotify/Apple Music playlist or album page."""
    service = _streaming_service(url)
    if service is None:
        return False
    try:
        segs = [s for s in urlparse(url).path.split('/') if s]
    except Exception:
        return False
    if service == 'tidal':
        return 'playlist' in segs
    return 'playlist' in segs or 'album' in segs


def _fetch_page(url, timeout=25):
    """Fetch a public page with a fresh cookieless session. Returns text or None."""
    try:
        session = requests.Session()
        session.cookies.clear()
        resp = session.get(url, timeout=timeout, proxies=_http_proxies(), headers={
            'Accept': 'text/html',
            'User-Agent': _BROWSER_UA,
        })
        if resp.status_code != 200 or not resp.content:
            return None
        # requests defaults to ISO-8859-1 when the server sends no charset;
        # these pages are UTF-8, so prefer that over the latin-1 guess.
        encoding = resp.encoding or ''
        if encoding.lower().replace('_', '-') in ('', 'iso-8859-1', 'ascii'):
            encoding = 'utf-8'
        try:
            return resp.content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            return resp.text
    except Exception:
        pass
    return None


def _extract_spotify_playlist(url):
    """Read a public Spotify playlist/album via its embed page (no login).

    Returns {'success': True, 'title': str, 'tracks': [{'artist', 'title'}]}
    or {'success': False, 'error': str}.
    """
    try:
        segs = [s for s in urlparse(url).path.split('/') if s]
        kind = 'playlist' if 'playlist' in segs else 'album'
        sid = segs[segs.index(kind) + 1].split('?')[0]
        if not sid:
            raise IndexError
    except (ValueError, IndexError):
        return {'success': False, 'error': 'Could not find a Spotify playlist ID in that link'}

    html = _fetch_page(f'https://open.spotify.com/embed/{kind}/{sid}')
    if not html:
        return {'success': False, 'error': 'Spotify did not return the playlist page'}
    match = re.search(r'"__NEXT_DATA__" type="application/json">(\{.*?\})</script>',
                      html, re.S)
    if not match:
        return {'success': False, 'error': 'Spotify did not include the track listing in its page'}
    try:
        entity = json.loads(match.group(1))['props']['pageProps']['state']['data']['entity']
    except (ValueError, KeyError, TypeError):
        return {'success': False, 'error': 'Spotify returned an unexpected page format'}

    title = str(entity.get('name') or entity.get('title') or 'Spotify Playlist').strip()
    tracks = []
    for entry in entity.get('trackList') or []:
        if not isinstance(entry, dict):
            continue
        song = str(entry.get('title') or '').strip()
        artist = re.sub(r'\s+', ' ',
                        str(entry.get('subtitle') or '').replace('\xa0', ' ')).strip(' ,')
        try:
            duration = float(entry.get('duration') or 0) / 1000.0
        except (TypeError, ValueError):
            duration = 0.0
        if song:
            tracks.append({'artist': artist, 'title': song, 'duration': duration})
    if not tracks:
        return {'success': False, 'error': 'No tracks found on that Spotify page'}
    return {'success': True, 'title': title or 'Spotify Playlist', 'tracks': tracks}


def _extract_apple_playlist(url):
    """Read a public Apple Music playlist/album from its embedded data (no login).

    Playlists carry a JSON-LD track list; albums instead embed one row per
    track, so album tracks are paired positionally (nearest preceding title
    and artist for each track's `?i=` link).

    Returns {'success': True, 'title': str, 'tracks': [{'artist', 'title'}]}
    or {'success': False, 'error': str}.
    """
    html = _fetch_page(url)
    if not html:
        return {'success': False, 'error': 'Apple Music did not return the playlist page'}
    match = re.search(
        r'<script id=schema:music-(?:playlist|album) type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        html, re.S)
    data = None
    if match:
        try:
            data = json.loads(match.group(1))
        except ValueError:
            data = None
    title = ''
    if isinstance(data, dict):
        title = str(data.get('name') or '').strip()
        if data.get('@type') == 'MusicPlaylist':
            raw_tracks = [t for t in data.get('track', [])
                          if isinstance(t, dict) and t.get('name')]
            names = [t.get('name') for t in raw_tracks]
            durations = [_parse_iso8601_duration(t.get('duration')) for t in raw_tracks]
            artists = _apple_artist_names(html)
            if len(artists) == len(names):
                paired = list(zip(names, artists, durations))
            elif len(artists) == 1:
                paired = [(name, artists[0], duration)
                          for name, duration in zip(names, durations)]
            else:
                paired = [(name, '', duration)
                          for name, duration in zip(names, durations)]
            tracks = [{'artist': artist, 'title': name, 'duration': duration}
                      for name, artist, duration in paired]
            if tracks:
                return {'success': True,
                        'title': title or 'Apple Music Playlist', 'tracks': tracks}
    # Albums (and anything without a JSON-LD track list): pair each track's
    # `?i=` link with its nearest preceding title/artist.
    tracks = _extract_apple_album_tracks(html)
    if not tracks:
        return {'success': False, 'error': 'No tracks found on that Apple Music page'}
    if not title:
        og = re.search(r'<meta property="og:title" content="([^"]{1,120})"', html)
        title = og.group(1).strip() if og else 'Apple Music Playlist'
    return {'success': True, 'title': title or 'Apple Music Playlist',
            'tracks': tracks}


def _apple_artist_names(html):
    """Ordered artist names from Apple Music page data."""
    artists = []
    for raw_artist in re.findall(r'"artistName":"((?:[^"\\]|\\.)*)"', html):
        try:
            artists.append(json.loads('"' + raw_artist + '"'))
        except ValueError:
            artists.append(raw_artist)
    return artists


def _extract_apple_album_tracks(html):
    """Pair album track rows positionally via their `?i=` links."""
    artists = _apple_artist_names(html)
    album_artist = artists[0] if artists else ''
    tracks = []
    seen = set()
    for match in re.finditer(r'\?i=(\d+)', html):
        tid = match.group(1)
        if tid in seen:
            continue
        seen.add(tid)
        pre = html[max(0, match.start() - 1500):match.start()]
        titles = re.findall(r'"title":"((?:[^"\\]|\\.){1,120})"', pre)
        row_artists = re.findall(r'"artistName":"((?:[^"\\]|\\.){1,120})"', pre)
        if not titles:
            continue
        try:
            song = json.loads('"' + titles[-1] + '"')
        except ValueError:
            song = titles[-1]
        artist = album_artist
        if row_artists:
            try:
                artist = json.loads('"' + row_artists[-1] + '"')
            except ValueError:
                artist = row_artists[-1]
        tracks.append({'artist': artist, 'title': song, 'duration': 0.0})
    return tracks


def resolve_streaming_playlist(url, max_tracks=IMPORT_MAX_TRACKS):
    """Extract the track listing of a foreign-service playlist.

    Returns {'success': True, 'title', 'source', 'tracks': [...]} or
    {'success': False, 'error': str}.
    """
    service = _streaming_service(url)
    if service == 'tidal':
        from app.routes.tidal import extract_tidal_playlist
        result = extract_tidal_playlist(url, max_tracks=max_tracks)
        if not result.get('success'):
            return result
        return {'success': True, 'title': result['title'],
                'source': 'tidal', 'tracks': result['tracks'][:max_tracks]}
    if service == 'spotify':
        result = _extract_spotify_playlist(url)
    elif service == 'apple':
        result = _extract_apple_playlist(url)
    else:
        return {'success': False, 'error': 'That link is not a supported playlist page'}
    if not result.get('success'):
        return result
    tracks = result['tracks'][:max_tracks]
    if not tracks:
        return {'success': False, 'error': 'No tracks found on that playlist page'}
    return {'success': True, 'title': result['title'],
            'source': service, 'tracks': tracks}


def _entry_duration(entry):
    try:
        return float(entry.get('duration') or 0)
    except (TypeError, ValueError):
        return 0.0


def _duration_close(length, expected):
    """True when a search hit's length matches the expected track length.

    Accepts millisecond units too (some extractors report durations in ms).
    """
    if length <= 0 or not expected or expected <= 0:
        return False
    candidates = [length]
    if length > expected * 10:
        candidates.append(length / 1000.0)
    tolerance = max(20, expected * 0.25)
    return any(abs(candidate - expected) <= tolerance for candidate in candidates)


def _search_once(engine, query, timeout):
    """One search pass via yt-dlp; returns a list of entry dicts."""
    result = _run_ytdlp(
        ['--no-warnings', '--flat-playlist', '--dump-single-json',
         f'{engine}:{query}'],
        query, timeout=timeout,
    )
    if result is None or result.returncode != 0:
        return []
    try:
        info = json.loads(result.stdout)
    except ValueError:
        return []
    return [e for e in (info.get('entries') or []) if isinstance(e, dict)]


def _entry_url(entry, engine):
    """Canonical download URL for a search hit, or None when unusable."""
    track_url = entry.get('webpage_url') or entry.get('url')
    if not track_url and entry.get('id'):
        if engine != 'ytsearch5':
            return None
        track_url = f'https://www.youtube.com/watch?v={entry["id"]}'
    return sanitize_url(track_url) if track_url else None


def _ranked_hits(entries, engine, expected_duration):
    """Split search hits into (duration matches, rest), URLs resolved."""
    matched, rest = [], []
    for entry in entries:
        url = _entry_url(entry, engine)
        if not url:
            continue
        title = str(entry.get('title') or '')
        duration = _entry_duration(entry)
        hit = {'url': url, 'title': title, 'duration': duration,
               'engine': engine}
        if _duration_close(duration, expected_duration):
            matched.append(hit)
        else:
            rest.append(hit)
    return matched, rest


def _search_candidates(query, timeout=60, expected_duration=None, limit=6,
                       exclude=()):
    """Ranked download candidates from both YouTube and SoundCloud.

    Order: YouTube duration-matches, other YouTube hits, SoundCloud
    duration-matches, other SoundCloud hits. The original URL (when given)
    is excluded so a failed source is never suggested back to itself.
    """
    excluded = {sanitize_url(u) for u in (exclude or ()) if u}
    youtube = _search_once('ytsearch5', query, timeout)
    yt_matched, yt_rest = _ranked_hits(youtube, 'ytsearch5', expected_duration)
    soundcloud = _search_once('scsearch1', query, timeout)
    sc_matched, sc_rest = _ranked_hits(soundcloud, 'scsearch1', expected_duration)
    ordered = yt_matched + yt_rest + sc_matched + sc_rest
    return [hit for hit in ordered if hit['url'] not in excluded][:limit]


def search_track_url(query, timeout=60, expected_duration=None):
    """Find an audio URL for an 'Artist - Title' query (no API key).

    Takes the top-ranked candidate across YouTube and SoundCloud, preferring
    hits whose length is close to the expected duration when one is known —
    a strong signal against wrong videos (hour-long loops, unrelated
    uploads). Returns the URL or None.
    """
    candidates = _search_candidates(query, timeout=timeout,
                                    expected_duration=expected_duration,
                                    limit=1)
    return candidates[0]['url'] if candidates else None


def find_alternate_sources(url, meta, expected_title='', limit=4):
    """Other-platform URLs likely to hold the same song.

    When a direct download fails, the same recording almost always exists on
    the other service, so search both and return ranked candidates excluding
    the failed URL itself.
    """
    meta = meta or {}
    query = (expected_title or '').strip()
    if not query:
        artist = str(meta.get('artist') or '').strip()
        title = str(meta.get('title') or '').strip()
        query = f'{artist} - {title}' if artist and title else title
    if not query:
        return []
    try:
        expected_duration = float(meta.get('duration') or 0) or None
    except (TypeError, ValueError):
        expected_duration = None
    candidates = _search_candidates(
        query, expected_duration=expected_duration, limit=6,
        exclude=(url,))
    youtube = [hit for hit in candidates if hit.get('engine') == 'ytsearch5']
    other = [hit for hit in candidates if hit.get('engine') != 'ytsearch5']
    ordered = []
    for pair in zip(youtube, other):
        ordered.extend(pair)
    ordered.extend(youtube[len(other):] or other[len(youtube):])
    return [hit['url'] for hit in ordered[:limit]]


def resolve_import_urls(url, max_tracks=IMPORT_MAX_TRACKS):
    """Extract a foreign playlist and resolve each track to a YouTube URL.

    Returns {'success': True, 'title', 'source', 'urls', 'found', 'total'}
    or {'success': False, 'error': str}.
    """
    resolved = resolve_streaming_playlist(url, max_tracks=max_tracks)
    if not resolved.get('success'):
        return resolved
    urls = []
    items = []
    misses = []
    for track in resolved['tracks']:
        expected_title = (f"{track['artist']} - {track['title']}"
                          if track.get('artist') else track['title'])
        try:
            expected_duration = float(track.get('duration') or 0)
        except (TypeError, ValueError):
            expected_duration = 0.0
        found = search_track_url(expected_title,
                                 expected_duration=expected_duration or None)
        if found:
            urls.append(found)
            items.append({'url': found, 'expected_title': expected_title,
                          'expected_duration': expected_duration})
        else:
            misses.append({'artist': track.get('artist') or '',
                           'title': track['title'],
                           'duration': expected_duration})
    if not urls:
        return {'success': False,
                'error': 'None of the playlist tracks could be found online'}
    return {'success': True, 'title': resolved['title'],
            'source': resolved['source'], 'urls': urls, 'items': items,
            'misses': misses, 'found': len(urls),
            'total': len(resolved['tracks'])}


def _cleanup(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# -------------------------------------------------------- duplicate skip ----

def find_duplicate(directory, filename):
    """Return the path of an existing file that matches `filename` (case-insensitive), or None."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    target = filename.lower()
    for entry in entries:
        if entry.lower() == target:
            full = os.path.join(directory, entry)
            if os.path.isfile(full):
                return full
    return None


def _should_skip_duplicates():
    settings = UserSettings.query.first()
    return getattr(settings, 'skip_existing', True)


# ----------------------------------------------------------------- ffprobe ---

def _ffmpeg_file_info(path):
    """Run `ffmpeg -i` on a file and parse its Duration plus metadata tags.

    Returns (duration_seconds, tags_dict). Unparseable files yield (0.0, {}).
    Using ffmpeg instead of ffprobe keeps the app down to a single binary
    dependency (static ffmpeg builds often ship without ffprobe).
    """
    try:
        res = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', path],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0, {}
    out = res.stderr or ''
    duration = 0.0
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', out)
    if match and 'N/A' not in match.group(0):
        try:
            duration = (int(match.group(1)) * 3600
                        + int(match.group(2)) * 60
                        + float(match.group(3)))
        except ValueError:
            duration = 0.0
    tags = {}
    for key in ('title', 'artist', 'album', 'date'):
        tag = re.search(r'(?im)^\s*' + key + r'\s*:\s*(.+?)\s*$', out)
        if tag:
            tags[key] = tag.group(1)
    return duration, tags


def _probe_metadata(path):
    """Extract title, artist, album, and duration from a converted file."""
    duration, tags = _ffmpeg_file_info(path)
    return {
        'title': tags.get('title', ''),
        'artist': tags.get('artist', ''),
        'album': tags.get('album', ''),
        'duration': duration,
    }


def _is_valid_audio(path):
    """Check if a file is genuinely playable audio, not just named like it.

    Fully decodes the file with ffmpeg: truncated downloads and garbage
    bytes fail the decode, while header-only ffprobe checks would pass them.
    Returns True if the file is valid, False if corrupted or unreadable.
    """
    try:
        # Quick check: file must exist and be non-empty before decoding
        if not path or not os.path.isfile(path):
            return False
        if os.path.getsize(path) == 0:
            return False
        res = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', path, '-f', 'null', '-'],
            capture_output=True, text=True, timeout=120,
        )
        return res.returncode == 0
    except Exception:
        return False


def _probe_duration(path):
    """Return a file's audio duration in seconds, or 0 if unreadable."""
    duration, _tags = _ffmpeg_file_info(path)
    return duration


def _parse_iso8601_duration(value):
    """Parse an ISO-8601 duration like 'PT3M33S' into seconds (0 on failure)."""
    try:
        match = re.fullmatch(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?',
                             str(value or '').strip())
        if not match:
            return 0.0
        hours, minutes, seconds = match.groups()
        return (int(hours or 0) * 3600 + int(minutes or 0) * 60
                + float(seconds or 0))
    except Exception:
        return 0.0


def _normalize_title(value):
    """Lowercase a title with bracketed suffixes removed for fuzzy comparison."""
    text = re.sub(r'\s*[\(\[].*?[\)\]]', '', str(value or '').lower())
    return re.sub(r'\s+', ' ', text).strip()


def _titles_match(expected, actual, threshold=0.4):
    """Heuristic: does the downloaded video look like the expected song?

    Bracketed suffixes ('(Remastered 2009)', '[Official Video]') are ignored
    so legitimate variants still match, while completely unrelated videos
    score far below the threshold.
    """
    exp, act = _normalize_title(expected), _normalize_title(actual)
    if not exp or not act:
        return True
    if exp in act or act in exp:
        return True
    return difflib.SequenceMatcher(None, exp, act).ratio() >= threshold


def _verify_output(path, expected_duration=None, expected_title=None):
    """Verify a freshly converted file. Returns (ok, reason).

    Checks, in order: the file parses as audio (not corrupted), its length
    matches the expected duration when one is known (not truncated, not a
    hours-long wrong video), and — for imported tracks — its source title
    resembles the expected song (not a wrong search hit).
    """
    if not _is_valid_audio(path):
        return False, 'File is corrupted or unreadable'
    if expected_duration and expected_duration > 0:
        actual = _probe_duration(path)
        if actual <= 0:
            return False, 'File duration could not be read'
        if actual < expected_duration * 0.9 - 2:
            return False, (f'File looks truncated '
                           f'({actual:.0f}s of {expected_duration:.0f}s expected)')
        if actual > max(expected_duration * 3, expected_duration + 600):
            return False, (f'File length does not match '
                           f'({actual:.0f}s vs {expected_duration:.0f}s expected)')
    if expected_title:
        meta = _probe_metadata(path)
        if not _titles_match(expected_title, meta.get('title', '')):
            return False, 'Downloaded audio does not match the expected song'
    return True, 'File verified'


def verify_job_file(job):
    """Verify a single job's output file. Returns (is_valid, message)."""
    if not job or job.status not in ('completed', 'skipped'):
        return False, 'Job not in a completable state'
    path = job.output_path or ''
    if not path:
        return False, 'No output path set'
    if _is_valid_audio(path):
        return True, 'File is valid'
    # File is corrupted or invalid
    try:
        os.remove(path)
    except Exception:
        pass
    # Reset the job to pending so it can be re-downloaded
    job.retry_attempts = 0
    job.status = 'pending'
    job.progress = 0
    job.error = 'File corrupted or invalid; re-queued for re-download'
    db.session.commit()
    # Re-queue the job
    _conversion_queue.put(job.id)
    return True, 'File deleted and job re-queued for re-download'


def verify_all_files():
    """Scan all conversions and verify/repair corrupted files.
    Returns a dict with counts of checked, fixed, and failed jobs."""
    with app.app_context():
        from app.models import ConversionHistory
        jobs = ConversionHistory.query.filter(
            ConversionHistory.status.in_('completed', 'skipped')
        ).all()
        results = {'checked': 0, 'repaired': 0, 'errors': 0}
        for job in jobs:
            results['checked'] += 1
            try:
                ok, msg = verify_job_file(job)
                if ok:
                    results['repaired'] += 1
                else:
                    results['errors'] += 1
            except Exception as e:
                results['errors'] += 1
        return results