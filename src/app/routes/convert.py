import json
import os
import queue
import re
import difflib
import glob
import hashlib
import shutil
import subprocess
import sys
import time
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests
from sqlalchemy import or_
from flask import (Blueprint, render_template, request, flash, redirect,
                   url_for, jsonify, send_file, abort)
from app.models import db, ConversionHistory, UserSettings, effective_output_path, utcnow

bp = Blueprint('convert', __name__)

VALID_FORMATS = {
    'flac': {'ext': 'flac', 'label': 'FLAC', 'kind': 'audio',
             'base_args': ['-c:a', 'flac']},
    'alac': {'ext': 'm4a', 'label': 'ALAC', 'kind': 'audio',
             'base_args': ['-c:a', 'alac']},
    'wav': {'ext': 'wav', 'label': 'WAV', 'kind': 'audio',
            'base_args': ['-c:a', 'pcm_s16le']},
    'ogg_vorbis': {'ext': 'ogg', 'label': 'OGG Vorbis', 'kind': 'audio',
                   'base_args': ['-c:a', 'vorbis', '-q:a', '8', '-strict', 'experimental']},
    'video_mp4': {'ext': 'mp4', 'label': 'MP4 Video', 'kind': 'video',
                  'base_args': []},
    'video_webm': {'ext': 'webm', 'label': 'WebM Video', 'kind': 'video',
                   'base_args': []},
    'video_mkv': {'ext': 'mkv', 'label': 'MKV Video', 'kind': 'video',
                  'base_args': []},
}

# Containers browsers can play natively in a <video> element. Anything else
# downloads for external players and the theater offers a download instead.
BROWSER_VIDEO_EXTS = ('mp4', 'm4v', 'webm', 'ogv')

LABEL_TO_KEY = {v['label']: k for k, v in VALID_FORMATS.items()}

# Formats that support embedded cover art
COVER_FORMATS = ('flac', 'alac')

# ---------------------------------------------------------------- queue ----

_conversion_queue = queue.Queue()
# Live yt-dlp processes by job id (for pause) and ids the user paused.
# Membership tests are GIL-atomic; proc objects are only touched by owner.
_active_downloads = {}
_paused_jobs = set()
_paused_lock = threading.Lock()
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
    _ensure_scheduler()


def _spawn_workers(count):
    """Start worker threads up to `count` living ones."""
    with _pool_lock:
        living = _active_workers
    missing = count - living
    for index in range(max(0, missing)):
        t = threading.Thread(target=_worker_loop, daemon=True,
                             name=f'audio-worker-{index}')
        t.start()


def _queue_has(job_id):
    """True when a job id is already waiting in memory (no double-queue)."""
    try:
        with _conversion_queue.mutex:
            return job_id in _conversion_queue.queue
    except Exception:
        return False


def _resume_interrupted():
    """Requeue conversions orphaned by a restart or crash.

    In-flight rows (downloading/converting) can never finish on their own;
    reset them to pending. Pending rows lost their in-memory queue slot
    when the process died, so hand every active row back to the pool —
    skipping ones already queued. Returns the number set in motion.
    Safe to call on every launch (idempotent).
    """
    try:
        rows = db.session.query(ConversionHistory).filter(
            ConversionHistory.status.in_(
                list(ConversionHistory.ACTIVE_STATUSES))).all()
    except Exception:
        return 0
    count = 0
    for row in rows:
        try:
            if row.status != 'pending':
                row.status = 'pending'
                db.session.commit()
            if not _queue_has(row.id):
                _conversion_queue.put(row.id)
                count += 1
        except Exception:
            db.session.rollback()
    if count:
        _ensure_worker()
    return count


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
                    if job.status == 'paused':
                        # Paused between dequeue and start (or requeued while
                        # paused): nothing downloaded, nothing to clean up.
                        # Resume requeues when the user is ready.
                        if job.parent_id:
                            _refresh_playlist_parent(job.parent_id)
                        continue
                    if job.status not in ConversionHistory.ACTIVE_STATUSES:
                        # Terminal already (e.g. a twice-queued id the first
                        # run finished): never re-download a done track.
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
                        if job.status == 'paused' or (
                                job.id in _paused_jobs):
                            # Paused mid-download: the partial file stays so
                            # resume continues it (--continue). Never treat
                            # the killed process as a failure or retry it.
                            if job.parent_id:
                                _refresh_playlist_parent(job.parent_id)
                            continue
                        if result.get('duplicate'):
                            _store_file_facts(job, result['filepath'])
                            _job_finish(job, status='skipped', progress=100, error='Already exists on disk',
                                        output_path=result['filepath'],
                                        duration=job.duration or 0)
                            _invalidate_stat(result['filepath'])
                        elif result['success']:
                            duration = _store_file_facts(job, result['filepath'])
                            if not _job_finish(job, status='completed', progress=100,
                                               error=None, output_path=result['filepath'],
                                               duration=duration):
                                _cleanup(result['filepath'])
                            else:
                                _invalidate_stat(result['filepath'])
                                if not job.parent_id and not job.is_playlist:
                                    _notify('Download finished',
                                            os.path.basename(result['filepath']))
                        else:
                            action, attempts, message = _failure_plan(
                                job.retry_attempts, max_retries, result.get('error'))
                            if action == 'retry':
                                if _job_finish(job, status='pending', progress=0,
                                                retry_attempts=attempts,
                                                error=message):
                                    _conversion_queue.put(job.id)
                            elif _job_finish(job, status='failed', error=message):
                                if not job.parent_id and not job.is_playlist:
                                    _notify('Download failed',
                                            (job.url or '')[:200])
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
                        if getattr(e, 'errno', None) == 28 or _looks_like_nospace(e):
                            _job_finish(job, status='failed',
                                        error='Disk filled up mid-download — free space and retry this track.')
                        elif job.retry_attempts < max_retries and not is_permanent:
                            attempts = job.retry_attempts + 1
                            if _job_finish(job, status='pending', progress=0,
                                            retry_attempts=attempts,
                                            error=f'Retry {attempts}/{max_retries}: Internal error — will retry'):
                                _conversion_queue.put(job.id)
                        else:
                            _job_finish(job, status='failed', error=_note_stale_helper(
                                        f'Max retries ({max_retries}) exceeded: {str(e) if str(e) else "Permanent error"}'))
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
            short = _active_workers < _desired_workers
        if short:
            # Died unexpectedly (not a pool shrink, which exits only when
            # over target): replace so the queue can never stall silently.
            try:
                _spawn_workers(_desired_workers)
            except Exception:
                pass


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

    _append_children(job, urls, title, folder, expectations,
                     subscription_id=None)


def _append_children(parent, urls, title, folder, expectations=None,
                     subscription_id=None):
    """Create one pending child row per URL, continuing item indexes.

    Shared by first-time playlist expansion and subscription re-checks so
    both flows enqueue, count, and verify identically.
    """
    expectations = expectations or {}
    top = db.session.query(ConversionHistory).filter_by(
        parent_id=parent.id).order_by(
        ConversionHistory.item_index.desc()).first()
    start = (top.item_index + 1) if top else 0
    total = start + len(urls)

    children = []
    for offset, url in enumerate(urls):
        expected = expectations.get(url, {})
        children.append(ConversionHistory(
            url=url,
            format=parent.format,
            output_path=folder,
            status='pending',
            progress=0,
            parent_id=parent.id,
            is_playlist=False,
            playlist_title=title,
            item_index=start + offset,
            item_count=total,
            expected_title=expected.get('expected_title', ''),
            expected_duration=expected.get('expected_duration', 0),
            subscription_id=(subscription_id if subscription_id is not None
                             else parent.subscription_id),
        ))
    db.session.add_all(children)
    parent.item_count = total
    db.session.commit()
    for child in children:
        _conversion_queue.put(child.id)
    return children


# ------------------------------------------------- subscriptions ----
# Followed playlists/channels: a daemon re-resolves them on a schedule and
# appends only tracks that have never been queued, so new uploads arrive
# converted without re-downloading anything.

SUBSCRIPTION_INTERVALS = (6, 12, 24, 168)

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _ensure_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if not _scheduler_started:
            t = threading.Thread(target=_scheduler_loop, daemon=True,
                                 name='subscription-scheduler')
            t.start()
            _scheduler_started = True
    _ensure_helper_watch()


def _scheduler_loop():
    from app import app as flask_app
    while True:
        try:
            with flask_app.app_context():
                _check_due_subscriptions()
        except Exception:
            pass
        time.sleep(60)


_helper_watch_started = False
_helper_watch_lock = threading.Lock()
_tag_backfill_started = False
_tag_backfill_lock = threading.Lock()


def _ensure_helper_watch():
    """Start the background yt-dlp freshness check (once per process)."""
    global _helper_watch_started
    with _helper_watch_lock:
        if not _helper_watch_started:
            t = threading.Thread(target=_helper_watch_loop, daemon=True,
                                 name='helper-watch')
            t.start()
            _helper_watch_started = True
    global _tag_backfill_started
    with _tag_backfill_lock:
        if not _tag_backfill_started:
            t = threading.Thread(target=_tag_backfill_loop, daemon=True,
                                 name='tag-backfill')
            t.start()
            _tag_backfill_started = True


def _tag_backfill_loop():
    """Fill duration/tags for pre-upgrade rows without stalling requests.

    New conversions store facts at completion; this covers older rows one
    probe at a time in the background so the first library render after
    upgrading doesn't pay for hundreds of ffmpeg spawns at once.
    """
    from app import app as flask_app
    time.sleep(60)
    while True:
        try:
            with flask_app.app_context():
                rows = db.session.query(ConversionHistory).filter(
                    ConversionHistory.status.in_(['completed', 'skipped']),
                    ConversionHistory.output_path.isnot(None),
                    (ConversionHistory.tag_title.is_(None) |
                     (ConversionHistory.tag_title == ''))).limit(25).all()
                if not rows:
                    return
                for row in rows:
                    try:
                        path = row.output_path or ''
                        if path and os.path.isfile(path):
                            _store_file_facts(row, path)
                    except Exception:
                        pass
                    time.sleep(0.2)
        except Exception:
            pass
        time.sleep(30)


def _helper_watch_loop():
    """Nudge the health banner when a newer yt-dlp release exists.

    YouTube changes break old downloaders silently; waiting for a failed
    conversion to notice wastes the user's time. First check runs a few
    minutes after startup (never on the launch path), then every 6 hours.
    Failures are silent — offline just means no nudge.
    """
    from app import _version_tuple, app as flask_app
    time.sleep(300)
    while True:
        try:
            current = _ytdlp_version()
            latest = _latest_ytdlp_release()
            if (latest and current and
                    _version_tuple(latest['version']) > _version_tuple(current)):
                auto = False
                try:
                    with flask_app.app_context():
                        settings = UserSettings.query.first()
                        auto = bool(getattr(settings, 'auto_update_ytdlp', False))
                except Exception:
                    auto = False
                if auto:
                    ok, message, _version = _perform_ytdlp_update()
                    if ok:
                        try:
                            with flask_app.app_context():
                                _notify('Downloader updated', message)
                        except Exception:
                            pass
                    else:
                        _stale_helper_event.set()
                else:
                    _stale_helper_event.set()
            else:
                _stale_helper_event.clear()
        except Exception:
            pass
        time.sleep(6 * 3600)


def _due_subscriptions():
    from app.models import Subscription
    from datetime import datetime, timedelta
    now = utcnow()
    due = []
    for sub in Subscription.query.filter_by(active=True).all():
        interval = sub.interval_hours if sub.interval_hours in SUBSCRIPTION_INTERVALS else 24
        if not sub.last_checked or now - sub.last_checked >= timedelta(hours=interval):
            due.append(sub)
    return due


def _check_due_subscriptions():
    from datetime import datetime
    for sub in _due_subscriptions():
        try:
            result = check_subscription(sub.id)
            added = result.get('added', 0) if isinstance(result, dict) else 0
            if added:
                title = (getattr(sub, 'playlist_title', '') or
                         getattr(sub, 'url', '') or 'Subscription')
                _notify('New tracks available',
                        f'{title}: {added} new track(s) queued.')
        except Exception:
            pass
        try:
            sub.last_checked = utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()


def _resolve_subscription_urls(sub):
    """Current track URLs for a subscription, via native or import listing."""
    if _is_streaming_playlist_url(sub.url):
        result = resolve_import_urls(sub.url)
    else:
        result = resolve_playlist(sub.url)
    if not result.get('success'):
        return result
    return result


def check_subscription(subscription_id):
    """Append a subscription's new tracks as children of its parent row.

    Returns {'ok': True, 'added': N} or {'ok': False, 'error': str}.
    """
    from app.models import Subscription
    sub = db.session.get(Subscription, subscription_id)
    if sub is None:
        return {'ok': False, 'error': 'Subscription not found'}
    parent = db.session.get(ConversionHistory, sub.parent_id) if sub.parent_id else None
    if parent is None:
        return {'ok': False, 'error': 'Original playlist row is gone'}

    try:
        result = _resolve_subscription_urls(sub)
    except Exception as e:
        return {'ok': False, 'error': f'Could not read playlist: {str(e)}'}
    if not result.get('success'):
        return {'ok': False, 'error': result.get('error') or 'Could not read playlist'}

    known = {c.url for c in db.session.query(ConversionHistory).filter_by(
        parent_id=parent.id).all()}
    fresh = [u for u in result['urls'] if u not in known]
    if not fresh:
        return {'ok': True, 'added': 0}

    folder = parent.output_path or sub.output_path
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        return {'ok': False, 'error': f'Cannot use folder: {str(e)}'}

    expectations = {item['url']: item for item in result.get('items', [])}
    _append_children(parent, fresh, result.get('title') or parent.playlist_title,
                     folder, expectations, subscription_id=sub.id)
    # Reopen the parent so its completion (and notification) fires again.
    parent.status = 'downloading'
    parent.error = None
    db.session.commit()
    _ensure_worker()
    _refresh_playlist_parent(parent.id)
    return {'ok': True, 'added': len(fresh)}


