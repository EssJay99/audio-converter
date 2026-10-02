import os
import sys
import threading
import time
from datetime import timedelta

from flask import Blueprint, render_template, request, jsonify, abort, Response
from sqlalchemy import or_
from app.models import ConversionHistory, UserSettings, effective_output_path, utcnow
from app.routes.convert import _serialize, sanitize_filename
from app import db, APP_VERSION

bp = Blueprint('home', __name__)


@bp.route('/')
def index():
    history = db.session.query(ConversionHistory).order_by(
        ConversionHistory.created_at.desc()
    ).limit(10).all()

    settings = UserSettings.query.first()
    default_format = getattr(settings, 'default_format', 'flac') or 'flac'
    today = utcnow().date()
    yesterday = today - timedelta(days=1)
    return render_template(
        'index.html',
        history=history,
        today=today,
        yesterday=yesterday,
        default_output_path=effective_output_path(),
        default_format=default_format,
        request_path=request.path,
        show_history=False,
        app_version=APP_VERSION,
    )


@bp.route('/player')
def player():
    return render_template(
        'player.html',
        request_path=request.path,
        app_version=APP_VERSION,
    )


@bp.route('/about')
def about():
    return render_template('about.html', request_path=request.path)


@bp.route('/contact')
def contact():
    return render_template('contact.html', request_path=request.path)


@bp.route('/history')
def history():
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per = min(500, max(10, int(request.args.get('per', 100))))
    except (TypeError, ValueError):
        per = 100
    total = db.session.query(ConversionHistory).count()
    pages = max(1, -(-total // per))
    page = min(page, pages)

    history = db.session.query(ConversionHistory).order_by(
        ConversionHistory.created_at.desc()
    ).offset((page - 1) * per).limit(per).all()

    today = utcnow().date()
    yesterday = today - timedelta(days=1)
    return render_template(
        'index.html',
        history=history,
        today=today,
        yesterday=yesterday,
        default_output_path=effective_output_path(),
        default_format='flac',
        request_path=request.path,
        show_history=True,
        history_page=page,
        history_pages=pages,
        history_total=total,
        app_version=APP_VERSION,
    )


@bp.route('/api/conversions')
def api_conversions():
    limit = request.args.get('limit', 20, type=int)
    if not isinstance(limit, int):
        limit = 20
    limit = min(100, max(1, limit))

    history = db.session.query(ConversionHistory).order_by(
        ConversionHistory.created_at.desc()
    ).limit(limit).all()

    return jsonify([_serialize(item) for item in history])


@bp.route('/api/search')
def api_search():
    """Search conversions by URL, file path, playlist title, or tags.

    Unlike the instant client-side filter (which only sees rendered rows),
    this scans the whole database — including tracks inside playlists that
    were never expanded. Minimum 2 characters, 25 results max.
    """
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'ok': True, 'query': q, 'items': []})
    # Escape LIKE wildcards so a literal % or _ in the query can't widen
    # the match into unrelated rows.
    escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    like = f'%{escaped}%'
    rows = db.session.query(ConversionHistory).filter(
        or_(ConversionHistory.url.ilike(like, escape='\\'),
            ConversionHistory.output_path.ilike(like, escape='\\'),
            ConversionHistory.playlist_title.ilike(like, escape='\\'),
            ConversionHistory.tag_title.ilike(like, escape='\\'),
            ConversionHistory.tag_artist.ilike(like, escape='\\'),
            ConversionHistory.tag_album.ilike(like, escape='\\'))
    ).order_by(ConversionHistory.created_at.desc()).limit(25).all()
    return jsonify({'ok': True, 'query': q,
                    'items': [_serialize(item) for item in rows]})


DURATION_PROBE_CAP = 500