def _refresh_playlist_parent(parent_id):
    """Recompute a playlist parent's status/progress from its children."""
    parent = db.session.get(ConversionHistory, parent_id)
    if parent is None:
        return
    was_terminal = parent.status in ('completed', 'failed')

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
    if not was_terminal and parent.status in ('completed', 'failed'):
        title = parent.playlist_title or 'Playlist'
        if parent.status == 'failed':
            _notify('Playlist failed', title)
        elif parent.error:
            _notify('Playlist finished', f'{title} — {parent.error}')
        else:
            _notify('Playlist finished', f'{title} — all {total} tracks ready')


def _notifications_enabled():
    try:
        settings = UserSettings.query.first()
        if settings is None:
            return True
        return bool(getattr(settings, 'desktop_notifications', True))
    except Exception:
        return True


def _log_notice(title, message):
    """Persist a copy for the in-app notification bell (best-effort)."""
    try:
        from app.models import Notice
        db.session.add(Notice(title=str(title or '')[:200],
                              body=str(message or '')[:500]))
        db.session.commit()
        stale = db.session.query(Notice).order_by(
            Notice.id.desc()).offset(200).all()
        for old in stale:
            db.session.delete(old)
        if stale:
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _notify(title, message):
    """Best-effort OS notification. Never raises, never blocks the worker."""
    _log_notice(title, message)
    try:
        if not _notifications_enabled():
            return
        if sys.platform == 'darwin':
            safe = lambda s: str(s).replace('\\', '\\\\').replace('"', '')
            script = 'display notification "{}" with title "{}"'.format(
                safe(message)[:300], safe(title)[:100])
            subprocess.run(['osascript', '-e', script],
                           capture_output=True, timeout=10)
        elif sys.platform.startswith('win'):
            safe = lambda s: (str(s).replace('&', '&amp;').replace('<', '&lt;')
                              .replace('>', '&gt;')[:300])
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, "
                "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
                "$xml = '<toast><visual><binding template=\"ToastGeneric\">"
                f"<text>{safe(title)}</text><text>{safe(message)}</text>"
                "</binding></visual></toast>'; "
                "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                "$doc.LoadXml($xml); "
                "[Windows.UI.Notifications.ToastNotificationManager]::"
                "CreateToastNotifier('AudioConverter').Show("
                "[Windows.UI.Notifications.ToastNotification]::new($doc))")
            subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                           capture_output=True, timeout=15)
        else:
            if shutil.which('notify-send'):
                subprocess.run(['notify-send', str(title)[:100],
                                str(message)[:300]],
                               capture_output=True, timeout=10)
    except Exception:
        pass


def _job_update(job, **fields):
    for key, value in fields.items():
        setattr(job, key, value)
    db.session.commit()
    if fields.get('status') in ('completed', 'failed', 'skipped'):
        _progress_cache.pop(getattr(job, 'id', None), None)


# Last committed (percent, monotonic time) per job, so concurrent downloads
# don't fsync the database on every single progress tick.
_progress_cache = {}


def _job_progress(job, pct):
    """Record download progress, committing at most every 2s or 2 points.

    yt-dlp emits a progress line per percent per worker; committing each one
    would serialize all workers on the database lock and starve the UI.
    """
    now = time.monotonic()
    last = _progress_cache.get(job.id)
    if last is not None:
        last_pct, last_time = last
        if pct < last_pct + 2 and now - last_time < 2.0:
            return
    _progress_cache[job.id] = (pct, now)
    _job_update(job, progress=int(pct))


# Filesystem stat cache: staleness-tolerant existence/size lookups so list
# and polling endpoints never block on slow (cloud-synced, network) paths.
_stat_cache = {}
_stat_lock = threading.Lock()


def _cached_stat(path, ttl=30.0):
    """(exists, size) for `path`, rechecked at most once per `ttl` seconds."""
    now = time.monotonic()
    with _stat_lock:
        entry = _stat_cache.get(path)
        if entry is not None and now - entry[2] < ttl:
            return entry[0], entry[1]
    try:
        exists = os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
    except OSError:
        exists, size = False, 0
    with _stat_lock:
        _stat_cache[path] = (exists, size, now)
        # Bound memory: drop the oldest entries past a comfortable size.
        if len(_stat_cache) > 2000:
            oldest = sorted(_stat_cache,
                            key=lambda k: _stat_cache[k][2])[:500]
            for key in oldest:
                _stat_cache.pop(key, None)
    return exists, size


def _invalidate_stat(path):
    with _stat_lock:
        _stat_cache.pop(path, None)
    _invalidate_storage()


_storage_cache = {'time': 0.0, 'total': 0, 'files': 0}


def _invalidate_storage():
    _storage_cache['time'] = 0.0
    # Path entries may claim a deleted file still exists; drop them too so
    # the recomputed total (and file_exists flags) read fresh state.
    _stat_cache.clear()


# Signatures of a stale yt-dlp (sites change constantly; an old helper
# fails in recognizable ways). Matching failures hint the fix and raise a
# banner instead of failing silently forever.
_STALE_PATTERNS = (
    'signature extraction failed',
    'player response',
    'nsig',
    'throttling',
    'http error 403',
    'did not match',
    'update yt-dlp',
    'unsupported url',
)
_stale_helper_event = threading.Event()


def _looks_stale(error_text):
    lowered = (error_text or '').lower()
    return any(pattern in lowered for pattern in _STALE_PATTERNS)


def _looks_like_nospace(error):
    """True when a failure means the disk filled up mid-download.

    Retrying those is pointless: free space or delete something first.
    """
    text = str(error or '').lower()
    return ('no space left on device' in text
            or 'disk quota' in text
            or 'enospc' in text
            or 'no space available' in text)


def _failure_plan(retry_attempts, max_retries, error):
    """Decide a download failure's fate.

    Returns (action, attempts, message) with action 'retry' or 'fail'.
    A full disk never retries; stale-helper failures carry the fix hint.
    """
    error_text = error or 'Conversion failed'
    if _looks_like_nospace(error_text):
        return ('fail', retry_attempts,
                'Disk filled up mid-download — free space and retry this track.')
    if 'disk space' in error_text.lower():
        # The pre-download free-space check; retrying is pointless.
        return ('fail', retry_attempts, _note_stale_helper(error_text))
    if retry_attempts < max_retries:
        attempts = retry_attempts + 1
        return ('retry', attempts,
                f'Retry {attempts}/{max_retries}: {error_text}')
    return ('fail', retry_attempts,
            _note_stale_helper(
                f'Max retries ({max_retries}) exceeded: {error_text}'))


def _note_stale_helper(error_text):
    """Flag a stale helper and append the fix hint. Returns the message."""
    message = error_text or 'Conversion failed'
    if _looks_stale(error_text):
        _stale_helper_event.set()
        message += (' — looks like an outdated downloader; '
                    'update yt-dlp in Settings › Helpers')
    return message


def _is_safe_origin():
    """True when a state-changing GET comes from this app (or has no origin).

    Browsers attach Origin/Referer to cross-site requests, so an evil.com
    <img> or link aimed at localhost carries a foreign origin and is
    rejected. Direct curl/address-bar use sends neither and keeps working.
    """
    for header in (request.headers.get('Origin'), request.headers.get('Referer')):
        if not header:
            continue
        try:
            host = urlparse(header).netloc.lower()
        except Exception:
            return False
        name = host.split(':')[0]
        if name not in ('127.0.0.1', 'localhost', '[::1]'):
            return False
    return True


def _same_origin_required(view):
    """Reject cross-site GETs that would otherwise trigger local actions."""
    from functools import wraps

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _is_safe_origin():
            return jsonify({'ok': False,
                            'message': 'Cross-site requests are not allowed'}), 403
        return view(*args, **kwargs)
    return wrapper


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
    """The standalone Convert page was merged into Home; keep the URL alive."""
    return redirect(url_for('home.index'))


@bp.route('/video')
def video_page():
    """Show the video conversion page."""
    default_output_path = effective_output_path()
    return render_template('video.html',
                           default_output_path=default_output_path,
                           request_path='/video')


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

    if not _is_supported_url(url):
        flash('That link is not from a supported site — use YouTube, '
              'SoundCloud, Spotify, Apple Music, or Tidal', 'error')
        return redirect(url_for('home.index'))

    if not output_path:
        output_path = effective_output_path()

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
        'meta': _cached_metadata(history.output_path),
    })


@bp.route('/api/like/<int:conversion_id>', methods=['POST'])
def toggle_like(conversion_id):
    """Heart/unheart a track for the Player tab library."""
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404
    history.liked = not history.liked
    db.session.commit()
    return jsonify({'ok': True, 'liked': bool(history.liked)})


@bp.route('/api/played/<int:conversion_id>', methods=['POST'])
def record_played(conversion_id):
    """Count a listen (called once per playback start by the player)."""
    from datetime import datetime
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404
    history.play_count = (history.play_count or 0) + 1
    history.last_played_at = utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'play_count': history.play_count})


@bp.route('/api/library')
def api_library():
    """Playable tracks for the Player tab library view.

    Optional paging for large libraries: ?limit= (default 1000, max 5000),
    ?offset=, and ?q= to pre-filter on filename/tags server-side. Always
    returns {items, total} so clients can show "showing X of Y".
    """
    try:
        limit = int(request.args.get('limit', 1000))
    except (TypeError, ValueError):
        limit = 1000
    limit = min(5000, max(1, limit))
    try:
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)
    query = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped']))
    q = (request.args.get('q') or '').strip()
    if q:
        escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        like = f'%{escaped}%'
        query = query.filter(or_(
            ConversionHistory.tag_title.ilike(like, escape='\\'),
            ConversionHistory.tag_artist.ilike(like, escape='\\'),
            ConversionHistory.tag_album.ilike(like, escape='\\')))
    total = query.count()
    rows = query.order_by(
        ConversionHistory.created_at.desc()).offset(offset).limit(limit).all()
    items = []
    for row in rows:
        if not row.output_path or not os.path.isfile(row.output_path):
            continue
        items.append(_serialize(row))
    return jsonify({'ok': True, 'items': items, 'total': total})


def _serialize_user_playlist(pl):
    from app.models import PlayerPlaylistItem
    items = db.session.query(PlayerPlaylistItem).filter_by(
        playlist_id=pl.id).order_by(PlayerPlaylistItem.position.asc()).all()
    tracks = []
    for i in items:
        track = db.session.get(ConversionHistory, i.conversion_id)
        if track is None:
            title = 'Missing track'
        elif track.output_path:
            title = os.path.splitext(os.path.basename(track.output_path))[0]
        else:
            title = track.playlist_title or 'Track'
        tracks.append({'item_id': i.id, 'conversion_id': i.conversion_id,
                       'title': title})
    return {'id': pl.id, 'name': pl.name, 'tracks': tracks,
            'count': len(tracks)}


@bp.route('/api/playlists')
def api_user_playlists():
    """List the user's Player-tab playlists."""
    from app.models import PlayerPlaylist
    playlists = db.session.query(PlayerPlaylist).order_by(
        PlayerPlaylist.created_at.asc()).all()
    return jsonify({'ok': True,
                    'items': [_serialize_user_playlist(p) for p in playlists]})


@bp.route('/api/playlists', methods=['POST'])
def api_playlist_create():
    """Create a user playlist."""
    from app.models import PlayerPlaylist
    payload = request.get_json(silent=True) or {}
    name = str(payload.get('name') or '').strip()[:200]
    if not name:
        return jsonify({'ok': False, 'message': 'Name the playlist'}), 400
    pl = PlayerPlaylist(name=name)
    db.session.add(pl)
    db.session.commit()
    return jsonify({'ok': True, 'playlist': _serialize_user_playlist(pl)})


@bp.route('/api/playlists/<int:playlist_id>')
def api_user_playlist_items(playlist_id):
    """A user playlist's tracks in order, playable ones first-class."""
    from app.models import PlayerPlaylist, PlayerPlaylistItem
    pl = db.session.get(PlayerPlaylist, playlist_id)
    if not pl:
        return jsonify({'ok': False, 'message': 'Playlist not found'}), 404
    items = db.session.query(PlayerPlaylistItem).filter_by(
        playlist_id=pl.id).order_by(PlayerPlaylistItem.position.asc()).all()
    tracks = []
    for entry in items:
        track = db.session.get(ConversionHistory, entry.conversion_id)
        if track is None:
            continue
        serialized = _serialize(track)
        serialized['item_id'] = entry.id
        tracks.append(serialized)
    return jsonify({'ok': True, 'playlist': {'id': pl.id, 'name': pl.name},
                    'items': tracks})


@bp.route('/api/playlists/<int:playlist_id>/add', methods=['POST'])
def api_playlist_add(playlist_id):
    """Append a finished track to a user playlist."""
    from app.models import PlayerPlaylist, PlayerPlaylistItem
    pl = db.session.get(PlayerPlaylist, playlist_id)
    if not pl:
        return jsonify({'ok': False, 'message': 'Playlist not found'}), 404
    payload = request.get_json(silent=True) or {}
    try:
        conversion_id = int(payload.get('conversion_id', 0))
    except (TypeError, ValueError):
        conversion_id = 0
    track = db.session.get(ConversionHistory, conversion_id)
    if not track or track.status not in ('completed', 'skipped'):
        return jsonify({'ok': False,
                        'message': 'Only finished tracks can be added'}), 400
    top = db.session.query(PlayerPlaylistItem).filter_by(
        playlist_id=pl.id).order_by(
        PlayerPlaylistItem.position.desc()).first()
    entry = PlayerPlaylistItem(playlist_id=pl.id, conversion_id=track.id,
                               position=(top.position + 1) if top else 0)
    db.session.add(entry)
    db.session.commit()
    return jsonify({'ok': True, 'playlist': _serialize_user_playlist(pl)})


@bp.route('/api/playlists/<int:playlist_id>/remove', methods=['POST'])
def api_playlist_remove(playlist_id):
    """Remove one entry from a user playlist (track file is kept)."""
    from app.models import PlayerPlaylist, PlayerPlaylistItem
    payload = request.get_json(silent=True) or {}
    try:
        item_id = int(payload.get('item_id', 0))
    except (TypeError, ValueError):
        item_id = 0
    entry = db.session.get(PlayerPlaylistItem, item_id)
    if not entry or entry.playlist_id != playlist_id:
        return jsonify({'ok': False, 'message': 'Entry not found'}), 404
    db.session.delete(entry)
    db.session.commit()
    pl = db.session.get(PlayerPlaylist, playlist_id)
    return jsonify({'ok': True, 'playlist': _serialize_user_playlist(pl)})


@bp.route('/api/playlists/<int:playlist_id>/move', methods=['POST'])
def api_playlist_move(playlist_id):
    """Move a playlist entry up or down one slot."""
    from app.models import PlayerPlaylistItem
    payload = request.get_json(silent=True) or {}
    try:
        item_id = int(payload.get('item_id', 0))
    except (TypeError, ValueError):
        item_id = 0
    direction = -1 if str(payload.get('direction') or '') == 'up' else 1
    entries = db.session.query(PlayerPlaylistItem).filter_by(
        playlist_id=playlist_id).order_by(
        PlayerPlaylistItem.position.asc()).all()
    index = next((i for i, e in enumerate(entries) if e.id == item_id), None)
    if index is None or not 0 <= index + direction < len(entries):
        return jsonify({'ok': False, 'message': 'Cannot move it that way'}), 400
    other = entries[index + direction]
    entry = entries[index]
    entry.position, other.position = other.position, entry.position
    db.session.commit()
    return jsonify({'ok': True})


@bp.route('/api/playlists/<int:playlist_id>/delete', methods=['POST'])
def api_playlist_delete(playlist_id):
    """Delete a user playlist (track files are kept)."""
    from app.models import PlayerPlaylist, PlayerPlaylistItem
    pl = db.session.get(PlayerPlaylist, playlist_id)
    if not pl:
        return jsonify({'ok': False, 'message': 'Playlist not found'}), 404
    db.session.query(PlayerPlaylistItem).filter_by(
        playlist_id=pl.id).delete()
    db.session.delete(pl)
    db.session.commit()
    return jsonify({'ok': True})


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
        old_path = history.output_path
        os.rename(history.output_path, target)
    except OSError as e:
        return jsonify({'ok': False, 'message': f'Could not rename: {str(e)}'}), 500
    history.output_path = target
    db.session.commit()
    _invalidate_stat(old_path)
    _invalidate_stat(target)
    return jsonify({'ok': True, 'filename': os.path.basename(target),
                    'output_path': target})


def _write_tags(path, tags):
    """Rewrite a file's embedded title/artist/album tags, atomically.

    Returns (ok, error_message). Only non-empty tag values are written;
    other tags on the file are preserved.
    """
    clean = {key: str(tags.get(key) or '').strip()[:100]
             for key in ('title', 'artist', 'album')}
    if not any(clean.values()):
        return False, 'Provide at least one tag'
    fd, tmp_path = tempfile.mkstemp(
        suffix=os.path.splitext(path)[1] or '.tmp')
    os.close(fd)
    try:
        args = ['ffmpeg', '-y', '-i', path, '-map', '0', '-c', 'copy']
        for key, value in clean.items():
            if value:
                args += ['-metadata', f'{key}={value}']
        args.append(tmp_path)
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=120)
        if result.returncode != 0 or not os.path.exists(tmp_path):
            return False, 'Could not write tags to this file'
        os.replace(tmp_path, path)
    except Exception as e:
        _cleanup(tmp_path)
        return False, str(e)
    _meta_cache.pop(path, None)
    return True, ''


@bp.route('/api/metadata/<int:conversion_id>', methods=['POST'])
def edit_metadata(conversion_id):
    """Rewrite a converted file's embedded title/artist/album tags."""
    history = _playable_file_or_404(conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'No playable file'}), 404

    payload = request.get_json(silent=True) or {}
    ok, error = _write_tags(history.output_path,
                            {key: payload.get(key)
                             for key in ('title', 'artist', 'album')})
    if not ok:
        return jsonify({'ok': False, 'message': error}), 400
    _store_file_facts(history, history.output_path)
    return jsonify({'ok': True, 'meta': _probe_metadata(history.output_path)})


@bp.route('/api/tags/bulk', methods=['POST'])
def bulk_edit_tags():
    """Apply the same title/artist/album tags to many tracks at once.

    Only provided (non-empty) fields are written; each file keeps its other
    tags. Returns per-track results so one bad file never blocks the rest.
    """
    payload = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in (payload.get('ids') or [])][:200]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'message': 'Invalid track list'}), 400
    tags = {key: str(payload.get(key) or '').strip()[:100]
            for key in ('title', 'artist', 'album')}
    if not ids:
        return jsonify({'ok': False, 'message': 'No tracks selected'}), 400
    if not any(tags.values()):
        return jsonify({'ok': False, 'message': 'Provide at least one tag'}), 400
    updated, failed = 0, 0
    for track_id in ids:
        history = db.session.get(ConversionHistory, track_id)
        if (not history or history.status not in ('completed', 'skipped')
                or not history.output_path
                or not os.path.isfile(history.output_path)):
            failed += 1
            continue
        ok, _error = _write_tags(history.output_path, tags)
        if ok:
            _store_file_facts(history, history.output_path)
            updated += 1
        else:
            failed += 1
    return jsonify({'ok': True, 'updated': updated, 'failed': failed})


_COVER_CACHE_CAP = 500


def _prune_cover_cache():
    """Bound the thumbnail cache (LRU by mtime): it otherwise grows forever
    in system temp, one pair of JPEGs per track played."""
    try:
        cache_dir = os.path.join(tempfile.gettempdir(),
                                 'audio-converter-covers')
        names = os.listdir(cache_dir)
    except OSError:
        return 0
    if len(names) <= _COVER_CACHE_CAP:
        return 0
    try:
        ranked = sorted(
            ((os.path.getmtime(os.path.join(cache_dir, n)), n) for n in names
             if os.path.isfile(os.path.join(cache_dir, n))))
    except OSError:
        return 0
    removed = 0
    for _mtime, name in ranked[:len(ranked) - _COVER_CACHE_CAP]:
        try:
            os.remove(os.path.join(cache_dir, name))
            removed += 1
        except OSError:
            pass
    return removed


@bp.route('/api/cover/<int:conversion_id>')
def cover_art(conversion_id):
    """Serve a converted file's cover art (cached per file).

    Prefers art embedded in the file; falls back to the `<track>.cover.jpg`
    sidecar written for formats whose containers cannot hold pictures.
    `?size=thumb` serves a 160px variant for grids and the player bar.
    """
    history = _playable_file_or_404(conversion_id)
    if not history:
        abort(404)

    thumb = request.args.get('size') == 'thumb'
    cache_dir = os.path.join(tempfile.gettempdir(), 'audio-converter-covers')
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        abort(404)
    cached = os.path.join(cache_dir,
                          f'{history.id}-thumb.jpg' if thumb else f'{history.id}.jpg')
    try:
        fresh = (os.path.isfile(cached) and
                 os.path.getmtime(cached) >= os.path.getmtime(history.output_path))
    except OSError:
        fresh = False
    if not fresh:
        extract = ['ffmpeg', '-y', '-v', 'error', '-i', history.output_path, '-an']
        if thumb:
            extract += ['-vf', 'scale=160:-1', '-vframes', '1']
        else:
            extract += ['-vcodec', 'copy']
        extract.append(cached)
        result = subprocess.run(extract, capture_output=True, text=True,
                               timeout=30)
        if result.returncode != 0 or not os.path.isfile(cached):
            _cleanup(cached)
    if not os.path.isfile(cached):
        sidecar = os.path.splitext(history.output_path)[0] + '.cover.jpg'
        if os.path.isfile(sidecar):
            if thumb:
                scaled = os.path.join(cache_dir, f'{history.id}-thumb.jpg')
                thumb_ok = subprocess.run(
                    ['ffmpeg', '-y', '-v', 'error', '-i', sidecar,
                     '-vf', 'scale=160:-1', '-vframes', '1', scaled],
                    capture_output=True, text=True, timeout=30)
                if thumb_ok.returncode == 0 and os.path.isfile(scaled):
                    return send_file(scaled, mimetype='image/jpeg')
            else:
                return send_file(sidecar, mimetype='image/jpeg')
        # Last resort for videos: grab the first frame as the poster.
        ext = os.path.splitext(history.output_path)[1].lower().lstrip('.')
        if ext in BROWSER_VIDEO_EXTS + ('mkv', 'mov', 'avi'):
            frame = os.path.join(
                cache_dir, f'{history.id}-frame{"-thumb" if thumb else ""}.jpg')
            grab = ['ffmpeg', '-y', '-v', 'error', '-i', history.output_path]
            if thumb:
                grab += ['-vf', 'scale=160:-1']
            grab += ['-vframes', '1', frame]
            frame_ok = subprocess.run(
                grab, capture_output=True, text=True, timeout=30)
            if frame_ok.returncode == 0 and os.path.isfile(frame):
                _prune_cover_cache()
                return send_file(frame, mimetype='image/jpeg')
        abort(404)
    _prune_cover_cache()
    return send_file(cached, mimetype='image/jpeg')


@bp.route('/api/maintenance/clear-covers', methods=['POST'])
def api_clear_covers():
    """Wipe the entire cover-thumbnail cache (it rebuilds on demand)."""
    cleared = 0
    try:
        cache_dir = os.path.join(tempfile.gettempdir(),
                                 'audio-converter-covers')
        for name in os.listdir(cache_dir):
            try:
                os.remove(os.path.join(cache_dir, name))
                cleared += 1
            except OSError:
                pass
    except OSError:
        pass
    return jsonify({'ok': True, 'cleared': cleared})


_AUTOSTART_LABEL = 'com.audioconverter.app'


def _autostart_command():
    """How to launch this app again: the frozen binary, or dev interpreter."""
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    script = os.path.join(os.path.dirname(__file__), '..', 'desktop.py')
    return [sys.executable, os.path.abspath(script)]


def _autostart_paths():
    """(kind, target) describing this platform's autostart slot."""
    home = os.path.expanduser('~')
    if sys.platform == 'darwin':
        return ('launchd', os.path.join(
            home, 'Library', 'LaunchAgents',
            _AUTOSTART_LABEL + '.plist'))
    if sys.platform.startswith('win'):
        return ('registry', None)
    return ('desktop-file', os.path.join(
        home, '.config', 'autostart', 'audioconverter.desktop'))


def _autostart_enabled():
    """Whether launch-at-login is currently switched on."""
    kind, target = _autostart_paths()
    try:
        if kind == 'registry':
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Run',
                                0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, 'AudioConverter')
            return True
        return bool(target and os.path.isfile(target))
    except Exception:
        return False


def _set_autostart(enabled):
    """Switch launch-at-login on/off. Returns (ok, message)."""
    kind, target = _autostart_paths()
    try:
        if kind == 'launchd':
            agents = os.path.dirname(target)
            if enabled:
                os.makedirs(agents, exist_ok=True)
                from xml.sax.saxutils import escape as _xml_escape
                args = ''.join(
                    f'        <string>{_xml_escape(a)}</string>\n'
                    for a in _autostart_command())
                plist = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0">\n<dict>\n'
                    f'    <key>Label</key>\n    <string>{_AUTOSTART_LABEL}</string>\n'
                    '    <key>ProgramArguments</key>\n    <array>\n'
                    f'{args}'
                    '    </array>\n'
                    '    <key>RunAtLoad</key>\n    <true/>\n'
                    '</dict>\n</plist>\n')
                with open(target, 'w', encoding='utf-8') as f:
                    f.write(plist)
                subprocess.run(
                    ['launchctl', 'load', target],
                    capture_output=True, timeout=15)
            else:
                subprocess.run(
                    ['launchctl', 'unload', target],
                    capture_output=True, timeout=15)
                if os.path.isfile(target):
                    os.remove(target)
            return True, 'Login item updated.'
        if kind == 'registry':
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r'Software\Microsoft\Windows\CurrentVersion\Run',
                    0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, 'AudioConverter', 0,
                                      winreg.REG_SZ,
                                      subprocess.list2cmdline(
                                          _autostart_command()))
                else:
                    try:
                        winreg.DeleteValue(key, 'AudioConverter')
                    except FileNotFoundError:
                        pass
            return True, 'Login item updated.'
        if enabled:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, 'w', encoding='utf-8') as f:
                f.write('[Desktop Entry]\nType=Application\n'
                        'Name=Audio Converter\n'
                        f'Exec={" ".join(_autostart_command())}\n'
                        'Terminal=false\nX-GNOME-Autostart-enabled=true\n')
        elif os.path.isfile(target):
            os.remove(target)
        return True, 'Login item updated.'
    except Exception as e:
        return False, f'Could not change login item: {str(e)}'