@bp.route('/api/stats')
def api_stats():
    """Library dashboard numbers: counts, size, playtime, top playlists."""
    from app.routes.convert import _stored_duration, _cached_stat

    try:
        output_root = os.path.abspath(effective_output_path())
    except Exception:
        output_root = ''
    rows = db.session.query(ConversionHistory).all()
    by_status, by_format = {}, {}
    by_folder, by_format_bytes = {}, {}
    total_bytes, total_files = 0, 0
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_format[row.format] = by_format.get(row.format, 0) + 1
        path = row.output_path or ''
        if row.status in ('completed', 'skipped') and path:
            exists, size = _cached_stat(path, ttl=60.0)
            if exists:
                total_bytes += size
                total_files += 1
                by_format_bytes[row.format] = by_format_bytes.get(
                    row.format, 0) + size
                folder = os.path.dirname(os.path.abspath(path))
                if output_root and folder.startswith(output_root):
                    rel = os.path.relpath(folder, output_root)
                    top = rel.split(os.sep)[0]
                    label = top if top != '.' else '(output root)'
                else:
                    label = folder or '(unknown)'
                by_folder[label] = by_folder.get(label, 0) + size

    # Playtime: probe durations (cached by file mtime after the first pass),
    # capped so huge libraries stay responsive.
    duration_files = 0
    total_seconds = 0.0
    probed = 0
    for row in rows:
        if probed >= DURATION_PROBE_CAP:
            break
        if row.status not in ('completed', 'skipped'):
            continue
        duration = _stored_duration(row)
        if duration > 0:
            total_seconds += duration
            duration_files += 1
        probed += 1

    parents = db.session.query(ConversionHistory).filter_by(
        is_playlist=True).order_by(
        ConversionHistory.item_count.desc()).limit(5).all()
    week_ago = utcnow() - timedelta(days=7)
    recent = db.session.query(ConversionHistory).filter(
        ConversionHistory.created_at >= week_ago).count()

    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)

    # Listening stats come from stored columns only (no probing): plays,
    # listening time, and top artists/tracks by play count.
    played = [r for r in rows
              if (getattr(r, 'play_count', 0) or 0) > 0]
    total_plays = sum(r.play_count or 0 for r in played)
    listened_seconds = 0.0
    artist_plays: dict = {}
    for r in played:
        try:
            listened_seconds += float(r.duration or 0) * (r.play_count or 0)
        except (TypeError, ValueError):
            pass
        artist = (getattr(r, 'tag_artist', '') or '').strip() or 'Unknown'
        artist_plays[artist] = artist_plays.get(artist, 0) + (r.play_count or 0)
    top_artists = sorted(artist_plays.items(), key=lambda kv: -kv[1])[:5]
    top_tracks = sorted(played, key=lambda r: -(r.play_count or 0))[:5]
    lh, lm = int(listened_seconds // 3600), int((listened_seconds % 3600) // 60)
    return jsonify({
        'ok': True,
        'tracks': by_status.get('completed', 0) + by_status.get('skipped', 0),
        'bytes': total_bytes,
        'files': total_files,
        'playtime': f'{hours}h {minutes}m' if hours else f'{minutes}m',
        'playtime_tracks': duration_files,
        'by_status': by_status,
        'by_format': by_format,
        'recent_7d': recent,
        'top_playlists': [{'title': p.playlist_title or p.url[:40],
                           'tracks': p.item_count,
                           'status': p.status} for p in parents],
        'total_plays': total_plays,
        'listened': f'{lh}h {lm}m' if lh else f'{lm}m',
        'top_artists': [{'artist': a, 'plays': n} for a, n in top_artists],
        'top_tracks': [{'title': (r.tag_title or os.path.basename(r.output_path or '') or r.url[:40]),
                        'plays': r.play_count or 0} for r in top_tracks],
        'top_folders': [{'folder': name, 'bytes': n} for name, n in sorted(
            by_folder.items(), key=lambda kv: -kv[1])[:8]],
        'bytes_by_format': dict(sorted(by_format_bytes.items(),
                                       key=lambda kv: -kv[1])),
    })


@bp.route('/api/duplicates')
def api_duplicates():
    """Find the same song saved in more than one folder.

    Groups by normalized filename stem (ignoring ' (2)'-style suffixes and
    bracketed tags), so 'Song.flac' in two folders is flagged while genuinely
    different tracks are not. DB-only and fast.
    """
    import re as _re
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])).all()
    groups = {}
    for row in rows:
        path = row.output_path or ''
        if not path or not os.path.isfile(path):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        key = _re.sub(r'\s*[\(\[].*', '', stem).strip().lower()
        key = _re.sub(r'\s+', ' ', key)
        if not key:
            continue
        groups.setdefault(key, {'title': stem, 'items': []})
        groups[key]['items'].append(_serialize(row))
    dupes = []
    for key in sorted(groups):
        folders = {os.path.dirname(i['output_path']) for i in groups[key]['items']}
        if len(folders) > 1:
            dupes.append({'key': key, 'title': groups[key]['title'],
                          'items': groups[key]['items']})
        if len(dupes) >= 50:
            break
    return jsonify({'ok': True, 'groups': dupes})