@bp.route('/api/autostart')
def api_autostart():
    """Launch-at-login state for the maintenance panel."""
    return jsonify({'ok': True, 'supported': True,
                    'enabled': _autostart_enabled(),
                    'platform': sys.platform})


@bp.route('/api/autostart', methods=['POST'])
def api_set_autostart():
    """Flip launch-at-login on/off."""
    payload = request.get_json(silent=True) or {}
    ok, message = _set_autostart(bool(payload.get('enabled', False)))
    if not ok:
        return jsonify({'ok': False, 'message': message}), 500
    return jsonify({'ok': True, 'enabled': _autostart_enabled(),
                    'message': message})


@bp.route('/api/maintenance/vacuum', methods=['POST'])
def api_vacuum():
    """Compact the library database (VACUUM) plus prune the cover cache.

    WAL databases never shrink on their own; this reclaims space after
    heavy delete/prune sessions. Runs in a worker-safe way: VACUUM needs
    no other writer mid-flight, so failures roll back to a retryable
    message instead of risking the live DB.
    """
    pruned = _prune_cover_cache()
    try:
        db_path = db.engine.url.database
    except Exception:
        db_path = None
    if not db_path or db_path == ':memory:' or not os.path.isfile(db_path):
        return jsonify({'ok': False, 'message': 'No local database found'}), 404
    try:
        import sqlite3
        con = sqlite3.connect(db_path, timeout=30)
        try:
            con.execute('VACUUM')
            con.commit()
        finally:
            con.close()
    except Exception as e:
        return jsonify({'ok': False,
                        'message': f'Vacuum failed (try again when idle): {str(e)}'}), 500
    try:
        size = os.path.getsize(db_path)
    except OSError:
        size = 0
    return jsonify({'ok': True, 'db_bytes': size, 'covers_pruned': pruned})


@bp.route('/api/subs/<int:conversion_id>')
def api_subs_list(conversion_id):
    """Subtitle tracks sitting next to a converted video."""
    history = _playable_file_or_404(conversion_id)
    if not history:
        abort(404)
    subs = [{'lang': lang,
             'url': f'/api/subs/{conversion_id}/{lang}'}
            for lang, _path in _subtitle_tracks(history.output_path)]
    return jsonify({'ok': True, 'subs': subs})


@bp.route('/api/subs/<int:conversion_id>/<lang>')
def api_sub_file(conversion_id, lang):
    """Serve one subtitle sidecar. The language tag is strictly validated
    so this can never escape the track's own folder."""
    history = _playable_file_or_404(conversion_id)
    if not history or not re.fullmatch(r'[A-Za-z-]{2,12}', lang or ''):
        abort(404)
    stem = os.path.splitext(history.output_path)[0]
    for ext, mimetype in (('.vtt', 'text/vtt'), ('.srt', 'text/plain')):
        path = f'{stem}.{lang}{ext}'
        if os.path.isfile(path):
            return send_file(path, mimetype=mimetype)
    abort(404)


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
            if job is not None and job.id in _paused_jobs:
                return {'success': False, 'error': 'Paused'}
            for alternate in find_alternate_sources(url, meta, expected_title):
                if job is not None and job.id in _paused_jobs:
                    return {'success': False, 'error': 'Paused'}
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

        _collect_subtitles(temp_audio, output_file)

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

        if job is not None:
            try:
                job.cover_url = str(meta.get('thumbnail') or '')[:1000]
                db.session.flush()
            except Exception:
                db.session.rollback()
        return {'success': True, 'filepath': output_file, 'filename': os.path.basename(output_file),
                'via': downloaded_from}

    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Conversion timed out'}
    except FileNotFoundError:
        return {'success': False, 'error': 'yt-dlp not found! Install it: pip install yt-dlp'}
    except Exception as e:
        return {'success': False, 'error': f'Conversion failed: {str(e)}'}
    finally:
        # A paused job keeps its partial download so resume continues it.
        if job is None or job.id not in _paused_jobs:
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


# Hosts the app knows how to convert or import from. Anything else is
# rejected at submit time: an arbitrary URL would just burn a worker on a
# yt-dlp failure, and intranet/metadata addresses must never reach it.
_SUPPORTED_HOST_SUFFIXES = (
    'youtube.com', 'youtu.be', 'music.youtube.com',
    'soundcloud.com',
    'open.spotify.com', 'music.apple.com', 'tidal.com',
)


def _is_supported_url(url):
    """http(s) URL on a supported content host, nothing else."""
    try:
        parts = urlparse(url or '')
    except Exception:
        return False
    if parts.scheme not in ('http', 'https'):
        return False
    host = (parts.hostname or '').lower()
    if not host:
        return False
    return any(host == suffix or host.endswith('.' + suffix)
               for suffix in _SUPPORTED_HOST_SUFFIXES)


def _is_public_http_url(url, timeout=5):
    """True when a URL is safe for the server itself to fetch.

    Blocks non-http(s) schemes, literal private/loopback/link-local IPs, and
    hostnames that resolve to non-public addresses, so cover-art and metadata
    fetches can't be steered at the local network or cloud metadata endpoints.
    """
    import ipaddress
    import socket
    try:
        parts = urlparse(url or '')
    except Exception:
        return False
    if parts.scheme not in ('http', 'https'):
        return False
    host = (parts.hostname or '').strip()
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except Exception:
            return False
        if not ip.is_global:
            return False
    return True


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
    if not _is_public_http_url(url):
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
    if VALID_FORMATS[format_type].get('kind') == 'video':
        return convert_video_file(input_file, format_type, output_file, meta)
    args = ['ffmpeg', '-y']
    if cover_file and format_type in COVER_FORMATS:
        args += ['-i', input_file, '-i', cover_file]
    else:
        args += ['-i', input_file]

    args += ['-map', '0:a']
    if cover_file and format_type in COVER_FORMATS:
        args += ['-map', '1:v', '-c:v', 'copy', '-disposition:v', 'attached_pic']

    args += _format_args(format_type)
    if _want_normalize():
        # Single-pass EBU R128 normalization so playlists play at even volume.
        args += ['-filter:a', 'loudnorm']

    for key in ('title', 'artist', 'album', 'date'):
        value = meta.get(key)
        if value:
            args += ['-metadata', f'{key}={value}']

    # Cap encoder threads so parallel conversions share the CPU with the UI
    # instead of starving it.
    args += ['-threads', '2']
    args.append(output_file)
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)

    if result.returncode == 0 and os.path.exists(output_file):
        return {'success': True}
    else:
        error_msg = result.stderr.strip()[-500:] if result.stderr else 'FFmpeg returned non-zero exit code'
        return {'success': False, 'error': f'FFmpeg conversion failed: {error_msg}'}


def convert_video_file(input_file, format_type, output_file, meta):
    """Convert a download to a portable video file with metadata.

    MP4 is re-encoded to H.264/AAC (plays everywhere, faststart-tagged for
    seeking during streaming). WebM and MKV are stream-copied: YouTube's
    VP9/Opus sources fit those containers as-is, which keeps conversion
    fast and lossless; anything else fails loudly with ffmpeg's reason.
    Cover art rides alongside as a sidecar (see download_and_convert).
    """
    fmt = VALID_FORMATS[format_type]
    args = ['ffmpeg', '-y', '-i', input_file]
    normalize = _want_normalize()
    if fmt['ext'] == 'mp4':
        args += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                 '-c:a', 'aac', '-movflags', '+faststart']
        if normalize:
            args += ['-filter:a', 'loudnorm']
    elif normalize:
        # A filter forces a re-encode, so copy paths pick per-container
        # codecs instead of failing on `-c copy` plus a filter.
        audio_codec = 'libopus' if fmt['ext'] == 'webm' else 'aac'
        args += ['-c:v', 'copy', '-c:a', audio_codec, '-filter:a', 'loudnorm']
    else:
        args += ['-c', 'copy']

    for key in ('title', 'artist', 'album', 'date'):
        value = meta.get(key)
        if value:
            args += ['-metadata', f'{key}={value}']

    args += ['-threads', '2']
    args.append(output_file)
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)

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


def _want_sponsorblock():
    """Context-safe read of the sponsor-skipping toggle (default on)."""
    try:
        settings = UserSettings.query.first()
        if settings is None:
            return True
        return bool(getattr(settings, 'sponsorblock', True))
    except Exception:
        return True


def _want_normalize():
    """Context-safe read of the loudness toggle (default off)."""
    try:
        settings = UserSettings.query.first()
        if settings is None:
            return False
        return bool(getattr(settings, 'normalize_audio', False))
    except Exception:
        return False


def _want_subtitles():
    """Context-safe read of the subtitle toggle (default on)."""
    try:
        settings = UserSettings.query.first()
        if settings is None:
            return True
        return bool(getattr(settings, 'subtitles', True))
    except Exception:
        return True


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


def _active_bandwidth_limit(now=None):
    """Effective download cap in KB/s: the off-peak override wins inside
    its window ([start, end), wrapping past midnight), else the flat limit.
    `now` is injectable for tests."""
    try:
        settings = UserSettings.query.first()
        limit = int(getattr(settings, 'bandwidth_limit', 0) or 0)
        off = int(getattr(settings, 'offpeak_limit', 0) or 0)
        start = int(getattr(settings, 'offpeak_start', 22) or 0)
        end = int(getattr(settings, 'offpeak_end', 7) or 0)
    except Exception:
        return 0
    if off > 0:
        from datetime import datetime as _dt
        hour = (now or _dt.now()).hour
        in_window = (start <= hour < end) if start <= end else (hour >= start or hour < end)
        if in_window:
            return off
    return limit


def _ytdlp_net_args():
    """Proxy + speed-limit flags for yt-dlp. Safe without an app context."""
    args = []
    proxy = _get_proxy()
    if proxy:
        args += ['--proxy', proxy]
    limit = _active_bandwidth_limit()
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


def _download_kind(job):
    """'video' when the queued job wants video, else 'audio'."""
    if job is None:
        return 'audio'
    return VALID_FORMATS.get(LABEL_TO_KEY.get(job.format, ''), {}).get('kind', 'audio')


def _download_selector(job):
    # Video is capped at 1080p: 4K re-encodes blow past any sane timeout.
    if _download_kind(job) == 'video':
        return 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
    return 'bestaudio/best'


def download_audio(url, temp_audio, job=None):
    """Download best-quality audio (or video), reporting live progress."""
    selector = _download_selector(job)
    base = (list(_YTDLP_PRIVACY_FLAGS) + _ytdlp_net_args()
            + ['--no-playlist', '--continue', '-f', selector,
               '--newline', '-o', temp_audio])
    if _download_kind(job) == 'video':
        if _want_sponsorblock():
            base += ['--sponsorblock-remove', 'sponsor,intro,outro,selfpromo,interaction']
        if _want_subtitles():
            base += ['--write-subs', '--write-auto-subs',
                     '--sub-langs', 'all,-live_chat', '--convert-subs', 'vtt']
    cmd = _ytdlp_command() + base + [url]

    proc = None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    if job is not None:
        with _paused_lock:
            _active_downloads[job.id] = proc
    try:
        for line in iter(proc.stdout.readline, ''):
            match = re.search(r'(?:^|\s)\[download\]', line)
            if match and '%' in line:
                pct_match = re.search(r'(\d+(?:\.\d+)?)%', line)
                if pct_match and job:
                    pct = min(90, 5 + float(pct_match.group(1)) * 0.85)
                    _job_progress(job, pct)
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
    finally:
        if job is not None:
            with _paused_lock:
                if _active_downloads.get(job.id) is proc:
                    del _active_downloads[job.id]

    if proc.returncode != 0 and _is_youtube(url):
        fallback = (_active_privacy_flags() + _ytdlp_net_args()
                    + ['--no-playlist', '-f', selector, '-o', temp_audio,
                       '--extractor-args', 'youtube:player_client=android_vr,tv,web_embedded', url])
        retry = subprocess.run(_ytdlp_command() + fallback,
                               capture_output=True, text=True, timeout=300)
        if retry.returncode == 0:
            if _normalize_download(temp_audio):
                return {'success': True}
            return {'success': False, 'error': 'No audio file was downloaded'}

    if proc.returncode != 0:
        return {'success': False, 'error': 'yt-dlp download failed'}

    if _normalize_download(temp_audio):
        return {'success': True}

    return {'success': False, 'error': 'No audio file was downloaded'}


def _collect_subtitles(temp_audio, output_file):
    """Move downloaded subtitle sidecars next to the finished file.

    yt-dlp writes `<temp>.<lang>.vtt`; these become `<title>.<lang>.vtt`
    beside the output so players (and our theater) pick them up.
    Returns the list of (lang, path) moved.
    """
    moved = []
    stem = os.path.splitext(output_file)[0]
    for candidate in sorted(glob.glob(temp_audio + '.*.vtt') +
                            glob.glob(temp_audio + '.*.srt')):
        base = os.path.basename(candidate)
        parts = base.split('.')
        if len(parts) < 3:
            continue
        lang = parts[-2]
        if not re.fullmatch(r'[A-Za-z-]{2,12}', lang):
            continue
        ext = '.vtt' if candidate.endswith('.vtt') else '.srt'
        target = stem + '.' + lang + ext
        try:
            if os.path.abspath(target) != os.path.abspath(candidate):
                if os.path.exists(target):
                    os.remove(target)
                os.replace(candidate, target)
            moved.append((lang, target))
        except OSError:
            pass
    return moved


def _subtitle_tracks(output_path):
    """(lang, path) subtitle sidecars already sitting next to a file."""
    stem = os.path.splitext(output_path)[0]
    found = []
    for candidate in sorted(glob.glob(stem + '.*.vtt') +
                            glob.glob(stem + '.*.srt')):
        parts = os.path.basename(candidate).split('.')
        if len(parts) >= 3 and re.fullmatch(r'[A-Za-z-]{2,12}', parts[-2]):
            found.append((parts[-2], candidate))
    return found


def _normalize_download(temp_audio):
    """Resolve the real downloaded file, collapsing merged outputs.

    Video merges land as `<temp>.mkv`/`<temp>.mp4` rather than exactly at
    temp_audio; move that back so the rest of the pipeline sees one path.
    Returns the path, or None when nothing usable arrived.
    """
    if os.path.isfile(temp_audio):
        return temp_audio
    for candidate in sorted(glob.glob(temp_audio + '.*')):
        if candidate.endswith('.part'):
            continue
        if os.path.isfile(candidate):
            try:
                os.replace(candidate, temp_audio)
            except OSError:
                return candidate
            return temp_audio
    return None


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
            'https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest',
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




@bp.route('/api/helpers')
def api_helpers():
    """Installed helper versions plus whether a newer yt-dlp release exists."""
    from app import _version_tuple
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


def _perform_ytdlp_update():
    """Replace the yt-dlp helper binary with the latest release build.

    Shared by the manual endpoint and the opt-in auto-updater. Returns
    (ok, message, version_or_None). Only touches the standalone binary
    (bundled app or a PATH install that is a real file we can write);
    pip-based installs are left alone with guidance instead of a risky
    in-place pip upgrade.
    """
    exe = _find_ytdlp()
    if not exe or not os.path.isfile(exe):
        return False, 'No standalone yt-dlp binary found to update', None
    try:
        if not os.access(exe, os.W_OK):
            raise OSError('not writable')
    except OSError:
        return False, 'yt-dlp is managed by pip here; run: pip install -U yt-dlp', None

    if sys.platform == 'darwin':
        asset = 'yt-dlp_macos'
    elif sys.platform.startswith('win'):
        asset = 'yt-dlp.exe'
    else:
        asset = 'yt-dlp_linux'

    latest = _latest_ytdlp_release()
    if not latest or asset not in latest.get('assets', {}):
        return False, 'Could not find a fresh yt-dlp build', None
    tmp_path = ''
    try:
        expected = _asset_sha256(latest.get('assets', {}), asset)
        if not expected:
            raise ValueError('no published checksum for this build — '
                             'refusing to install an unverified binary')
        fd, tmp_path = tempfile.mkstemp(prefix='ytdlp-update-')
        os.close(fd)
        actual = _stream_download(latest['assets'][asset], tmp_path,
                                  timeout=300)
        if actual != expected:
            raise ValueError('checksum mismatch — the download may be '
                             'corrupt or tampered with')
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, exe)
    except Exception as e:
        _cleanup(tmp_path)
        return False, f'Update failed: {str(e)}', None
    _stale_helper_event.clear()
    return True, f"yt-dlp updated to {latest['version']}.", latest['version']


@bp.route('/api/helpers/update-ytdlp', methods=['POST'])
def api_update_ytdlp():
    """Replace the yt-dlp helper binary with the latest release build."""
    ok, message, version = _perform_ytdlp_update()
    if not ok:
        return jsonify({'ok': False, 'message': message}), 400
    return jsonify({'ok': True, 'version': version, 'message': message})


def _asset_sha256(assets, name, sums_asset='SHA2-256SUMS', timeout=60):
    """Expected SHA-256 of a release asset per the published checksums file.

    Returns the hex digest, or None when the sums file is missing/unparseable.
    `sums_asset` is 'SHA2-256SUMS' for yt-dlp releases and 'SHA256SUMS.txt'
    for our own installer releases (same `<hash>  <filename>` format).
    """
    sums_url = assets.get(sums_asset)
    if not sums_url:
        return None
    try:
        session = requests.Session()
        session.cookies.clear()
        resp = session.get(sums_url, timeout=timeout,
                           headers={'User-Agent': 'AudioConverter/1.0'})
        if resp.status_code != 200 or not resp.text:
            return None
    except Exception:
        return None
    for line in resp.text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip('*') == name:
            digest = parts[0].lower()
            if len(digest) == 64 and all(c in '0123456789abcdef' for c in digest):
                return digest
    return None


def _stream_download(url, dest, timeout=600):
    """Stream a URL to `dest` without buffering it in RAM.

    Installers are hundreds of MB; holding them in memory risks swapping
    or OOM on small machines. Returns the SHA-256 hex digest so callers
    can verify before trusting the file. Raises on HTTP errors or empty
    bodies.
    """
    digest = hashlib.sha256()
    total = 0
    session = requests.Session()
    session.cookies.clear()
    with session.get(url, timeout=timeout, stream=True,
                     headers={'User-Agent': 'AudioConverter/1.0'}) as resp:
        if resp.status_code != 200:
            raise ValueError('download failed')
        with open(dest, 'wb') as f:
            for piece in resp.iter_content(chunk_size=1024 * 1024):
                if not piece:
                    continue
                f.write(piece)
                digest.update(piece)
                total += len(piece)
    if not total:
        raise ValueError('download failed')
    return digest.hexdigest()


def _verify_installer_bytes(release, name, digest):
    """Raise ValueError unless `digest` matches the release's SHA256SUMS.txt.

    Refuses when the checksums file is absent: releases published by the
    updated build workflow always carry one, so a missing file means the
    payload can't be trusted, not that verification is optional.
    """
    assets = release.get('assets') if isinstance(release, dict) else None
    asset_map = {str(a.get('name') or ''): a.get('browser_download_url') or ''
                 for a in (assets or [])}
    expected = _asset_sha256(asset_map, name, sums_asset='SHA256SUMS.txt')
    if not expected:
        raise ValueError('no published checksum for this installer — '
                         'download it from the release page instead')
    if digest != expected:
        raise ValueError('checksum mismatch — the installer may be corrupt '
                         'or tampered with')


def _running_bundle_dir():
    """The installed .app bundle dir when running from /Applications."""
    if not getattr(sys, 'frozen', False) or sys.platform != 'darwin':
        return None
    macos_dir = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.basename(macos_dir) != 'MacOS':
        return None
    bundle = os.path.dirname(os.path.dirname(macos_dir))
    if not bundle.endswith('.app'):
        return None
    if os.path.realpath(bundle) != '/Applications/AudioConverter.app':
        return None
    return bundle


@bp.route('/api/update-install', methods=['POST'])
def api_update_install():
    """Install the latest release with the least possible friction.

    macOS bundle in /Applications: downloads the disk image, swaps the app
    in, and relaunches. Windows: downloads the Setup wizard and opens it
    (it handles elevation itself). Anything else gets the manual link,
    because a failed self-replace is the one unrecoverable state.
    """
    from app.routes.settings import _default_update_feed
    if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
        return _macos_update_install()
    if getattr(sys, 'frozen', False) and sys.platform.startswith('win'):
        return _windows_update_install()
    return jsonify({'ok': False,
                    'message': 'Automatic install works from the installed '
                               'macOS (/Applications) or Windows app. Download '
                               'the installer from the release page instead.'}), 400


def _find_asset_by_suffix(release, suffixes):
    assets = release.get('assets') if isinstance(release, dict) else None
    for asset in assets or []:
        name = str(asset.get('name') or '')
        url = asset.get('browser_download_url') or ''
        if url and any(name.endswith(suffix) for suffix in suffixes):
            return name, url
    return None, None


def _macos_update_install():
    """Swap the /Applications bundle for the latest disk image contents."""
    from app.routes.settings import _default_update_feed
    bundle = _running_bundle_dir()
    if bundle is None:
        return jsonify({'ok': False,
                        'message': 'Automatic install works from the '
                                   'Applications copy of the app.'}), 400
    try:
        resp = requests.get(_default_update_feed(), timeout=20,
                            headers={'Accept': 'application/json'})
        release = resp.json() if resp.status_code == 200 else {}
    except Exception:
        release = {}
    _name, url = _find_asset_by_suffix(release, ('.dmg',))
    if not url:
        return jsonify({'ok': False,
                        'message': 'No macOS installer found in the latest release.'}), 502
    dmg_name = _name

    workdir = tempfile.mkdtemp(prefix='audio-converter-update-')
    dmg_path = os.path.join(workdir, 'update.dmg')
    mount = os.path.join(workdir, 'mnt')
    os.makedirs(mount, exist_ok=True)
    try:
        digest = _stream_download(url, dmg_path, timeout=600)
        _verify_installer_bytes(release, dmg_name, digest)
        attached = subprocess.run(
            ['hdiutil', 'attach', '-nobrowse', '-readonly',
             '-mountpoint', mount, dmg_path],
            capture_output=True, text=True, timeout=60)
        if attached.returncode != 0:
            raise ValueError('could not mount installer')
        try:
            staged = os.path.join(mount, 'AudioConverter.app')
            if not os.path.isdir(staged):
                raise ValueError('installer looks wrong')
            needed = sum(os.path.getsize(os.path.join(r, f))
                         for r, _d, fs in os.walk(staged) for f in fs)
            if shutil.disk_usage('/Applications').free < needed * 2:
                raise ValueError('not enough free disk space')
            trash = bundle + '.old'
            if os.path.exists(trash):
                shutil.rmtree(trash, ignore_errors=True)
            os.rename(bundle, trash)
            try:
                shutil.copytree(staged, bundle, symlinks=True)
            except Exception:
                if os.path.exists(trash):
                    os.rename(trash, bundle)
                raise
            shutil.rmtree(trash, ignore_errors=True)
        finally:
            subprocess.run(['hdiutil', 'detach', mount, '-force'],
                           capture_output=True, timeout=60)
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({'ok': False, 'message': f'Install failed: {str(e)}'}), 500
    shutil.rmtree(workdir, ignore_errors=True)

    def _relaunch():
        try:
            subprocess.Popen(['open', os.path.join(
                bundle, 'Contents', 'MacOS', 'AudioConverter')])
        except Exception:
            pass
        os._exit(0)

    threading.Timer(2.0, _relaunch).start()
    return jsonify({'ok': True,
                    'message': 'Installed. Restarting into the new version…'})


def _windows_update_install():
    """Download the Setup wizard and open it; elevation is its own business.

    Unlike macOS the running app is left alone: the wizard installs over it
    and the user relaunches when ready.
    """
    from app.routes.settings import _default_update_feed
    try:
        resp = requests.get(_default_update_feed(), timeout=20,
                            headers={'Accept': 'application/json'})
        release = resp.json() if resp.status_code == 200 else {}
    except Exception:
        release = {}
    name, url = _find_asset_by_suffix(release, ('.exe',))
    if not url:
        return jsonify({'ok': False,
                        'message': 'No Windows installer found in the latest release.'}), 502
    try:
        downloads = os.path.join(os.path.expanduser('~'), 'Downloads')
        os.makedirs(downloads, exist_ok=True)
        dest = os.path.join(downloads, name)
        digest = _stream_download(url, dest, timeout=600)
        _verify_installer_bytes(release, name, digest)
        subprocess.Popen([dest])
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Install failed: {str(e)}'}), 500
    return jsonify({'ok': True,
                    'message': 'Installer downloaded and opened — follow the wizard, then relaunch.'})


# Audio extensions the library understands, mapped to display labels for
# adopted files (which were never converted through a format choice).
_ADOPT_FORMATS = {
    '.flac': 'FLAC', '.m4a': 'ALAC', '.alac': 'ALAC', '.wav': 'WAV',
    '.ogg': 'OGG Vorbis', '.opus': 'OGG Vorbis', '.mp3': 'MP3',
    '.mp4': 'MP4 Video', '.webm': 'WebM Video', '.mkv': 'MKV Video',
    '.mov': 'MOV Video', '.avi': 'AVI Video',
}
_ADOPT_SCAN_CAP = 5000
_ADOPT_ADD_CAP = 300


def _adopt_row(path, existing):
    """Build (not yet added) a library row for a local audio/video file.

    Returns the row, or None when already adopted. Duration/tags fill in
    via the background backfill so adopting stays fast.
    """
    real = os.path.realpath(path)
    if real in existing:
        return None
    ext = os.path.splitext(real)[1].lower()
    if ext not in _ADOPT_FORMATS:
        return None
    return ConversionHistory(
        url='local:' + real, format=_ADOPT_FORMATS[ext],
        output_path=real, status='completed', progress=100)


@bp.route('/api/adopt', methods=['POST'])
def api_adopt():
    """Adopt a folder of existing audio/video files into the library.

    Scans recursively (hidden folders skipped), creates finished rows for
    anything not already tracked. Facts backfill in the background.
    """
    payload = request.get_json(silent=True) or {}
    folder = (payload.get('path') or '').strip()
    if not folder or not os.path.isdir(folder):
        return jsonify({'ok': False,
                        'message': 'Choose an existing folder'}), 400
    existing = {row[0] for row in
                db.session.query(ConversionHistory.output_path).all()
                if row[0]}
    scanned = 0
    added = 0
    skipped = 0
    truncated = False
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for name in sorted(files):
            if name.startswith('.'):
                continue
            scanned += 1
            if scanned > _ADOPT_SCAN_CAP:
                truncated = True
                break
            if os.path.splitext(name)[1].lower() not in _ADOPT_FORMATS:
                continue
            if added >= _ADOPT_ADD_CAP:
                truncated = True
                break
            row = _adopt_row(os.path.join(root, name), existing)
            if row is None:
                skipped += 1
                continue
            db.session.add(row)
            existing.add(os.path.realpath(os.path.join(root, name)))
            added += 1
        if truncated:
            break
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False,
                        'message': 'Could not save the adopted files'}), 500
    return jsonify({'ok': True, 'added': added, 'skipped': skipped,
                    'scanned': scanned, 'truncated': truncated})