@bp.route('/api/recently-played')
def api_recently_played():
    """Most recently played tracks, newest listen first."""
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.last_played_at.isnot(None)
    ).order_by(ConversionHistory.last_played_at.desc()).limit(10).all()
    items = []
    for row in rows:
        if not row.output_path or not os.path.isfile(row.output_path):
            continue
        items.append(_serialize(row))
    return jsonify({'ok': True, 'items': items})


def _csv_cell(value):
    """Neutralize spreadsheet formula injection in exported text.

    Filenames derive from video titles, so a hostile title like
    `=cmd|'/c calc'!A0` would execute on open in Excel/LibreOffice.
    Prefixing risky leading characters with a quote keeps the text inert.
    """
    text = str(value or '')
    if text[:1] in ('=', '+', '-', '@', '|', '%'):
        return "'" + text
    return text


@bp.route('/api/library.csv')
def api_library_csv():
    """Download the whole library as CSV (no probing: stored values only)."""
    from flask import Response
    import csv
    import io
    rows = db.session.query(ConversionHistory).filter(
        ConversionHistory.status.in_(['completed', 'skipped'])
    ).order_by(ConversionHistory.created_at.desc()).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['filename', 'format', 'duration_s', 'status', 'plays',
                     'liked', 'file', 'created_at'])
    for row in rows:
        writer.writerow([
            _csv_cell(os.path.basename(row.output_path or '')),
            row.format,
            getattr(row, 'duration', 0) or 0,
            row.status,
            getattr(row, 'play_count', 0) or 0,
            bool(getattr(row, 'liked', False)),
            _csv_cell(row.output_path or ''),
            str(row.created_at) if row.created_at else '',
        ])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition':
                             'attachment; filename="library.csv"'})