@bp.route('/api/upload', methods=['POST'])
def api_upload():
    """Import dropped audio files: save into the output folder + adopt.

    Browsers don't reveal local paths on file drop, so the bytes come up
    multipart and land as new library rows. Audio/video extensions only.
    """
    files = request.files.getlist('files')
    if not files:
        return jsonify({'ok': False, 'message': 'No files received'}), 400
    try:
        dest_dir = effective_output_path()
        os.makedirs(dest_dir, exist_ok=True)
    except OSError:
        return jsonify({'ok': False,
                        'message': 'Output folder is not writable'}), 400
    existing = {row[0] for row in
                db.session.query(ConversionHistory.output_path).all()
                if row[0]}
    added, skipped = 0, []
    for upload in files[:50]:
        name = sanitize_filename(
            os.path.basename(upload.filename or ''))[:200]
        ext = os.path.splitext(name)[1].lower()
        if not name or ext not in _ADOPT_FORMATS:
            skipped.append(os.path.basename(upload.filename or ''))
            continue
        dest = os.path.join(dest_dir, name)
        stem, suffix = os.path.splitext(dest)
        counter = 2
        while os.path.exists(dest):
            dest = f'{stem} ({counter}){suffix}'
            counter += 1
        try:
            upload.save(dest)
        except Exception:
            skipped.append(name)
            continue
        row = _adopt_row(dest, existing)
        if row is None:
            skipped.append(name)
            continue
        db.session.add(row)
        existing.add(os.path.realpath(dest))
        added += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False,
                        'message': 'Could not save the uploads'}), 500
    return jsonify({'ok': True, 'added': added, 'skipped': skipped})


@bp.route('/api/tidy', methods=['POST'])
def api_tidy():
    """Move library files into Artist/Album folders from their tags.

    Rows without usable tags are reported, not touched. Sidecar covers
    move with their track. Collisions keep the existing file in place.
    """
    try:
        base = effective_output_path()
        os.makedirs(base, exist_ok=True)
    except OSError:
        return jsonify({'ok': False,
                        'message': 'Output folder is not writable'}), 400
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).limit(500).all()
    moved, skipped, already = 0, 0, 0
    for row in rows:
        if row.is_playlist:
            continue
        path = row.output_path or ''
        if not path or not os.path.isfile(path):
            continue
        _title, artist, album = _stored_tags(row)
        artist = (artist or '').strip()
        album = (album or '').strip()
        if not artist or not album:
            skipped += 1
            continue
        target_dir = os.path.join(base, sanitize_filename(artist)[:100],
                                  sanitize_filename(album)[:100])
        target = os.path.join(target_dir, os.path.basename(path))
        if os.path.abspath(target) == os.path.abspath(path):
            already += 1
            continue
        if os.path.exists(target):
            skipped += 1
            continue
        try:
            os.makedirs(target_dir, exist_ok=True)
            try:
                os.replace(path, target)
            except OSError:
                shutil.move(path, target)
            sidecar = os.path.splitext(path)[0] + '.cover.jpg'
            if os.path.isfile(sidecar):
                try:
                    os.replace(sidecar,
                               os.path.splitext(target)[0] + '.cover.jpg')
                except OSError:
                    pass
            row.output_path = target
            _invalidate_stat(path)
            _meta_cache.pop(path, None)
            db.session.commit()
            moved += 1
        except Exception:
            db.session.rollback()
            skipped += 1
    return jsonify({'ok': True, 'moved': moved, 'skipped': skipped,
                    'already': already})


def _has_embedded_art(path):
    """True when ffmpeg can extract a picture stream from the file."""
    fd, tmp = tempfile.mkstemp(suffix='.jpg')
    os.close(fd)
    try:
        res = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-i', path, '-an',
             '-vcodec', 'copy', tmp],
            capture_output=True, text=True, timeout=30)
        return (res.returncode == 0 and os.path.isfile(tmp)
                and os.path.getsize(tmp) > 0)
    except Exception:
        return False
    finally:
        _cleanup(tmp)


@bp.route('/api/covers/backfill', methods=['POST'])
def api_backfill_covers():
    """Fetch missing cover art from each track's stored thumbnail URL.

    Videos already get a poster frame from the cover endpoint, so only
    audio rows are considered. Sidecars are written next to the track.
    """
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).limit(50).all()
    filled, has_art, missing_url, failed = 0, 0, 0, 0
    for row in rows:
        if row.is_playlist:
            continue
        path = row.output_path or ''
        if not path or not os.path.isfile(path):
            continue
        if os.path.splitext(path)[1].lower() in (
                '.mp4', '.webm', '.mkv', '.mov', '.avi'):
            has_art += 1
            continue
        if os.path.isfile(os.path.splitext(path)[0] + '.cover.jpg'):
            has_art += 1
            continue
        if _has_embedded_art(path):
            has_art += 1
            continue
        url = getattr(row, 'cover_url', '') or ''
        if not url:
            missing_url += 1
            continue
        try:
            fetched = _fetch_thumbnail(url)
            if not fetched:
                failed += 1
                continue
            shutil.copyfile(fetched, os.path.splitext(path)[0] + '.cover.jpg')
            _cleanup(fetched)
            filled += 1
        except Exception:
            failed += 1
    return jsonify({'ok': True, 'filled': filled, 'has_art': has_art,
                    'missing_url': missing_url, 'failed': failed})


@bp.route('/api/transcode', methods=['POST'])
def api_transcode():
    """Convert finished tracks to another audio format without re-downloading.

    Creates a sibling file (same folder, new extension) plus a new library
    row; the original is untouched. Tags carry over, covers re-attach for
    formats that hold them (or copy as sidecars otherwise).
    """
    payload = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in (payload.get('ids') or [])][:50]
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'message': 'Invalid track list'}), 400
    fmt_key = str(payload.get('format') or '').strip()
    if not ids:
        return jsonify({'ok': False, 'message': 'No tracks selected'}), 400
    if not is_valid_format(fmt_key):
        return jsonify({'ok': False, 'message': 'Invalid format'}), 400
    if _download_kind_for(fmt_key) == 'video':
        return jsonify({'ok': False,
                        'message': 'Transcoding targets audio formats'}), 400
    fmt = VALID_FORMATS[fmt_key]
    converted, failed = 0, []
    for track_id in ids:
        history = db.session.get(ConversionHistory, track_id)
        src = (history.output_path or '') if history else ''
        if (not history or history.status not in ('completed', 'skipped')
                or not src or not os.path.isfile(src)):
            failed.append(track_id)
            continue
        directory = os.path.dirname(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        dest = os.path.join(directory, f'{stem}.{fmt["ext"]}')
        counter = 2
        base_stem = stem
        while os.path.exists(dest):
            dest = os.path.join(directory, f'{base_stem} ({counter}).{fmt["ext"]}')
            counter += 1
        title, artist, album = _stored_tags(history)
        meta = {'title': title, 'artist': artist, 'album': album}
        cover = None
        sidecar_src = os.path.splitext(src)[0] + '.cover.jpg'
        if fmt_key in COVER_FORMATS and os.path.isfile(sidecar_src):
            cover = sidecar_src
        try:
            result = convert_audio_file(src, fmt_key, dest, meta, cover)
        except Exception as e:
            result = {'success': False, 'error': str(e)}
        if not result.get('success'):
            _cleanup(dest)
            failed.append(track_id)
            continue
        if fmt_key not in COVER_FORMATS and os.path.isfile(sidecar_src):
            try:
                shutil.copyfile(sidecar_src,
                                os.path.splitext(dest)[0] + '.cover.jpg')
            except OSError:
                pass
        row = ConversionHistory(
            url=history.url, format=fmt['label'], output_path=dest,
            status='completed', progress=100,
            cover_url=getattr(history, 'cover_url', '') or '')
        db.session.add(row)
        db.session.commit()
        _store_file_facts(row, dest)
        _invalidate_stat(dest)
        converted += 1
    return jsonify({'ok': True, 'converted': converted, 'failed': failed})


def _download_kind_for(fmt_key):
    """'video' for video target formats, else 'audio'."""
    return VALID_FORMATS.get(fmt_key, {}).get('kind', 'audio')


def _trash_path(path):
    """Move a user file to the OS trash; fall back to deleting it.

    Returns (gone_from_place, used_trash). Temp files keep using plain
    removal via _cleanup — the trash is only for deliberate deletes.
    """
    try:
        from send2trash import send2trash
        send2trash(path)
        return True, True
    except Exception:
        pass
    try:
        os.remove(path)
        return True, False
    except OSError:
        return False, False


def is_valid_format(format_type):
    return format_type.lower() in VALID_FORMATS


def _serialize(item):
    saved_path = item.output_path if item.status in ('completed', 'skipped') else ''
    try:
        missed = len(json.loads(item.import_misses or '[]'))
    except (ValueError, TypeError):
        missed = 0
    file_exists = False
    if saved_path:
        file_exists, _size = _cached_stat(saved_path)
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
        'file_exists': file_exists,
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
        'liked': bool(getattr(item, 'liked', False)),
        'play_count': getattr(item, 'play_count', 0) or 0,
        'rating': getattr(item, 'rating', 0) or 0,
        'quality': getattr(item, 'quality', '') or '',
        'duration': getattr(item, 'duration', 0) or 0,
        'tag_title': getattr(item, 'tag_title', '') or '',
        'tag_artist': getattr(item, 'tag_artist', '') or '',
        'tag_album': getattr(item, 'tag_album', '') or '',
        'error': item.error or '',
        'created_at': str(item.created_at) if item.created_at else '',
    }


@bp.route('/api/reveal/<int:conversion_id>')
@_same_origin_required
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
@_same_origin_required
def api_queue_pause():
    """Pause the download queue; workers idle until resumed."""
    _queue_paused.set()
    return jsonify({'ok': True, 'paused': True})


@bp.route('/api/queue/resume')
@_same_origin_required
def api_queue_resume():
    """Resume a paused download queue."""
    _queue_paused.clear()
    return jsonify({'ok': True, 'paused': False})


@bp.route('/api/queue/status')
def api_queue_status():
    """Queue state for the toolbar: paused flag plus waiting jobs."""
    return jsonify({'ok': True, 'paused': _queue_paused.is_set(),
                    'pending': _conversion_queue.qsize()})


@bp.route('/api/health')
def api_health():
    """Client-visible warnings: downloader health and output writability."""
    output_ok = False
    disk_free = None
    try:
        target = effective_output_path()
        os.makedirs(target, exist_ok=True)
        output_ok = os.access(target, os.W_OK)
        disk_free = shutil.disk_usage(target).free
    except OSError:
        pass
    return jsonify({'ok': True,
                    'stale_helper_suspected': _stale_helper_event.is_set(),
                    'ffmpeg': check_ffmpeg(),
                    'ytdlp': bool(_find_ytdlp()),
                    'output_writable': output_ok,
                    'disk_free_bytes': disk_free})


@bp.route('/api/prune-missing', methods=['POST'])
def api_prune_missing():
    """Drop history rows whose files are gone from disk.

    Only finished rows are considered; active downloads are never touched.
    Playlist parents are removed once none of their tracks remain.
    """
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).all()
    removed = 0
    for row in rows:
        if row.is_playlist:
            continue
        path = row.output_path or ''
        if path and not os.path.isfile(path):
            db.session.delete(row)
            removed += 1
    db.session.commit()
    orphaned = 0
    for parent in db.session.query(ConversionHistory).filter_by(
            is_playlist=True).all():
        remaining = db.session.query(ConversionHistory).filter_by(
            parent_id=parent.id).count()
        if remaining == 0:
            db.session.delete(parent)
            orphaned += 1
    db.session.commit()
    _invalidate_storage()
    return jsonify({'ok': True, 'removed': removed + orphaned})


@bp.route('/api/skip/<int:conversion_id>')
@_same_origin_required
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


@bp.route('/api/pause/<int:conversion_id>')
@_same_origin_required
def pause_job(conversion_id):
    """Pause an active download, keeping its partial file for resume.

    The running yt-dlp process is terminated; the worker notices the
    paused status and stands down without failing or retrying the job.
    Resuming continues the same partial file (--continue).
    """
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404
    if history.status not in ('downloading', 'converting', 'pending'):
        return jsonify({'ok': False,
                        'message': 'Only active conversions can be paused'}), 400
    with _paused_lock:
        _paused_jobs.add(history.id)
        proc = _active_downloads.get(history.id)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    history.status = 'paused'
    db.session.commit()
    if history.parent_id:
        _refresh_playlist_parent(history.parent_id)
    return jsonify({'ok': True, 'paused': 1})


@bp.route('/api/resume/<int:conversion_id>')
@_same_origin_required
def resume_job(conversion_id):
    """Resume a paused download from its partial file."""
    history = db.session.get(ConversionHistory, conversion_id)
    if not history:
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404
    if history.status != 'paused':
        return jsonify({'ok': False,
                        'message': 'Only paused conversions can be resumed'}), 400
    with _paused_lock:
        _paused_jobs.discard(history.id)
    history.status = 'pending'
    db.session.commit()
    if not _queue_has(history.id):
        _conversion_queue.put(history.id)
    _ensure_worker()
    if history.parent_id:
        _refresh_playlist_parent(history.parent_id)
    return jsonify({'ok': True, 'resumed': 1})


@bp.route('/api/diagnostics')
def api_diagnostics():
    """Downloadable support bundle. Contains no secrets: tokens, client
    secrets, and proxy credentials are never included (only whether a
    proxy is configured at all)."""
    from flask import Response
    from app import APP_VERSION
    settings = UserSettings.query.first()
    rows = db.session.query(ConversionHistory).all()
    by_status = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    disk = {}
    try:
        usage = shutil.disk_usage(effective_output_path())
        disk = {'free_bytes': usage.free, 'total_bytes': usage.total}
    except OSError:
        pass
    with _pool_lock:
        workers = _active_workers
    bundle = {
        'ok': True,
        'app_version': APP_VERSION,
        'platform': sys.platform,
        'python': sys.version.split()[0],
        'ffmpeg': _ffmpeg_version(),
        'ytdlp': _ytdlp_version(),
        'conversions_total': len(rows),
        'by_status': by_status,
        'disk': disk,
        'queue': {'paused': _queue_paused.is_set(),
                  'pending': _conversion_queue.qsize(),
                  'workers_desired': _desired_workers,
                  'workers_active': workers},
        'settings': {
            'output_path': getattr(settings, 'output_path', ''),
            'format_defaults': {
                'wav_sample_rate': getattr(settings, 'wav_sample_rate', ''),
                'wav_bit_depth': getattr(settings, 'wav_bit_depth', ''),
                'ogg_quality': getattr(settings, 'ogg_quality', ''),
                'flac_compression': getattr(settings, 'flac_compression', ''),
            },
            'skip_existing': bool(getattr(settings, 'skip_existing', True)),
            'privacy_mode': bool(getattr(settings, 'privacy_mode', True)),
            'retry_count': getattr(settings, 'retry_count', 3),
            'job_timeout': getattr(settings, 'job_timeout', 300),
            'bandwidth_limit': getattr(settings, 'bandwidth_limit', 0),
            'worker_count': getattr(settings, 'worker_count', 3),
            'proxy_configured': bool(getattr(settings, 'proxy', '')),
            'tidal_connected': bool(getattr(settings, 'tidal_access_token', '')),
            'desktop_notifications': bool(getattr(settings, 'desktop_notifications', True)),
            'close_behavior': getattr(settings, 'close_behavior', 'ask'),
            'tray_icon': bool(getattr(settings, 'tray_icon', True)),
        },
        'recent_log': _recent_log_tail(),
    }
    return Response(json.dumps(bundle, indent=2), mimetype='application/json',
                    headers={'Content-Disposition':
                             'attachment; filename="audio-converter-diagnostics.json"'})


def _recent_log_tail(limit=40):
    """Last log lines for the support bundle, with embedded credentials cut.

    The desktop launcher rotates <data_dir>/logs/app.log; without it this
    is just []. Never includes tokens: userinfo in URLs is masked.
    """
    try:
        db_path = db.engine.url.database
    except Exception:
        return []
    if not db_path or db_path == ':memory:':
        return []
    log_path = os.path.join(os.path.dirname(db_path), 'logs', 'app.log')
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    return [re.sub(r'(://)[^/@\s]+@', r'\1***@', line.rstrip('\n'))
            for line in lines]


@bp.route('/api/backup')
def backup_database():
    """Download a timestamped copy of the library database."""
    from flask import after_this_request
    import sqlite3
    try:
        db_path = db.engine.url.database
    except Exception:
        db_path = None
    if not db_path or db_path == ':memory:' or not os.path.isfile(db_path):
        return jsonify({'ok': False, 'message': 'No local database found'}), 404
    fd, tmp_path = tempfile.mkstemp(prefix='audio-converter-backup-',
                                    suffix='.db')
    os.close(fd)
    try:
        src = sqlite3.connect(db_path, timeout=15)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except Exception as e:
        _cleanup(tmp_path)
        return jsonify({'ok': False, 'message': f'Backup failed: {str(e)}'}), 500

    @after_this_request
    def _remove_temp(response):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return response

    stamp = time.strftime('%Y%m%d-%H%M%S')
    return send_file(tmp_path, as_attachment=True,
                     download_name=f'audio-converter-backup-{stamp}.db')


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
    trashed_files = 0
    kept_files = 0
    doomed = {c.id for c in children} | {parent.id}
    folder = parent.output_path or ''
    for child in children:
        path = child.output_path or ''
        if path and os.path.isfile(path):
            shared = db.session.query(ConversionHistory).filter(
                ConversionHistory.output_path == path,
                ~ConversionHistory.id.in_(doomed)).first() is not None
            if shared:
                kept_files += 1
            else:
                gone, trashed = _trash_path(path)
                if gone:
                    removed_files += 1
                    trashed_files += 1 if trashed else 0
                    _invalidate_stat(path)
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
                    'removed_files': removed_files,
                    'trashed_files': trashed_files,
                    'kept_files': kept_files})


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
    trashed = False
    shared = False
    path = history.output_path or ''
    if path and os.path.isfile(path):
        shared = db.session.query(ConversionHistory).filter(
            ConversionHistory.id != history.id,
            ConversionHistory.output_path == path).first() is not None
        if not shared:
            removed, trashed = _trash_path(path)
            if removed:
                _invalidate_stat(path)
            else:
                return jsonify({'ok': False,
                                'message': 'Could not delete file'}), 500

    parent_id = history.parent_id
    db.session.delete(history)
    db.session.commit()
    if parent_id:
        _refresh_playlist_parent(parent_id)
    if shared:
        return jsonify({'ok': True, 'removed_file': False,
                        'trashed': False,
                        'message': 'History entry removed. File kept — '
                                   'another entry still points at it.'})
    if removed and trashed:
        return jsonify({'ok': True, 'removed_file': True, 'trashed': True,
                        'message': 'Moved to Trash.'})
    return jsonify({'ok': True, 'removed_file': removed, 'trashed': False})


def _readable_bytes(num):
    """Human-readable byte count for the storage readout."""
    value = float(num or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024


@bp.route('/api/storage')
def api_storage():
    """Total disk space used by converted files still on disk.

    Recomputed at most once a minute; file deletions, renames, and finished
    downloads invalidate the cached total immediately.
    """
    now = time.monotonic()
    if now - _storage_cache['time'] < 60.0 and _storage_cache['time'] > 0:
        total = _storage_cache['total']
        count = _storage_cache['files']
        return jsonify({'ok': True, 'bytes': total, 'files': count,
                        'readable': _readable_bytes(total)})
    total = 0
    count = 0
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).all()
    for row in rows:
        path = row.output_path or ''
        if not path:
            continue
        exists, size = _cached_stat(path, ttl=60.0)
        if exists:
            total += size
            count += 1
    _storage_cache.update({'time': now, 'total': total, 'files': count})
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


def _serialize_subscription(sub):
    return {
        'id': sub.id,
        'url': sub.url,
        'playlist_title': sub.playlist_title or '',
        'parent_id': sub.parent_id,
        'interval_hours': sub.interval_hours,
        'active': bool(sub.active),
        'last_checked': str(sub.last_checked) if sub.last_checked else '',
    }


@bp.route('/api/subscribe/<int:parent_id>', methods=['POST'])
def subscribe_playlist(parent_id):
    """Follow a playlist: re-check it on a schedule for new tracks."""
    from app.models import Subscription
    parent = db.session.get(ConversionHistory, parent_id)
    if not parent or not parent.is_playlist:
        return jsonify({'ok': False, 'message': 'Playlist not found'}), 404

    payload = request.get_json(silent=True) or {}
    try:
        interval = int(payload.get('interval_hours', 24))
    except (TypeError, ValueError):
        interval = 24
    if interval not in SUBSCRIPTION_INTERVALS:
        interval = 24

    existing = db.session.query(Subscription).filter_by(
        parent_id=parent.id).first()
    if existing:
        existing.active = True
        existing.interval_hours = interval
        db.session.commit()
        return jsonify({'ok': True, 'id': existing.id,
                        'message': 'Already following this playlist.'})

    sub = Subscription(
        url=parent.url,
        format=parent.format,
        output_path=parent.output_path,
        organization=parent.playlist_organization or 'folder',
        playlist_title=parent.playlist_title,
        parent_id=parent.id,
        interval_hours=interval,
    )
    db.session.add(sub)
    db.session.commit()
    parent.subscription_id = sub.id
    db.session.commit()
    _ensure_scheduler()
    return jsonify({'ok': True, 'id': sub.id,
                    'message': 'Following playlist for new tracks.'})


@bp.route('/api/subscriptions')
def api_subscriptions():
    """List followed playlists."""
    from app.models import Subscription
    subs = db.session.query(Subscription).order_by(
        Subscription.created_at.desc()).all()
    return jsonify({'ok': True,
                    'items': [_serialize_subscription(s) for s in subs]})


@bp.route('/api/subscriptions/<int:sub_id>/check', methods=['POST'])
def api_subscription_check(sub_id):
    """Run a subscription check right now instead of waiting for schedule."""
    result = check_subscription(sub_id)
    if not result.get('ok'):
        return jsonify({**result, 'added': 0}), 400
    return jsonify(result)


@bp.route('/api/subscriptions/<int:sub_id>/toggle', methods=['POST'])
def api_subscription_toggle(sub_id):
    """Pause or resume a subscription without deleting it."""
    from app.models import Subscription
    sub = db.session.get(Subscription, sub_id)
    if not sub:
        return jsonify({'ok': False, 'message': 'Subscription not found'}), 404
    sub.active = not sub.active
    db.session.commit()
    return jsonify({'ok': True, 'active': bool(sub.active)})


@bp.route('/api/subscriptions/<int:sub_id>/interval', methods=['POST'])
def api_subscription_interval(sub_id):
    """Change how often a subscription is re-checked."""
    from app.models import Subscription
    sub = db.session.get(Subscription, sub_id)
    if not sub:
        return jsonify({'ok': False, 'message': 'Subscription not found'}), 404
    payload = request.get_json(silent=True) or {}
    try:
        interval = int(payload.get('interval_hours', 24))
    except (TypeError, ValueError):
        interval = 24
    sub.interval_hours = interval if interval in SUBSCRIPTION_INTERVALS else 24
    db.session.commit()
    return jsonify({'ok': True, 'interval_hours': sub.interval_hours})