@bp.route('/api/playlist/<int:parent_id>/m3u')
def api_playlist_m3u(parent_id):
    """Download a playlist as an .m3u file pointing at the converted tracks."""
    parent = db.session.get(ConversionHistory, parent_id)
    if not parent or not parent.is_playlist:
        abort(404)
    children = db.session.query(ConversionHistory).filter_by(
        parent_id=parent_id
    ).order_by(ConversionHistory.item_index.asc()).all()

    lines = ['#EXTM3U']
    for child in children:
        path = child.output_path or ''
        if child.status not in ('completed', 'skipped') or not os.path.isfile(path):
            continue
        from app.routes.convert import _stored_duration, _cached_metadata
        duration = _stored_duration(child)
        meta = _cached_metadata(path)
        title = meta.get('title') or os.path.splitext(os.path.basename(path))[0]
        artist = meta.get('artist') or ''
        # Metadata tags can carry newlines; keep each entry on its own line.
        title = str(title).replace('\r', ' ').replace('\n', ' ')
        artist = str(artist).replace('\r', ' ').replace('\n', ' ')
        lines.append(f'#EXTINF:{int(duration) if duration > 0 else -1},'
                     f'{artist + " - " if artist else ""}{title}')
        lines.append(path)
    if len(lines) == 1:
        abort(404)
    name = sanitize_filename(parent.playlist_title or 'playlist') + '.m3u'
    return Response('\n'.join(lines) + '\n', mimetype='audio/x-mpegurl',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


@bp.route('/api/playlist/<int:parent_id>')
def api_playlist(parent_id):
    """Return the per-track rows that make up a playlist conversion."""
    children = db.session.query(ConversionHistory).filter_by(
        parent_id=parent_id
    ).order_by(ConversionHistory.item_index.asc()).limit(500).all()

    return jsonify({
        'parent_id': parent_id,
        'total': len(children),
        'items': [_serialize(item) for item in children],
    })


def _expand_user_path(path):
    """Expand ~ and environment vars, returning '' if the result is unusable."""
    if not path:
        return ''
    expanded = os.path.expandvars(os.path.expanduser(path.strip()))
    return expanded if os.path.isabs(expanded) else ''


@bp.route('/api/directories')
def api_directories():
    """Return directories for the searchable path dropdown.

    Query params:
      q      the path being typed / navigated (defaults to the user's home)
      depth  how deep to pre-expand (the '>' paths). Default 2, max 3.

    Response shape:
      {
        "directories": ["/path/one", "/path/two"],
        "valid": true,          # is the current q a usable directory?
        "open_dirs": [...]      # dirs at 'depth' to seed the next expansion
      }
    """
    q = request.args.get('q', '')
    depth = request.args.get('depth', 2, type=int)
    if not isinstance(depth, int):
        depth = 2
    depth = max(1, min(depth, 3))

    base = _expand_user_path(q) or os.path.expanduser('~')

    directories = []
    valid = False

    try:
        if os.path.isdir(base):
            valid = True
            entries = sorted(os.listdir(base))
            for name in entries:
                if name.startswith('.'):
                    continue
                child = os.path.join(base, name)
                if os.path.isdir(child) and not os.path.islink(child):
                    directories.append(child)
            if not directories:
                # A bare file/drive root: list other top-level volumes
                parent = os.path.dirname(base)
                if parent and os.path.isdir(parent):
                    for name in os.listdir(parent):
                        child = os.path.join(parent, name)
                        if name.startswith('.'):
                            continue
                        if os.path.isdir(child) and not os.path.islink(child):
                            directories.append(child)
                    if directories:
                        base = parent
    except (PermissionError, OSError):
        pass

    directories.sort()

    # Pre-expand one level below `depth` so the UI can immediately show
    # deeper folders after the user picks the next directory.
    open_dirs = []
    if directories:
        seen = set()
        for d in directories:
            rel_depth = len([p for p in d.split(os.sep) if p])
            if rel_depth >= depth:
                continue
            try:
                for name in sorted(os.listdir(d)):
                    if name.startswith('.'):
                        continue
                    child = os.path.join(d, name)
                    if os.path.isdir(child) and not os.path.islink(child) and child not in seen:
                        seen.add(child)
                        open_dirs.append(child)
            except (PermissionError, OSError):
                continue
        open_dirs.sort()

    return jsonify({
        'base': base,
        'valid': valid,
        'directories': directories,
        'open_dirs': open_dirs,
        'home': os.path.expanduser('~'),
    })


# Directories we never descend into during full-machine search: hidden
# folders, code/vendor trees, OS internals.
SEARCH_STOP_DIRS = {
    '.git', '.svn', '.hg', '__pycache__', 'node_modules', 'venv', '.venv',
    'Library', 'System', 'usr', 'bin', 'sbin', 'private', 'cores', 'tmp',
    'dev', 'proc', 'sys', 'etc', 'var',
}


def get_search_roots():
    """Top-level directories to search for the whole-machine folder search.

    Deliberately limited to user-relevant locations: the current user's home
    dir (where downloads/music/docs live), external drives, and common shared
    mount points. Scanning every physical root in the OS would mean walking
    system dirs and *every* user account, which is slow and rarely useful.
    """
    roots = [os.path.expanduser('~')]
    if sys.platform == 'win32':
        import string
        for letter in string.ascii_uppercase:
            drive = letter + ':\\'
            if os.path.exists(drive):
                roots.append(drive)
    else:
        for candidate in ['/Volumes', '/media', '/mnt', '/Applications',
                          '/Users/Shared']:
            if os.path.isdir(candidate) and candidate not in roots:
                roots.append(candidate)
    return roots


# Directories that made a whole-machine search stall. Discovered at runtime:
# if a search times out with its worker thread still alive, the last directory
# the walk entered is remembered here (with a size cap) so subsequent searches
# skip it instead of hanging again and again.
BLOCKED_DIRS = []
BLOCKED_DIRS_CAP = 50


def _scan_for_matches(query, roots, max_results, time_budget, collect, last_dir):
    """Walk `roots`, streaming scored matches into `collect` as it goes.

    Uses explicit recursion (not os.walk) so we know *exactly* which directory
    the walk was inside when it ran out of time -- that path goes on the
    blocklist so later searches skip it. `collect` is called incrementally so
    results already gathered are never lost to a later stall.
    """
    q = query.strip().lower()
    if len(q) < 2:
        return
    deadline = time.monotonic() + time_budget
    seen = set()

    def walk(dirpath):
        # Record where we are *before* listing children: if this scandir
        # hangs, this is the exact path to skip on future searches.
        last_dir[0] = dirpath
        if time.monotonic() > deadline:
            return False
        try:
            with os.scandir(dirpath) as it:
                entries = list(it)
        except OSError:
            return True  # unreadable dir: just skip it

        subdirs = [
            e for e in entries
            if e.is_dir(follow_symlinks=False)
            and not e.name.startswith('.')
            and e.name not in SEARCH_STOP_DIRS
            and e.path not in BLOCKED_DIRS
            and e.path not in seen
        ]
        for entry in sorted(subdirs, key=lambda e: e.name):
            if time.monotonic() > deadline:
                return False
            seen.add(entry.path)
            low = entry.name.lower()
            if q in low:
                if low == q:
                    score = 0
                elif low.startswith(q):
                    score = 1
                else:
                    score = 2
                if not collect(score, entry.path):
                    return False
            if not walk(entry.path):
                return False
        return True

    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        if root not in seen:
            seen.add(root)
            if not walk(root):
                return


def search_directories(query, roots=None, max_results=200, time_budget=3.0):
    """Find folders whose name matches `query` anywhere under `roots`.

    The walk runs on a daemon thread and streams matches as it finds them, so
    a directory that blocks the filesystem (network volume, cloud-sync folder)
    can never hang the request or lose the results found before it. Dirs that
    stall the walk are remembered and skipped on later searches.
    """
    if len(query.strip()) < 2:
        return []

    roots = roots if roots is not None else get_search_roots()
    found = []
    done = threading.Event()
    last_dir = [None]

    def collect(score, path):
        found.append((score, len(path), path))
        if len(found) >= max_results:
            done.set()
            return False
        return True

    def walker():
        _scan_for_matches(query, roots, max_results, time_budget, collect, last_dir)
        done.set()

    thread = threading.Thread(target=walker, daemon=True)
    thread.start()

    started = time.monotonic()
    while not done.wait(0.05) and time.monotonic() - started < time_budget:
        pass

    if thread.is_alive() and last_dir[0]:
        # The worker is stuck (almost certainly blocked on a directory I/O).
        # Remember exactly where so the next search skips that path.
        stuck = last_dir[0]
        if stuck not in BLOCKED_DIRS:
            BLOCKED_DIRS.append(stuck)
        if len(BLOCKED_DIRS) > BLOCKED_DIRS_CAP:
            del BLOCKED_DIRS[:len(BLOCKED_DIRS) - BLOCKED_DIRS_CAP]

    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return [path for _, _, path in found[:max_results]]


@bp.route('/api/directory-search')
def api_directory_search():
    """Folder search across the whole machine.

    Query params:
      q   search term (minimum 2 characters)

    Response:
      {
        "query": "...",
        "directories": ["/full/path", ...],   // ranked matches
        "elapsed": 0.42,
        "truncated": true                     // hit the result cap
      }
    """
    start = time.monotonic()
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'query': q, 'directories': [], 'elapsed': 0.0,
                        'truncated': False})

    dirs = search_directories(q)
    return jsonify({
        'query': q,
        'directories': dirs,
        'elapsed': round(time.monotonic() - start, 2),
        'truncated': len(dirs) >= 200,
    })