@bp.route('/api/subscriptions/<int:sub_id>/delete', methods=['POST'])
def api_subscription_delete(sub_id):
    """Stop following a playlist (history is kept)."""
    from app.models import Subscription
    sub = db.session.get(Subscription, sub_id)
    if not sub:
        return jsonify({'ok': False, 'message': 'Subscription not found'}), 404
    db.session.delete(sub)
    db.session.commit()
    return jsonify({'ok': True})


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
    """Start a background integrity scan of all downloaded files.

    Decoding a whole library takes minutes, so this returns immediately and
    the scan runs on a daemon thread; poll /api/verify-status for progress.
    Corrupted files are deleted and re-queued for re-download as found.
    """
    with _verify_lock:
        if _verify_state['running']:
            return jsonify({'ok': True, 'started': False,
                            'message': 'A verification scan is already running.',
                            **{k: _verify_state[k] for k in ('checked', 'total')}})
        # Mark running synchronously so status polls never observe a stale
        # idle state between this response and the thread's first update.
        _verify_state.update({'running': True, 'checked': 0, 'total': 0,
                              'repaired': 0, 'errors': 0, 'done': False,
                              'message': ''})
    thread = threading.Thread(target=_verify_in_background, daemon=True,
                              name='verify-worker')
    thread.start()
    return jsonify({'ok': True, 'started': True,
                    'message': 'Verification started in the background.'})


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

    def _resolve_one(track):
        expected_title = (f"{track['artist']} - {track['title']}"
                          if track.get('artist') else track['title'])
        try:
            expected_duration = float(track.get('duration') or 0)
        except (TypeError, ValueError):
            expected_duration = 0.0
        found = search_track_url(expected_title,
                                 expected_duration=expected_duration or None)
        return track, expected_title, expected_duration, found

    # Searches are network-bound and independent: run a handful at once so a
    # 50-track import resolves in seconds, not minutes. map() preserves
    # track order for the folder listing.
    tracks = resolved['tracks']
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(_resolve_one, tracks))
    urls, items, misses = [], [], []
    for track, expected_title, expected_duration, found in outcomes:
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

    Returns (duration_seconds, tags_dict, quality_string). Unparseable
    files yield (0.0, {}, ''). Using ffmpeg instead of ffprobe keeps the
    app down to a single binary dependency (static ffmpeg builds often
    ship without ffprobe).
    """
    try:
        res = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', path],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0, {}, ''
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
    return duration, tags, _quality_line(out)


def _file_chapters(path):
    """Chapter markers (start/end seconds + title) parsed from ffmpeg output.

    Uses plain `ffmpeg -i` (no ffprobe dependency): chapters print as
    `Chapter #0:1: start 60.000000, end 120.000000` with an optional
    indented `title:` line beneath. Returns [] for chapterless files.
    """
    try:
        res = subprocess.run(
            ['ffmpeg', '-hide_banner', '-i', path],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    chapters = []
    current = None
    for line in (res.stderr or '').splitlines():
        match = re.search(
            r'Chapter #\d+:\d+:\s*start\s*([\d.]+),\s*end\s*([\d.]+)', line)
        if match:
            try:
                current = {'start': float(match.group(1)),
                           'end': float(match.group(2)), 'title': ''}
            except ValueError:
                current = None
            if current is not None:
                chapters.append(current)
            continue
        if current is not None:
            title = re.search(r'(?i)^\s*title\s*:\s*(.+?)\s*$', line)
            if title:
                current['title'] = title.group(1)
                current = None
    return [c for c in chapters if c['end'] > c['start'] >= 0]


def _parse_lrc(synced):
    """Parse synced `[mm:ss.xx] line` lyrics into [{t, text}]."""
    lines = []
    for raw in (synced or '').splitlines():
        match = re.match(r'\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$', raw.strip())
        if not match:
            continue
        try:
            t = int(match.group(1)) * 60 + float(match.group(2))
        except ValueError:
            continue
        text = match.group(3).strip()
        if text:
            lines.append({'t': t, 'text': text})
    return lines


_lyrics_cache = {}


def _fetch_lyrics(artist, title, album='', duration=0):
    """Look up plain + synced lyrics from LRCLIB (no key needed).

    Only ever called from the explicit lyrics button, so the third-party
    lookup is always user-initiated. Results are cached in memory.
    """
    key = (artist or '', title or '', album or '')
    if key in _lyrics_cache:
        return _lyrics_cache[key]
    result = {'plain': '', 'synced': []}
    if not (title or '').strip():
        return result
    try:
        params = {'track_name': title, 'artist_name': artist or '',
                  'album_name': album or ''}
        if duration:
            params['duration'] = int(duration)
        resp = requests.get('https://lrclib.net/api/get', params=params,
                            timeout=15,
                            headers={'User-Agent': 'AudioConverter/1.0'})
        if resp.status_code == 200:
            data = resp.json()
            result = {'plain': data.get('plainLyrics') or '',
                      'synced': _parse_lrc(data.get('syncedLyrics') or '')}
    except Exception:
        pass
    _lyrics_cache[key] = result
    if len(_lyrics_cache) > 200:
        for old in list(_lyrics_cache)[:50]:
            _lyrics_cache.pop(old, None)
    return result


@bp.route('/api/lyrics/<int:conversion_id>')
def api_lyrics(conversion_id):
    """Plain + synced lyrics for the lyrics overlay, looked up on demand."""
    history = _playable_file_or_404(conversion_id)
    if not history:
        abort(404)
    title, artist, album = _stored_tags(history)
    if not title:
        title = os.path.splitext(
            os.path.basename(history.output_path or ''))[0]
    data = _fetch_lyrics(artist, title, album,
                         _stored_duration(history))
    return jsonify({'ok': True, 'title': title, 'artist': artist,
                    'plain': data['plain'], 'synced': data['synced']})


@bp.route('/api/rate/<int:conversion_id>', methods=['POST'])
def api_rate(conversion_id):
    """Set a personal 0–5 star rating on a finished track."""
    history = db.session.get(ConversionHistory, conversion_id)
    if not history or history.status not in ('completed', 'skipped'):
        return jsonify({'ok': False, 'message': 'Conversion not found'}), 404
    payload = request.get_json(silent=True) or {}
    try:
        rating = int(payload.get('rating', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'message': 'Invalid rating'}), 400
    history.rating = max(0, min(5, rating))
    db.session.commit()
    return jsonify({'ok': True, 'rating': history.rating})


def _smart_items(key, limit=50):
    """Track rows for a smart mix. Keys: played, recent, unplayed,
    liked, rated."""
    base = ConversionHistory.status.in_(['completed', 'skipped'])
    query = db.session.query(ConversionHistory).filter(base)
    if key == 'played':
        query = query.filter(ConversionHistory.play_count > 0).order_by(
            ConversionHistory.play_count.desc())
    elif key == 'recent':
        query = query.order_by(ConversionHistory.created_at.desc())
    elif key == 'unplayed':
        query = query.filter((ConversionHistory.play_count.is_(None)) |
                              (ConversionHistory.play_count == 0)).order_by(
            ConversionHistory.created_at.desc())
    elif key == 'liked':
        query = query.filter(ConversionHistory.liked.is_(True)).order_by(
            ConversionHistory.created_at.desc())
    elif key == 'rated':
        query = query.filter(ConversionHistory.rating > 0).order_by(
            ConversionHistory.rating.desc(),
            ConversionHistory.play_count.desc())
    else:
        return None
    return query.limit(limit).all()


@bp.route('/api/smart/<key>')
def api_smart(key):
    """A smart mix: auto-built track lists (most played, recent, ...)."""
    items = _smart_items(key)
    if items is None:
        abort(404)
    return jsonify({'ok': True, 'key': key,
                    'items': [_serialize(i) for i in items]})


def _grouped_tracks(column, limit_groups=200):
    """[{name, tracks, seconds, cover_id}] grouped by a tag column."""
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).all()
    groups = {}
    for row in rows:
        if row.is_playlist:
            continue
        name = (getattr(row, column, '') or '').strip() or 'Unknown'
        entry = groups.setdefault(name, {'tracks': 0, 'seconds': 0.0,
                                         'cover_id': row.id,
                                         'last': row.created_at})
        entry['tracks'] += 1
        try:
            entry['seconds'] += float(row.duration or 0)
        except (TypeError, ValueError):
            pass
        if row.created_at and (not entry['last'] or row.created_at > entry['last']):
            entry['cover_id'] = row.id
            entry['last'] = row.created_at
    result = [{'name': name, 'tracks': e['tracks'],
               'seconds': e['seconds'], 'cover_id': e['cover_id']}
              for name, e in groups.items()]
    result.sort(key=lambda e: (-e['tracks'], e['name'].lower()))
    return result[:limit_groups]


@bp.route('/api/albums')
def api_albums():
    """Album browser: name, track count, length, newest track's cover."""
    return jsonify({'ok': True, 'albums': _grouped_tracks('tag_album')})


@bp.route('/api/artists')
def api_artists():
    """Artist browser, same shape as /api/albums."""
    return jsonify({'ok': True, 'artists': _grouped_tracks('tag_artist')})


@bp.route('/api/album/tracks')
def api_album_tracks():
    """Tracks of one album (or artist) for drill-down + play-all."""
    name = (request.args.get('name') or '').strip()
    by = request.args.get('by', 'album')
    column = ConversionHistory.tag_album if by == 'album' else ConversionHistory.tag_artist
    if not name:
        return jsonify({'ok': True, 'items': []})
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped']),
        column == name).order_by(ConversionHistory.created_at.desc()).all()
    return jsonify({'ok': True, 'items': [_serialize(r) for r in rows]})


@bp.route('/api/notices')
def api_notices():
    """In-app notification center: recent notices, newest first."""
    from app.models import Notice
    try:
        items = db.session.query(Notice).order_by(
            Notice.id.desc()).limit(50).all()
    except Exception:
        return jsonify({'ok': True, 'items': [], 'unread': 0})
    return jsonify({'ok': True,
                    'items': [{'id': n.id, 'title': n.title, 'body': n.body,
                               'read': bool(n.read),
                               'created_at': str(n.created_at) if n.created_at else ''}
                              for n in items],
                    'unread': sum(1 for n in items if not n.read)})


@bp.route('/api/notices/read', methods=['POST'])
def api_notices_read():
    """Mark all notices read (or clear them with ?clear=1)."""
    from app.models import Notice
    try:
        if request.args.get('clear'):
            db.session.query(Notice).delete()
        else:
            db.session.query(Notice).filter_by(read=False).update({'read': True})
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({'ok': True})


@bp.route('/api/first-run')
def api_first_run():
    """True until the first conversion exists: drives the welcome wizard."""
    try:
        has_rows = db.session.query(ConversionHistory).first() is not None
    except Exception:
        has_rows = True
    return jsonify({'ok': True, 'first_run': not has_rows})


@bp.route('/api/chapters/<int:conversion_id>')
def api_chapters(conversion_id):
    """Chapter markers for long videos/audiobooks in the theater view."""
    history = _playable_file_or_404(conversion_id)
    if not history:
        abort(404)
    return jsonify({'ok': True, 'chapters': _file_chapters(history.output_path)})


def _quality_line(ffmpeg_stderr):
    """Human source-quality line from `ffmpeg -i` output.

    Parses the first audio stream (`Audio: flac, 44100 Hz, stereo, s16`)
    into e.g. 'FLAC · 44.1 kHz · stereo'. Returns '' when unparseable.
    """
    match = re.search(r'(?im)^\s*Stream #\d+:\d+.*?:\s*Audio:\s*([^,\n]+)'
                      r'(?:,\s*(\d+)\s*Hz)?(?:,\s*([^,\n]+))?', ffmpeg_stderr or '')
    if not match:
        return ''
    codec = (match.group(1) or '').strip().split()[0].upper()
    parts = [codec] if codec else []
    try:
        rate = int(match.group(2) or 0)
        if rate > 0:
            khz = rate / 1000
            parts.append(f'{khz:g} kHz')
    except (TypeError, ValueError):
        pass
    layout = (match.group(3) or '').strip().split('(')[0].strip()
    if layout:
        parts.append(layout)
    return ' · '.join(parts)


def _probe_metadata(path):
    """Extract title, artist, album, duration, and quality from a file."""
    duration, tags, quality = _ffmpeg_file_info(path)
    return {
        'title': tags.get('title', ''),
        'artist': tags.get('artist', ''),
        'album': tags.get('album', ''),
        'duration': duration,
        'quality': quality,
    }


def _is_valid_audio(path):
    """Check if a file is genuinely playable audio, not just named like it.

    Decodes up to the first two minutes with ffmpeg: truncated downloads
    and garbage bytes fail the decode, while header-only checks would pass
    them. (Length beyond that is covered by the duration check.)
    Returns True if the file is valid, False if corrupted or unreadable.
    """
    try:
        # Quick check: file must exist and be non-empty before decoding
        if not path or not os.path.isfile(path):
            return False
        if os.path.getsize(path) == 0:
            return False
        res = subprocess.run(
            ['ffmpeg', '-v', 'error', '-i', path, '-t', '120', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=150,
        )
        return res.returncode == 0
    except Exception:
        return False


def _probe_duration(path):
    """Return a file's audio duration in seconds, or 0 if unreadable."""
    duration, _tags, _quality = _ffmpeg_file_info(path)
    return duration


def _store_file_facts(row, path):
    """Fill a finished row's duration + embedded tags, probing at most once.

    New conversions call this at completion; old rows get filled lazily by
    _stored_tags the first time they are rendered. Either way listings,
    stats, and grouping views never spawn ffmpeg per row.
    """
    try:
        meta = _cached_metadata(path)
    except Exception:
        return 0.0
    try:
        row.duration = float(meta.get('duration') or 0)
        row.tag_title = str(meta.get('title') or '')[:500]
        row.tag_artist = str(meta.get('artist') or '')[:500]
        row.tag_album = str(meta.get('album') or '')[:500]
        row.quality = str(meta.get('quality') or '')[:100]
        db.session.commit()
    except Exception:
        db.session.rollback()
    return float(meta.get('duration') or 0)


def _stored_tags(row):
    """(title, artist, album) for a row, backfilling pre-upgrade rows once."""
    title = getattr(row, 'tag_title', '') or ''
    artist = getattr(row, 'tag_artist', '') or ''
    album = getattr(row, 'tag_album', '') or ''
    if title or artist or album:
        return title, artist, album
    path = getattr(row, 'output_path', '') or ''
    if not path or not os.path.isfile(path):
        return '', '', ''
    _store_file_facts(row, path)
    return (getattr(row, 'tag_title', '') or '',
            getattr(row, 'tag_artist', '') or '',
            getattr(row, 'tag_album', '') or '')


def _stored_duration(row):
    """A track's length, probing once and storing it for old rows."""
    try:
        if row.duration:
            return float(row.duration)
    except (TypeError, ValueError, AttributeError):
        pass
    path = getattr(row, 'output_path', '') or ''
    if not path or not os.path.isfile(path):
        return 0.0
    duration = _probe_duration(path)
    try:
        row.duration = duration
        db.session.commit()
    except Exception:
        db.session.rollback()
    return duration


# Metadata cache, validated by file mtime: tags only change when the file
# does, so repeated player/tag lookups never re-spawn ffmpeg.
_meta_cache = {}


def _cached_metadata(path):
    """Cheap repeat reads of a file's tags; re-probes after any rewrite."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {'title': '', 'artist': '', 'album': '', 'duration': 0}
    entry = _meta_cache.get(path)
    if entry is not None and entry[0] == mtime:
        return entry[1]
    meta = _probe_metadata(path)
    _meta_cache[path] = (mtime, meta)
    if len(_meta_cache) > 500:
        for key in list(_meta_cache)[:100]:
            _meta_cache.pop(key, None)
    return meta


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
        _invalidate_stat(path)
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


# Library verification runs in the background: a full decode pass over a
# big library takes minutes, which must never block an HTTP request.
_verify_state = {'running': False, 'checked': 0, 'total': 0, 'repaired': 0,
                 'errors': 0, 'done': True, 'message': ''}
_verify_lock = threading.Lock()


def _verify_in_background():
    from app import app as flask_app
    try:
        with flask_app.app_context():
            from app.models import ConversionHistory
            jobs = ConversionHistory.query.filter(
                ConversionHistory.status.in_(['completed', 'skipped'])
            ).all()
            with _verify_lock:
                _verify_state.update({'running': True, 'checked': 0,
                                      'total': len(jobs), 'repaired': 0,
                                      'errors': 0, 'done': False, 'message': ''})
            for job in jobs:
                try:
                    ok, msg = verify_job_file(job)
                    key = 'repaired' if ok else 'errors'
                except Exception:
                    key = 'errors'
                with _verify_lock:
                    _verify_state['checked'] += 1
                    _verify_state[key] += 1
    except Exception as exc:
        with _verify_lock:
            _verify_state['message'] = f'Scan failed: {str(exc)}'
    finally:
        with _verify_lock:
            _verify_state.update({'running': False, 'done': True})
            if not _verify_state['message']:
                _verify_state['message'] = 'Checked {checked} file(s), repaired {repaired}.'.format(
                    **_verify_state)


@bp.route('/api/verify-status')
def api_verify_status():
    """Progress of the background integrity scan."""
    with _verify_lock:
        return jsonify({'ok': True, **_verify_state})