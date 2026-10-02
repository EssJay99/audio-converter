import json
import os
import sys

import pytest

from app.models import db, ConversionHistory
from app.routes import home as home_module
import app.routes.convert as convert_module


def test_home_page_renders(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'Audio Converter' in resp.data
    assert b'Convert Audio' in resp.data


def test_convert_page_renders(client):
    # The standalone Convert page was merged into Home; the URL redirects.
    resp = client.get('/convert')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/')


def test_no_duplicate_helpers():
    """Path defaults and version comparison each have exactly one home."""
    import app.routes.convert as convert_module
    import app.routes.home as home_module
    import app.routes.settings as settings_module
    from app import models
    assert not hasattr(convert_module, 'get_default_output_path')
    assert not hasattr(home_module, 'get_default_output_path')
    assert not hasattr(settings_module, 'get_default_output_path')
    from app import _version_tuple
    assert settings_module._version_tuple is _version_tuple
    assert not hasattr(convert_module, '_version_tuple')
    assert _version_tuple('v1.2.3') == (1, 2, 3)
    assert _version_tuple('nope') == ()
    assert callable(models.effective_output_path)


def test_settings_page_renders_with_defaults(client):
    resp = client.get('/settings')
    assert resp.status_code == 200
    assert b'FLAC Compression' in resp.data
    assert b'WAV Bit Depth' in resp.data


def test_post_convert_queues_job(client, monkeypatch):
    # Prevent the worker thread from touching the network in tests
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/watch?v=abc',
        'format': 'flac',
        'output_path': '/tmp/out',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        job = ConversionHistory.query.order_by(ConversionHistory.created_at.desc()).first()
        assert job is not None
        assert job.status == 'pending'
        assert job.format == 'FLAC'
        assert job.progress == 0


def test_post_convert_invalid_format(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/watch?v=abc',
        'format': 'mp3',
        'output_path': '/tmp/out',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        assert ConversionHistory.query.count() == 0


def test_api_conversions_returns_progress(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='WAV',
                                output_path='/tmp', status='downloading', progress=42)
        db.session.add(job)
        db.session.commit()

    resp = client.get('/api/conversions')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]['status'] == 'downloading'
    assert data[0]['progress'] == 42


def test_convert_status_by_url(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/status-test', format='FLAC',
                                output_path='/tmp', status='pending', progress=0)
        db.session.add(job)
        db.session.commit()

    resp = client.get('/convert/status?url=https://youtu.be/status-test')
    assert resp.status_code == 200
    assert resp.get_json()['status'] == 'pending'


def test_convert_status_not_found(client):
    resp = client.get('/convert/status?url=https://nope.example.com')
    assert resp.status_code == 404


def test_download_completed_file(client, completed_conversion):
    job_id, path = completed_conversion

    resp = client.get(f'/download/{job_id}')
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] and 'attachment' in resp.headers['Content-Disposition']
    assert resp.data == b'fLaC testdata'


def test_download_pending_returns_404(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x.flac', status='pending')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/download/{job_id}')
    assert resp.status_code == 404


def test_download_missing_file_returns_404(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/does-not-exist.flac', status='completed')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/download/{job_id}')
    assert resp.status_code == 404


def test_api_conversions_include_saved_file(client, completed_conversion):
    job_id, path = completed_conversion

    resp = client.get('/api/conversions', query_string={'limit': 10})
    assert resp.status_code == 200
    item = next(i for i in resp.get_json() if i['id'] == job_id)
    assert item['filename'] == os.path.basename(path)
    assert item['output_path'] == path
    assert item['file_exists'] is True

    with client.application.app_context():
        pending = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                    output_path='/tmp/pending-dir', status='pending')
        db.session.add(pending)
        db.session.commit()
        pending_id = pending.id

    resp = client.get('/api/conversions', query_string={'limit': 10})
    item = next(i for i in resp.get_json() if i['id'] == pending_id)
    assert item['filename'] == ''
    assert item['file_exists'] is False


def test_reveal_requires_completed(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x.flac', status='pending')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/api/reveal/{job_id}')
    assert resp.status_code == 404


def test_reveal_missing_file_returns_404(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/does-not-exist.flac', status='completed')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/api/reveal/{job_id}')
    assert resp.status_code == 404


def test_reveal_opens_file_manager(client, completed_conversion, monkeypatch):
    job_id, path = completed_conversion
    calls = []

    monkeypatch.setattr(convert_module.subprocess, 'Popen',
                        lambda cmd, **kw: calls.append(cmd))

    resp = client.get(f'/api/reveal/{job_id}')
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True
    assert calls, 'file manager was never opened'
    if sys.platform == 'darwin':
        assert calls[0][:2] == ['open', '-R']
        assert calls[0][2] == path
    elif sys.platform.startswith('win'):
        assert calls[0][-1] == path
    else:
        # Linux reveals the containing folder.
        assert calls[0] == ['xdg-open', os.path.dirname(path)]


# --------------------------------------------------------------- playlists --

@pytest.mark.parametrize('url,expected', [
    ('https://www.youtube.com/playlist?list=PLabc123', True),
    ('https://music.youtube.com/playlist?list=OLAK5uy_xxx', True),
    ('https://www.youtube.com/watch?v=abc&list=RDxyz', True),
    ('https://soundcloud.com/user/sets/my-dj-mix', True),
    ('https://www.youtube.com/channel/UCabc', True),
    ('https://www.youtube.com/user/someone', True),
    ('https://www.youtube.com/@some-handle', True),
    ('https://www.youtube.com/c/SomeName', True),
    ('https://music.youtube.com/channel/UCxyz', True),
    ('https://soundcloud.com/an-artist-profile', True),
    ('https://m.youtube.com/@short-handle', True),
    ('https://www.youtube.com/watch?v=jNQXAC9IVRw', False),
    ('https://soundcloud.com/artist/single-track', False),
    ('https://youtu.be/jNQXAC9IVRw', False),
    ('https://soundcloud.com/settings', False),
])
def test_is_collection_url(url, expected):
    assert convert_module._is_collection_url(url) is expected


def test_resolve_playlist_extracts_entries(client, monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = json.dumps({'title': 'Sunday Drive', 'entries': [
            {'webpage_url': 'https://www.youtube.com/watch?v=aaa'},
            {'id': 'bbb'},
            None,
        ]})
        stderr = ''

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())

    result = convert_module.resolve_playlist(
        'https://www.youtube.com/playlist?list=PLx')
    assert result['success'] is True
    assert result['title'] == 'Sunday Drive'
    assert result['urls'] == [
        'https://www.youtube.com/watch?v=aaa',
        'https://www.youtube.com/watch?v=bbb',
    ]


def test_resolve_playlist_reports_failure(client, monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ''
        stderr = 'Something went wrong'

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())

    result = convert_module.resolve_playlist(
        'https://www.youtube.com/playlist?list=PLx')
    assert result['success'] is False


def test_post_convert_playlist_queues_parent(client, monkeypatch, tmp_path):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

    out = tmp_path / 'out'
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/playlist?list=PL123',
        'format': 'flac',
        'output_path': str(out),
    })
    assert resp.status_code == 302

    with client.application.app_context():
        job = ConversionHistory.query.order_by(
            ConversionHistory.created_at.desc()).first()
        assert job is not None
        assert job.is_playlist is True
        assert job.parent_id is None
        assert job.status == 'pending'
        assert job.output_path == str(out)


def test_expand_playlist_creates_children(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'resolve_playlist', lambda url: {
        'success': True,
        'title': 'Sunday Drive',
        'urls': ['https://www.youtube.com/watch?v=1',
                 'https://www.youtube.com/watch?v=2'],
    })

    with client.application.app_context():
        parent = ConversionHistory(
            url='https://www.youtube.com/playlist?list=X',
            format='FLAC', output_path=str(tmp_path), status='pending',
            is_playlist=True)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        convert_module._expand_playlist_job(parent)

        parent = db.session.get(ConversionHistory, pid)
        assert parent.playlist_title == 'Sunday Drive'
        assert parent.item_count == 2
        assert parent.status == 'downloading'

        folder = os.path.join(str(tmp_path), 'Sunday Drive')
        assert parent.output_path == folder
        assert os.path.isdir(folder)

        children = ConversionHistory.query.filter_by(
            parent_id=pid).order_by(ConversionHistory.item_index).all()
        assert len(children) == 2
        assert children[0].item_index == 0
        assert children[1].item_index == 1
        assert all(c.output_path == folder for c in children)


def test_refresh_playlist_parent_aggregates(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        c1 = ConversionHistory(url='https://youtu.be/1', format='FLAC',
                               output_path='/tmp/p/1.flac', status='completed',
                               parent_id=pid, item_index=0)
        c2 = ConversionHistory(url='https://youtu.be/2', format='FLAC',
                               output_path='/tmp/p/2.flac', status='pending',
                               parent_id=pid, item_index=1)
        db.session.add_all([c1, c2])
        db.session.commit()

        convert_module._refresh_playlist_parent(pid)
        parent = db.session.get(ConversionHistory, pid)
        assert parent.progress == 50
        assert parent.status == 'downloading'

        c2.status = 'completed'
        db.session.commit()
        convert_module._refresh_playlist_parent(pid)
        parent = db.session.get(ConversionHistory, pid)
        assert parent.status == 'completed'
        assert parent.progress == 100

        c2.status = 'failed'
        db.session.commit()
        convert_module._refresh_playlist_parent(pid)
        parent = db.session.get(ConversionHistory, pid)
        assert parent.status == 'completed'
        assert '1 of 2' in (parent.error or '')

        c1.status = 'failed'
        c2.status = 'failed'
        db.session.commit()
        convert_module._refresh_playlist_parent(pid)
        parent = db.session.get(ConversionHistory, pid)
        assert parent.status == 'failed'


def test_api_playlist_lists_children(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='completed',
                                   is_playlist=True, item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        db.session.add_all([
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path='/tmp/p/2.flac', status='completed',
                              parent_id=pid, item_index=1),
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path='/tmp/p/1.flac', status='completed',
                              parent_id=pid, item_index=0),
        ])
        db.session.commit()

    resp = client.get(f'/api/playlist/{pid}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['total'] == 2
    assert [i['item_index'] for i in data['items']] == [0, 1]
    assert data['items'][0]['is_playlist'] is False
    assert data['items'][0]['parent_id'] == pid


def test_reveal_folder_in_file_manager(client, tmp_path, monkeypatch):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                output_path=str(tmp_path), status='completed',
                                is_playlist=True, item_count=1)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    calls = []
    monkeypatch.setattr(convert_module.subprocess, 'Popen',
                        lambda cmd, **kw: calls.append(cmd))

    resp = client.get(f'/api/reveal/{job_id}')
    assert resp.status_code == 200
    assert calls and calls[0][-1] == str(tmp_path)


def test_settings_save(client):
    resp = client.post('/settings', data={
        'output_path': os.path.expanduser('~/Music/Converter'),
        'wav_sample_rate': '96000',
        'wav_bit_depth': '24',
        'ogg_quality': '9',
        'flac_compression': '8',
    })
    assert resp.status_code == 302

    page = client.get('/settings')
    assert b'96000' in page.data
    assert b'24' in page.data
    assert b'8' in page.data


def test_csrf_protects_post(client):
    app = client.application
    app.config['WTF_CSRF_ENABLED'] = True
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/watch?v=x',
        'format': 'flac',
        'output_path': '/tmp',
    })
    assert resp.status_code == 400


def test_api_directories_root(client, tmp_path):
    sub = tmp_path / 'Music'
    sub.mkdir()
    (tmp_path / 'notes.txt').write_text('x')

    resp = client.get('/api/directories', query_string={'q': str(tmp_path)})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['valid'] is True
    assert data['base'] == str(tmp_path)
    assert str(sub) in data['directories']
    # Files must never appear as selectable directories
    assert all(p.endswith('/notes.txt') is False for p in data['directories'])


def test_api_directories_home_expands_tilde(client):
    resp = client.get('/api/directories', query_string={'q': '~'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['valid'] is True
    assert data['base'] == __import__('os').path.expanduser('~')


def test_api_directories_invalid_path(client):
    resp = client.get('/api/directories', query_string={'q': '/nonexistent-dir-xyz'})
    assert resp.status_code == 200
    assert resp.get_json()['valid'] is False


def test_api_directory_search_finds_subfolder(client, tmp_path, monkeypatch):
    songs = tmp_path / 'HeavyMetalSongs'
    songs.mkdir()
    (songs / 'live').mkdir()
    (tmp_path / 'notes.txt').write_text('ignore me')

    monkeypatch.setattr(home_module, 'get_search_roots', lambda: [str(tmp_path)])

    resp = client.get('/api/directory-search', query_string={'q': 'heavymetal'})
    assert resp.status_code == 200
    dirs = resp.get_json()['directories']
    assert [d for d in dirs if d == str(songs)]


def test_api_directory_search_short_query_empty(client):
    resp = client.get('/api/directory-search', query_string={'q': 'x'})
    assert resp.status_code == 200
    assert resp.get_json()['directories'] == []


def test_api_directory_search_skips_hidden(client, tmp_path, monkeypatch):
    (tmp_path / '.SecretMusic').mkdir()

    monkeypatch.setattr(home_module, 'get_search_roots', lambda: [str(tmp_path)])

    resp = client.get('/api/directory-search', query_string={'q': 'secretmusic'})
    assert resp.get_json()['directories'] == []


def test_search_directories_ranks_starts_with_first(client, tmp_path):
    (tmp_path / 'output').mkdir()
    (tmp_path / 'myoutput').mkdir()
    (tmp_path / 'voice_output').mkdir()

    results = home_module.search_directories('oUt', roots=[str(tmp_path)])
    assert results[0] == os.path.join(str(tmp_path), 'output')


def test_search_directories_respects_max_results(client, tmp_path):
    for i in range(10):
        (tmp_path / ('out' + str(i))).mkdir()

    results = home_module.search_directories('out', roots=[str(tmp_path)], max_results=5)
    assert len(results) == 5


# ------------------------------------------------------ duplicate prevention --

def test_find_duplicate_matches_case_insensitively(tmp_path):
    (tmp_path / 'Song Title.flac').write_bytes(b'flac')
    got = convert_module.find_duplicate(str(tmp_path), 'song title.flac')
    assert got is not None
    assert os.path.basename(got) == 'Song Title.flac'


def test_find_duplicate_no_match(tmp_path):
    (tmp_path / 'Different Song.flac').write_bytes(b'flac')
    assert convert_module.find_duplicate(str(tmp_path), 'Song Title.flac') is None


def test_find_duplicate_ignores_matcher_files(tmp_path):
    (tmp_path / 'Song Title.flac').write_bytes(b'flac')
    assert convert_module.find_duplicate(str(tmp_path), 'Song Title.mp3') is None


def test_download_and_convert_skips_existing_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'extract_metadata',
                        lambda url: {'title': 'Track'})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: True)

    (tmp_path / 'Track.flac').write_bytes(b'flac contents')

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(tmp_path), status='downloading')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'flac', str(tmp_path), job)

    assert result['success'] is False
    assert result['duplicate'] is True
    assert os.path.basename(result['filepath']) == 'Track.flac'


def test_download_and_convert_downloads_when_skip_off(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'extract_metadata',
                        lambda url: {'title': 'Track'})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: False)
    monkeypatch.setattr(convert_module, 'check_ffmpeg', lambda: False)

    (tmp_path / 'Track.flac').write_bytes(b'flac contents')

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(tmp_path), status='downloading')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'flac', str(tmp_path), job)

    # Skip is off, so it must NOT report a duplicate; it proceeds to check ffmpeg.
    assert result.get('duplicate') is not True
    assert result['success'] is False  # ffmpeg is monkeypatched to be "missing"


def test_serialize_skipped_reports_saved_file(client, tmp_path, monkeypatch):
    target = tmp_path / 'Existing.flac'
    target.write_bytes(b'flac')

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(target), status='skipped',
                                progress=100, error='Already exists on disk')
        db.session.add(job)
        db.session.commit()
        item = convert_module._serialize(job)

    assert item['filename'] == 'Existing.flac'
    assert item['file_exists'] is True
    assert item['status'] == 'skipped'


def test_refresh_playlist_parent_counts_skipped_as_finished(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        c1 = ConversionHistory(url='https://youtu.be/1', format='FLAC',
                               output_path='/tmp/p/1.flac', status='completed',
                               parent_id=pid, item_index=0)
        c2 = ConversionHistory(url='https://youtu.be/2', format='FLAC',
                               output_path='/tmp/p/2.flac', status='skipped',
                               parent_id=pid, item_index=1)
        db.session.add_all([c1, c2])
        db.session.commit()

        convert_module._refresh_playlist_parent(pid)
        parent = db.session.get(ConversionHistory, pid)
        assert parent.status == 'completed'
        assert parent.progress == 100
        assert '1 already existed' in (parent.error or '')


# --------------------------------------------------------------- media player

def test_audio_stream_supports_range(client, completed_conversion):
    job_id, path = completed_conversion

    resp = client.get(f'/audio/{job_id}', headers={'Range': 'bytes=0-3'})
    assert resp.status_code == 206
    assert 'Content-Range' in resp.headers
    assert resp.data.startswith(b'fLaC')

    whole = client.get(f'/audio/{job_id}')
    assert whole.status_code == 200
    assert whole.data == b'fLaC testdata'


def test_audio_stream_pending_returns_404(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x.flac', status='pending')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    assert client.get(f'/audio/{job_id}').status_code == 404


def test_api_track_returns_item_and_meta(client, completed_conversion, monkeypatch):
    job_id, path = completed_conversion
    monkeypatch.setattr(convert_module, '_probe_metadata',
                        lambda p: {'title': 'A', 'artist': 'B', 'album': 'C', 'duration': 12.5})

    resp = client.get(f'/api/track/{job_id}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['ok'] is True
    assert data['item']['id'] == job_id
    assert data['meta'] == {'title': 'A', 'artist': 'B', 'album': 'C', 'duration': 12.5}


def test_api_track_bad_file_meta_degrades_to_empty(client, completed_conversion):
    job_id, path = completed_conversion
    resp = client.get(f'/api/track/{job_id}')
    assert resp.status_code == 200
    meta = resp.get_json()['meta']
    assert meta['duration'] == 0
    assert meta['title'] == ''


def test_api_track_missing_file_returns_404(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/does-not-exist.flac', status='completed')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/api/track/{job_id}')
    assert resp.status_code == 404


# ------------------------------------------------- privacy / anti-tracking --

@pytest.mark.parametrize('url,expected', [
    ('https://www.youtube.com/watch?v=abc&si=SHARETOKEN&utm_source=x',
     'https://www.youtube.com/watch?v=abc'),
    ('https://www.youtube.com/watch?v=abc&list=PL1&index=2&feature=shared&pp=ab',
     'https://www.youtube.com/watch?v=abc&list=PL1&index=2'),
    ('https://youtu.be/abc?si=SHARETOKEN&t=42',
     'https://youtu.be/abc?t=42'),
    ('https://soundcloud.com/artist/track?utm_source=clipboard&si=x',
     'https://soundcloud.com/artist/track'),
    ('https://www.youtube.com/watch?v=abc', 'https://www.youtube.com/watch?v=abc'),
    ('', ''),
])
def test_sanitize_url_strips_tracking_params(url, expected):
    assert convert_module.sanitize_url(url) == expected


def test_ytdlp_privacy_flags_present(client, monkeypatch):
    seen = {}

    class FakeResult:
        returncode = 0
        stdout = '{}'
        stderr = ''

    def fake_run(cmd, **kwargs):
        seen['cmd'] = cmd
        return FakeResult()

    monkeypatch.setattr(convert_module.subprocess, 'run', fake_run)
    convert_module._run_ytdlp(['--skip-download', 'https://youtu.be/x'],
                              'https://youtu.be/x', timeout=5)
    cmd = seen['cmd']
    for flag in convert_module._YTDLP_PRIVACY_FLAGS:
        assert flag in cmd


def test_download_audio_command_has_privacy_flags(client, monkeypatch, tmp_path):
    seen = {}

    class FakeStdout:
        def readline(self):
            return ''

    class FakeProc:
        stdout = FakeStdout()
        returncode = 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen['cmd'] = cmd
        return FakeProc()

    monkeypatch.setattr(convert_module.subprocess, 'Popen', fake_popen)
    target = tmp_path / 'out.tmp'
    target.write_bytes(b'data')
    result = convert_module.download_audio(
        'https://youtu.be/x', str(target))
    assert result['success'] is True
    for flag in convert_module._YTDLP_PRIVACY_FLAGS:
        assert flag in seen['cmd']


def test_fetch_thumbnail_uses_cookieless_session(client, monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200
        content = b'img'

    class FakeSession:
        def __init__(self):
            from requests.cookies import RequestsCookieJar
            self.cookies = RequestsCookieJar()

        def get(self, url, **kwargs):
            seen['url'] = url
            seen['headers'] = kwargs.get('headers', {})
            return FakeResp()

    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    path = convert_module._fetch_thumbnail('https://i.ytimg.com/x.jpg')
    assert seen['url'] == 'https://i.ytimg.com/x.jpg'
    assert 'Cookie' not in seen['headers']
    assert 'Referer' not in seen['headers']
    assert path and os.path.isfile(path)
    os.unlink(path)


def test_templates_have_no_external_cdn(client):
    import glob
    templates = glob.glob(os.path.join(
        os.path.dirname(__file__), '..', 'src', 'templates', '*.html'))
    assert templates, 'expected template files to exist'
    for template in templates:
        with open(template) as fh:
            html = fh.read()
        assert 'cdn.jsdelivr.net' not in html, template


def test_post_convert_sanitizes_tracking_params(client, monkeypatch, tmp_path):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

    out = tmp_path / 'out'
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/watch?v=abc&si=SHARETOKEN&utm_source=x',
        'format': 'flac',
        'output_path': str(out),
    })
    assert resp.status_code == 302

    with client.application.app_context():
        job = ConversionHistory.query.order_by(
            ConversionHistory.created_at.desc()).first()
        assert job is not None
        assert job.url == 'https://www.youtube.com/watch?v=abc'
    page = client.get('/settings')
    assert page.status_code == 200
    assert b'id="skip_existing"' in page.data
    assert b'checked' in page.data


def test_settings_save_skip_existing_off(client):
    resp = client.post('/settings', data={
        'output_path': os.path.expanduser('~/Music/Converter'),
        'wav_sample_rate': '48000',
        'wav_bit_depth': '24',
        'ogg_quality': '9',
        'flac_compression': '8',
        # note: skip_existing checkbox omitted -> should save False
    })
    assert resp.status_code == 302

    page = client.get('/settings')
    assert b'id="skip_existing"' in page.data
    assert 'checked' not in str(page.data)[str(page.data).find('id="skip_existing"'):str(page.data).find('id="skip_existing"') + 80]

# ------------------------------------------- streaming playlist import ----

SPOTIFY_FIXTURE_HTML = (
    '<html><head><script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"state":{"data":{"entity":{'
    '"type":"playlist","name":"Gym Hits",'
    '"trackList":['
    '{"uri":"spotify:track:aaa","title":"Song One","subtitle":"Artist A"},'
    '{"uri":"spotify:track:bbb","title":"Song Two","subtitle":"Artist B,\\u00a0Feat C"}'
    ']}}}}}}</script></head></html>'
)

APPLE_FIXTURE_HTML = (
    '<html><head><script id=schema:music-playlist type="application/ld+json">'
    '{"@context":"http://schema.org","@type":"MusicPlaylist","name":"Chill Mix",'
    '"track":[{"@type":"MusicRecording","name":"Calm Waters"},'
    '{"@type":"MusicRecording","name":"Night Drive"}]}'
    '</script></head><body>'
    '<div data="x","artistName":"Ocean Band","y":1></div>'
    '<div data="x","artistName":"Neon Lights","y":2></div>'
    '</body></html>'
)


def _fake_session_factory(html):
    class FakeCookies:
        def clear(self):
            pass

    class FakeResp:
        status_code = 200
        text = html
        content = html.encode('utf-8')
        encoding = 'ISO-8859-1'

    class FakeSession:
        def __init__(self):
            self.cookies = FakeCookies()

        def get(self, url, **kwargs):
            return FakeResp()

    return FakeSession


@pytest.mark.parametrize('url,expected', [
    ('https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M', True),
    ('https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy', True),
    ('https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb', True),
    ('https://music.apple.com/us/album/abbey-road/1441164359', True),
    ('https://tidal.com/browse/playlist/01854b76-1c45-4c94-9743-92ce6d9aff86', True),
    ('https://open.spotify.com/track/6q5F7UUiXK0', False),
    ('https://www.youtube.com/watch?v=jNQXAC9IVRw', False),
    ('https://soundcloud.com/artist/single-track', False),
])
def test_is_streaming_playlist_url(url, expected):
    assert convert_module._is_streaming_playlist_url(url) is expected


@pytest.mark.parametrize('url,expected', [
    ('https://open.spotify.com/playlist/xyz', 'spotify'),
    ('https://music.apple.com/us/playlist/a/pl.b', 'apple'),
    ('https://tidal.com/browse/playlist/01854b76-1c45-4c94-9743-92ce6d9aff86', 'tidal'),
    ('https://www.youtube.com/watch?v=x', None),
])
def test_streaming_service(url, expected):
    assert convert_module._streaming_service(url) == expected


def test_extract_spotify_playlist(monkeypatch):
    monkeypatch.setattr(convert_module.requests, 'Session',
                        _fake_session_factory(SPOTIFY_FIXTURE_HTML))
    result = convert_module._extract_spotify_playlist(
        'https://open.spotify.com/playlist/xyz')
    assert result['success'] is True
    assert result['title'] == 'Gym Hits'
    assert result['tracks'] == [
        {'artist': 'Artist A', 'title': 'Song One', 'duration': 0.0},
        {'artist': 'Artist B, Feat C', 'title': 'Song Two', 'duration': 0.0},
    ]


def test_extract_apple_playlist(monkeypatch):
    monkeypatch.setattr(convert_module.requests, 'Session',
                        _fake_session_factory(APPLE_FIXTURE_HTML))
    result = convert_module._extract_apple_playlist(
        'https://music.apple.com/us/playlist/chill/pl.abc')
    assert result['success'] is True
    assert result['title'] == 'Chill Mix'
    assert result['tracks'] == [
        {'artist': 'Ocean Band', 'title': 'Calm Waters', 'duration': 0.0},
        {'artist': 'Neon Lights', 'title': 'Night Drive', 'duration': 0.0},
    ]


def test_resolve_streaming_tidal_needs_login():
    result = convert_module.resolve_streaming_playlist(
        'https://tidal.com/browse/playlist/01854b76-1c45-4c94-9743-92ce6d9aff86')
    assert result['success'] is False
    assert 'connect Tidal in Settings' in result['error']


def test_search_track_url_uses_first_result(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = json.dumps({'entries': [
            {'webpage_url': 'https://www.youtube.com/watch?v=vid1&si=x',
             'title': 'Artist - Song'},
        ]})
        stderr = ''

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())
    assert convert_module.search_track_url('Artist - Song') == \
        'https://www.youtube.com/watch?v=vid1'


def test_search_track_url_falls_back_to_id(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = json.dumps({'entries': [{'id': 'vid2', 'title': 'x'}]})
        stderr = ''

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())
    assert convert_module.search_track_url('Something') == \
        'https://www.youtube.com/watch?v=vid2'


def test_search_track_url_none_when_empty(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = json.dumps({'entries': []})
        stderr = ''

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())
    assert convert_module.search_track_url('Nothing Matches This') is None


def test_resolve_import_urls_searches_each_track(monkeypatch):
    monkeypatch.setattr(convert_module, 'resolve_streaming_playlist', lambda url, max_tracks=50: {
        'success': True, 'title': 'Gym Hits', 'source': 'spotify',
        'tracks': [{'artist': 'A', 'title': 'One', 'duration': 200},
                   {'artist': 'B', 'title': 'Two', 'duration': 180}],
    })
    seen = []

    def fake_search(query, timeout=60, expected_duration=None):
        seen.append((query, expected_duration))
        return 'https://www.youtube.com/watch?v=' + query.split()[-1].lower() if query != 'B - Two' else None

    monkeypatch.setattr(convert_module, 'search_track_url', fake_search)
    result = convert_module.resolve_import_urls('https://open.spotify.com/playlist/x')
    assert result['success'] is True
    assert result['title'] == 'Gym Hits'
    assert result['source'] == 'spotify'
    assert result['urls'] == ['https://www.youtube.com/watch?v=one']
    assert result['found'] == 1
    assert result['total'] == 2
    assert seen == [('A - One', 200), ('B - Two', 180)]
    assert result['items'] == [{'url': 'https://www.youtube.com/watch?v=one',
                                'expected_title': 'A - One', 'expected_duration': 200}]


def test_post_convert_streaming_queues_parent(client, monkeypatch, tmp_path):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

    out = tmp_path / 'out'
    resp = client.post('/convert', data={
        'url': 'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M',
        'format': 'flac',
        'output_path': str(out),
    })
    assert resp.status_code == 302

    with client.application.app_context():
        job = ConversionHistory.query.order_by(
            ConversionHistory.created_at.desc()).first()
        assert job is not None
        assert job.is_playlist is True
        assert job.url == 'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M'


def test_expand_import_creates_single_folder_children(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'resolve_import_urls', lambda url: {
        'success': True, 'title': 'Gym Hits', 'source': 'spotify',
        'urls': ['https://www.youtube.com/watch?v=1',
                 'https://www.youtube.com/watch?v=2'],
        'found': 2, 'total': 2,
    })

    with client.application.app_context():
        parent = ConversionHistory(
            url='https://open.spotify.com/playlist/X',
            format='FLAC', output_path=str(tmp_path), status='pending',
            is_playlist=True)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        convert_module._expand_playlist_job(parent)

        parent = db.session.get(ConversionHistory, pid)
        assert parent.playlist_title == 'Gym Hits'
        assert parent.import_source == 'spotify'

        folder = os.path.join(str(tmp_path), 'Gym Hits')
        assert parent.output_path == folder
        children = ConversionHistory.query.filter_by(
            parent_id=pid).order_by(ConversionHistory.item_index).all()
        assert len(children) == 2
        assert {c.output_path for c in children} == {folder}


def test_expand_import_failure_marks_parent(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'resolve_import_urls', lambda url: {
        'success': False, 'error': 'Nope'})

    with client.application.app_context():
        parent = ConversionHistory(
            url='https://open.spotify.com/playlist/X',
            format='FLAC', output_path=str(tmp_path), status='pending',
            is_playlist=True)
        db.session.add(parent)
        db.session.commit()

        convert_module._expand_playlist_job(parent)

        assert parent.status == 'failed'
        assert 'Nope' in (parent.error or '')


def test_serialize_includes_import_source(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://open.spotify.com/playlist/X', format='FLAC',
                                output_path='/tmp/p', status='downloading',
                                is_playlist=True, import_source='spotify')
        db.session.add(job)
        db.session.commit()
        assert convert_module._serialize(job)['import_source'] == 'spotify'


def test_fetch_page_prefers_utf8_over_latin1_guess(monkeypatch):
    class FakeCookies:
        def clear(self):
            pass

    class FakeResp:
        status_code = 200
        content = 'Today’s Hits'.encode('utf-8')
        encoding = 'ISO-8859-1'

        @property
        def text(self):
            return self.content.decode('iso-8859-1')

    class FakeSession:
        def __init__(self):
            self.cookies = FakeCookies()

        def get(self, url, **kwargs):
            return FakeResp()

    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    assert convert_module._fetch_page('https://example.com/x') == 'Today’s Hits'


def test_extract_apple_album_single_artist(monkeypatch):
    html = (
        '<html><head><script id=schema:music-album type="application/ld+json">'
        '{"@context":"http://schema.org","@type":"MusicAlbum","name":"Abbey Road"}'
        '</script>'
        '<meta property="og:title" content="Abbey Road by The Beatles">'
        '</head><body>'
        '<div data="x","artistName":"The Beatles","y":0></div>'
        '<div>"title":"Come Together","url":"https://music.apple.com/us/album/x/1?i=11",'
        '"artistName":"The Beatles"</div>'
        '<div>"title":"Something","url":"https://music.apple.com/us/album/x/1?i=22",'
        '"artistName":"The Beatles"</div>'
        '</body></html>'
    )
    monkeypatch.setattr(convert_module.requests, 'Session',
                        _fake_session_factory(html))
    result = convert_module._extract_apple_playlist(
        'https://music.apple.com/us/album/abbey-road/1441164359')
    assert result['success'] is True
    assert result['title'] == 'Abbey Road'
    assert result['tracks'] == [
        {'artist': 'The Beatles', 'title': 'Come Together', 'duration': 0.0},
        {'artist': 'The Beatles', 'title': 'Something', 'duration': 0.0},
    ]


# ----------------------------------------- output verification ----

@pytest.mark.parametrize('value,expected', [
    ('PT3M33S', 213.0),
    ('PT1H2M3S', 3723.0),
    ('PT45S', 45.0),
    ('PT2M', 120.0),
    ('', 0.0),
    ('not-a-duration', 0.0),
])
def test_parse_iso8601_duration(value, expected):
    assert convert_module._parse_iso8601_duration(value) == expected


@pytest.mark.parametrize('expected,actual,match', [
    ('The Beatles - Drive My Car', 'Drive My Car (Remastered 2009)', True),
    ('The Beatles - Drive My Car', 'The Beatles - Drive My Car (Lyric Video)', True),
    ('Morgan Wallen - Been By Now', 'Been By Now', True),
    ('The Beatles - Yesterday', '10 Hours of Rain Sounds', False),
    ('Olivia Rodrigo - stupid song', 'Epic Gaming Montage Music Mix', False),
    ('Anything', '', True),
    ('', 'Anything', True),
])
def test_titles_match(expected, actual, match):
    assert convert_module._titles_match(expected, actual) is match


def test_verify_output_rejects_corrupted(monkeypatch, tmp_path):
    target = tmp_path / 'x.flac'
    target.write_bytes(b'junk')
    monkeypatch.setattr(convert_module, '_is_valid_audio', lambda p: False)
    ok, reason = convert_module._verify_output(str(target), expected_duration=200)
    assert ok is False
    assert 'corrupt' in reason


def test_verify_output_rejects_truncated(monkeypatch, tmp_path):
    target = tmp_path / 'x.flac'
    target.write_bytes(b'data')
    monkeypatch.setattr(convert_module, '_is_valid_audio', lambda p: True)
    monkeypatch.setattr(convert_module, '_probe_duration', lambda p: 100.0)
    ok, reason = convert_module._verify_output(str(target), expected_duration=200)
    assert ok is False
    assert 'truncated' in reason


def test_verify_output_rejects_wrong_length(monkeypatch, tmp_path):
    target = tmp_path / 'x.flac'
    target.write_bytes(b'data')
    monkeypatch.setattr(convert_module, '_is_valid_audio', lambda p: True)
    monkeypatch.setattr(convert_module, '_probe_duration', lambda p: 36000.0)
    ok, reason = convert_module._verify_output(str(target), expected_duration=200)
    assert ok is False
    assert 'length' in reason


def test_verify_output_accepts_full_length(monkeypatch, tmp_path):
    target = tmp_path / 'x.flac'
    target.write_bytes(b'data')
    monkeypatch.setattr(convert_module, '_is_valid_audio', lambda p: True)
    monkeypatch.setattr(convert_module, '_probe_duration', lambda p: 195.0)
    ok, _ = convert_module._verify_output(str(target), expected_duration=200)
    assert ok is True


def test_verify_output_rejects_wrong_song(monkeypatch, tmp_path):
    target = tmp_path / 'x.flac'
    target.write_bytes(b'data')
    monkeypatch.setattr(convert_module, '_is_valid_audio', lambda p: True)
    monkeypatch.setattr(convert_module, '_probe_metadata',
                        lambda p: {'title': '10 Hours of Rain Sounds'})
    ok, reason = convert_module._verify_output(
        str(target), expected_title='The Beatles - Yesterday')
    assert ok is False
    assert 'match' in reason


def test_verify_output_accepts_matching_song(monkeypatch, tmp_path):
    target = tmp_path / 'x.flac'
    target.write_bytes(b'data')
    monkeypatch.setattr(convert_module, '_is_valid_audio', lambda p: True)
    monkeypatch.setattr(convert_module, '_probe_metadata',
                        lambda p: {'title': 'Drive My Car (Remastered 2009)'})
    ok, _ = convert_module._verify_output(
        str(target), expected_title='The Beatles - Drive My Car')
    assert ok is True


def test_extract_metadata_captures_duration(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = json.dumps({'title': 'Song', 'duration': 213.5})
        stderr = ''

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())
    assert convert_module.extract_metadata('https://youtu.be/x')['duration'] == 213.5


def test_search_prefers_duration_match(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = json.dumps({'entries': [
            {'webpage_url': 'https://www.youtube.com/watch?v=wrong',
             'duration': 36000, 'title': '10 Hour Loop'},
            {'webpage_url': 'https://www.youtube.com/watch?v=right',
             'duration': 215, 'title': 'Artist - Song'},
        ]})
        stderr = ''

    monkeypatch.setattr(convert_module, '_run_ytdlp',
                        lambda args, url, timeout: FakeResult())
    assert convert_module.search_track_url('Artist - Song',
                                           expected_duration=213) == \
        'https://www.youtube.com/watch?v=right'


def test_expand_import_stores_expectations(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'resolve_import_urls', lambda url: {
        'success': True, 'title': 'Gym Hits', 'source': 'spotify',
        'urls': ['https://www.youtube.com/watch?v=1'],
        'items': [{'url': 'https://www.youtube.com/watch?v=1',
                   'expected_title': 'A - One', 'expected_duration': 200}],
        'found': 1, 'total': 1,
    })

    with client.application.app_context():
        parent = ConversionHistory(
            url='https://open.spotify.com/playlist/X',
            format='FLAC', output_path=str(tmp_path), status='pending',
            is_playlist=True)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        convert_module._expand_playlist_job(parent)

        child = ConversionHistory.query.filter_by(parent_id=pid).first()
        assert child is not None
        assert child.expected_title == 'A - One'
        assert child.expected_duration == 200


# ------------------------------------------------------- skip + retry ----

def test_skip_pending_job(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x', status='downloading',
                                progress=10)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/api/skip/{job_id}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'skipped': 1}

    with client.application.app_context():
        job = db.session.get(ConversionHistory, job_id)
        assert job.status == 'skipped'
        assert job.progress == 100
        assert job.error == 'Skipped by user'


def test_skip_completed_returns_400(client, completed_conversion):
    job_id, _ = completed_conversion
    resp = client.get(f'/api/skip/{job_id}')
    assert resp.status_code == 400


def test_skip_missing_returns_404(client):
    assert client.get('/api/skip/999999').status_code == 404


def test_skip_playlist_skips_active_children(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, item_count=3)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add_all([
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path='/tmp/p/1.flac', status='completed',
                              parent_id=pid, item_index=0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path='/tmp/p', status='downloading',
                              parent_id=pid, item_index=1),
            ConversionHistory(url='https://youtu.be/3', format='FLAC',
                              output_path='/tmp/p', status='pending',
                              parent_id=pid, item_index=2),
        ])
        db.session.commit()

    resp = client.get(f'/api/skip/{pid}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'skipped': 2}

    with client.application.app_context():
        kids = ConversionHistory.query.filter_by(parent_id=pid).all()
        by_index = {k.item_index: k for k in kids}
        assert by_index[0].status == 'completed'
        assert by_index[1].status == 'skipped'
        assert by_index[2].status == 'skipped'
        parent = db.session.get(ConversionHistory, pid)
        assert parent.status == 'completed'
        assert '2 skipped' in (parent.error or '')


def test_refresh_parent_reports_user_skips_separately(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add_all([
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path='/tmp/p/1.flac', status='skipped',
                              error='Already exists on disk',
                              parent_id=pid, item_index=0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path='/tmp/p', status='skipped',
                              error='Skipped by user',
                              parent_id=pid, item_index=1),
        ])
        db.session.commit()

        convert_module._refresh_playlist_parent(pid)
        parent = db.session.get(ConversionHistory, pid)
        assert parent.status == 'completed'
        assert '1 already existed' in (parent.error or '')
        assert '1 skipped' in (parent.error or '')


def test_settings_retry_count_round_trip(client):
    resp = client.post('/settings', data={
        'output_path': os.path.expanduser('~/Music/Converter'),
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'retry_count': '7',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        from app.models import UserSettings
        assert UserSettings.query.first().retry_count == 7

    page = client.get('/settings')
    assert b'value="7"' in page.data


def test_settings_retry_count_clamped_and_defaulted(client):
    resp = client.post('/settings', data={
        'output_path': os.path.expanduser('~/Music/Converter'),
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'retry_count': 'not-a-number',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        from app.models import UserSettings
        assert UserSettings.query.first().retry_count == 3


def test_settings_toggle_existing_row(client):
    from app.models import UserSettings
    with client.application.app_context():
        db.session.add(UserSettings(output_path='/tmp/a', skip_existing=True,
                                    retry_count=3))
        db.session.commit()

    # Uncheck skip_existing and change retries on the EXISTING row
    resp = client.post('/settings', data={
        'output_path': '/tmp/a',
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'retry_count': '1',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        settings = UserSettings.query.first()
        assert settings.skip_existing is False
        assert settings.retry_count == 1


def test_job_finish_applies_when_active(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x', status='downloading',
                                progress=10)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        assert convert_module._job_finish(job, status='completed',
                                          progress=100) is True
        job = db.session.get(ConversionHistory, job_id)
        assert job.status == 'completed'
        assert job.progress == 100


def test_job_finish_refuses_when_skipped(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x', status='skipped',
                                progress=100, error='Skipped by user')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        assert convert_module._job_finish(job, status='completed',
                                          progress=100) is False
        job = db.session.get(ConversionHistory, job_id)
        assert job.status == 'skipped'
        assert job.error == 'Skipped by user'


# ------------------------------------------------------- history UI ----

def _home_with_history(client):
    from app.models import ConversionHistory
    with client.application.app_context():
        for i in range(3):
            db.session.add(ConversionHistory(
                url=f'https://youtu.be/h{i}', format='FLAC',
                output_path=f'/tmp/h{i}.flac', status='completed',
                progress=100))
        db.session.commit()
    return client.get('/')


def test_home_history_has_toolbar(client):
    page = _home_with_history(client)
    assert page.status_code == 200
    for marker in (b'id="historySearch"', b'id="historyStatus"',
                   b'id="historyPrev"', b'id="historyNext"',
                   b'id="historyCount"', b'history-table',
                   b'class="row mt-3"'):
        assert marker in page.data


def test_home_history_full_width_layout(client):
    page = _home_with_history(client)
    html = page.data.decode('utf-8')
    assert 'col-lg-5' in html
    assert 'col-lg-7' in html
    assert '<div class="col-12">' in html


def test_stylesheet_uses_wide_layout(client):
    import os as _os
    css_path = _os.path.join(_os.path.dirname(__file__), '..', 'src',
                             'static', 'css', 'style.css')
    with open(css_path) as fh:
        css = fh.read()
    assert 'max-width: 1200px' in css
    assert '.history-toolbar' in css


# ------------------------------------------- queue/net/library ----

def test_queue_pause_resume_status(client):
    data = client.get('/api/queue/status').get_json()
    assert data['ok'] is True
    assert data['paused'] is False
    assert data['pending'] >= 0
    assert client.get('/api/queue/pause').get_json()['paused'] is True
    assert client.get('/api/queue/status').get_json()['paused'] is True
    assert client.get('/api/queue/resume').get_json()['paused'] is False


def test_download_skipped_when_disk_full(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'extract_metadata',
                        lambda url: {'title': 'Track', 'duration': 200})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: False)

    class FakeUsage:
        free = 1

    monkeypatch.setattr(convert_module.shutil, 'disk_usage', lambda p: FakeUsage())

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(tmp_path), status='downloading')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'flac', str(tmp_path), job)
    assert result['success'] is False
    assert 'disk space' in result['error'].lower()


def test_ytdlp_net_args_proxy_and_rate(client, monkeypatch):
    from app.models import UserSettings
    with client.application.app_context():
        db.session.add(UserSettings(output_path='/tmp/x', bandwidth_limit=500,
                                    proxy='socks5://127.0.0.1:9050'))
        db.session.commit()
        args = convert_module._ytdlp_net_args()
    assert '--limit-rate' in args
    assert '500K' in args
    # Proxy credentials must not appear in argv (visible via ps);
    # they travel in the subprocess environment instead.
    assert '--proxy' not in args
    assert not any('9050' in a for a in args)
    with client.application.app_context():
        env = convert_module._ytdlp_env()
    assert env is not None
    assert env['https_proxy'] == 'socks5://127.0.0.1:9050'
    assert env['HTTPS_PROXY'] == 'socks5://127.0.0.1:9050'


def test_ytdlp_env_none_without_proxy(client):
    with client.application.app_context():
        assert convert_module._ytdlp_env() is None


def test_ytdlp_net_args_empty_by_default(client):
    with client.application.app_context():
        assert convert_module._ytdlp_net_args() == []


def test_delete_completed_file_and_row(client, completed_conversion):
    job_id, path = completed_conversion
    assert os.path.isfile(path)

    resp = client.post(f'/api/delete/{job_id}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'removed_file': True,
                               'trashed': True,
                               'message': 'Moved to Trash.'}
    assert not os.path.exists(path)

    with client.application.app_context():
        assert db.session.get(ConversionHistory, job_id) is None


def test_delete_active_job_rejected(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x', status='downloading')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    assert client.post(f'/api/delete/{job_id}').status_code == 400


def test_delete_playlist_rejected(client):
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                output_path='/tmp/p', status='completed',
                                is_playlist=True)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    assert client.post(f'/api/delete/{job_id}').status_code == 400


def test_storage_counts_files(client, completed_conversion, tmp_path):
    job_id, path = completed_conversion
    resp = client.get('/api/storage')
    data = resp.get_json()
    assert data['ok'] is True
    assert data['files'] >= 1
    assert data['bytes'] >= os.path.getsize(path)
    assert data['readable'].endswith(('B', 'KB', 'MB', 'GB', 'TB'))


def test_search_falls_back_to_soundcloud(monkeypatch):
    calls = []

    def fake_search_once(engine, query, timeout):
        calls.append(engine)
        return []

    monkeypatch.setattr(convert_module, '_search_once', fake_search_once)
    assert convert_module.search_track_url('Nobody - No Song') is None
    assert calls == ['ytsearch5', 'scsearch1']


def test_search_uses_soundcloud_hit(monkeypatch):
    def fake_search_once(engine, query, timeout):
        if engine == 'ytsearch5':
            return []
        return [{'webpage_url': 'https://soundcloud.com/artist/song',
                 'title': 'Artist - Song'}]

    monkeypatch.setattr(convert_module, '_search_once', fake_search_once)
    assert convert_module.search_track_url('Artist - Song') == \
        'https://soundcloud.com/artist/song'


def test_duration_close_handles_milliseconds():
    assert convert_module._duration_close(213000, 213) is True
    assert convert_module._duration_close(215, 213) is True
    assert convert_module._duration_close(36000, 213) is False
    assert convert_module._duration_close(0, 213) is False


def test_resolve_import_records_misses(monkeypatch):
    monkeypatch.setattr(convert_module, 'resolve_streaming_playlist', lambda url, max_tracks=50: {
        'success': True, 'title': 'Mix', 'source': 'apple',
        'tracks': [{'artist': 'A', 'title': 'Hit', 'duration': 200},
                   {'artist': 'B', 'title': 'Miss', 'duration': 180}],
    })
    monkeypatch.setattr(convert_module, 'search_track_url',
                        lambda q, timeout=60, expected_duration=None:
                        'https://youtu.be/hit' if 'Hit' in q else None)
    result = convert_module.resolve_import_urls('https://music.apple.com/x')
    assert result['found'] == 1
    assert result['misses'] == [{'artist': 'B', 'title': 'Miss', 'duration': 180}]


def test_retry_misses_queues_hits(client, tmp_path, monkeypatch):
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://open.spotify.com/playlist/X', format='FLAC',
            output_path=str(tmp_path), status='completed', is_playlist=True,
            playlist_title='Mix', item_count=1, import_source='spotify',
            import_misses='[{"artist": "B", "title": "Miss", "duration": 180}]')
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

        monkeypatch.setattr(convert_module, 'search_track_url',
                            lambda q, timeout=60, expected_duration=None:
                            'https://youtu.be/miss')
        monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

        resp = client.post(f'/api/retry-misses/{pid}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {'ok': True, 'queued': 1, 'remaining': 0}

        parent = db.session.get(ConversionHistory, pid)
        assert parent.import_misses == '[]'
        child = ConversionHistory.query.filter_by(parent_id=pid).first()
        assert child is not None
        assert child.url == 'https://youtu.be/miss'
        assert child.expected_title == 'B - Miss'


def test_retry_misses_empty_returns_400(client):
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://open.spotify.com/playlist/X', format='FLAC',
            output_path='/tmp/p', status='completed', is_playlist=True)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

    assert client.post(f'/api/retry-misses/{pid}').status_code == 400


def test_settings_network_fields_round_trip(client):
    resp = client.post('/settings', data={
        'output_path': os.path.expanduser('~/Music/Converter'),
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'retry_count': '3',
        'job_timeout': '600',
        'bandwidth_limit': '1000',
        'proxy': 'socks5://127.0.0.1:9050',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        from app.models import UserSettings
        settings = UserSettings.query.first()
        assert settings.job_timeout == 600
        assert settings.bandwidth_limit == 1000
        assert settings.proxy == 'socks5://127.0.0.1:9050'

    page = client.get('/settings')
    assert b'name="job_timeout"' in page.data
    assert b'name="bandwidth_limit"' in page.data
    assert b'name="proxy"' in page.data


def test_home_has_csrf_meta_queue_and_folder_controls(client):
    page = _home_with_history(client)
    for marker in (b'name="csrf-token"', b'id="queuePause"',
                   b'id="historyFolder"', b'id="storageInfo"',
                   b'data-delete', b'data-folder'):
        assert marker in page.data


def test_home_parent_shows_retry_unmatched(client):
    from app.models import ConversionHistory
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://open.spotify.com/playlist/X', format='FLAC',
            output_path='/tmp/p', status='completed', is_playlist=True,
            playlist_title='Mix', item_count=1, import_source='spotify',
            import_misses='[{"artist": "B", "title": "Miss"}]'))
        db.session.commit()
    page = client.get('/')
    assert b'data-retry-misses' in page.data
    assert b'Retry unmatched' in page.data


def test_resource_base_frozen_and_source(monkeypatch):
    import sys
    from app import _resource_base
    assert os.path.normpath(_resource_base()).endswith('src')

    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', '/bundle', raising=False)
    assert _resource_base() == '/bundle'


# ----------------------------------------- alternate sources ----

def _candidate_search_fake(mapping):
    def fake(engine, query, timeout):
        return [dict(e) for e in mapping.get(engine, [])]
    return fake


def test_search_candidates_rank_match_first(monkeypatch):
    monkeypatch.setattr(convert_module, '_search_once', _candidate_search_fake({
        'ytsearch5': [
            {'webpage_url': 'https://www.youtube.com/watch?v=top', 'duration': 30},
            {'webpage_url': 'https://www.youtube.com/watch?v=match', 'duration': 212},
        ],
        'scsearch1': [
            {'webpage_url': 'https://soundcloud.com/a/song', 'duration': 214},
            {'webpage_url': 'https://soundcloud.com/a/other', 'duration': 5},
        ],
    }))
    urls = [h['url'] for h in convert_module._search_candidates(
        'Artist - Song', expected_duration=213)]
    assert urls == [
        'https://www.youtube.com/watch?v=match',
        'https://www.youtube.com/watch?v=top',
        'https://soundcloud.com/a/song',
        'https://soundcloud.com/a/other',
    ]


def test_search_candidates_excludes_original_and_limits(monkeypatch):
    monkeypatch.setattr(convert_module, '_search_once', _candidate_search_fake({
        'ytsearch5': [
            {'webpage_url': 'https://www.youtube.com/watch?v=dead&si=x'},
            {'webpage_url': 'https://www.youtube.com/watch?v=other'},
        ],
        'scsearch1': [],
    }))
    urls = [h['url'] for h in convert_module._search_candidates(
        'Q', limit=1, exclude=('https://www.youtube.com/watch?v=dead',))]
    assert urls == ['https://www.youtube.com/watch?v=other']


def test_find_alternate_sources_queries_artist_title(monkeypatch):
    seen = {}

    def fake_candidates(query, timeout=60, expected_duration=None, limit=6,
                        exclude=()):
        seen['query'] = query
        seen['expected'] = expected_duration
        seen['exclude'] = exclude
        return [{'url': 'https://soundcloud.com/a/song'}]

    monkeypatch.setattr(convert_module, '_search_candidates', fake_candidates)
    urls = convert_module.find_alternate_sources(
        'https://www.youtube.com/watch?v=dead',
        {'artist': 'Artist', 'title': 'Song', 'duration': 200})
    assert urls == ['https://soundcloud.com/a/song']
    assert seen['query'] == 'Artist - Song'
    assert seen['expected'] == 200
    assert seen['exclude'] == ('https://www.youtube.com/watch?v=dead',)


def test_download_falls_back_to_alternate(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'extract_metadata', lambda url: {
        'title': 'Song', 'artist': 'Artist', 'duration': 200, 'thumbnail': ''})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: False)
    monkeypatch.setattr(convert_module, 'check_ffmpeg', lambda: True)

    calls = []

    def fake_download(url, temp_audio, job=None):
        calls.append(url)
        if 'soundcloud.com' in url:
            open(temp_audio, 'wb').write(b'data')
            return {'success': True}
        return {'success': False, 'error': 'yt-dlp download failed'}

    monkeypatch.setattr(convert_module, 'download_audio', fake_download)
    monkeypatch.setattr(convert_module, 'find_alternate_sources',
                        lambda url, meta, expected='', limit=4:
                        ['https://soundcloud.com/artist/song'])
    monkeypatch.setattr(convert_module, 'convert_audio_file',
                        lambda *a: {'success': True})
    monkeypatch.setattr(convert_module, '_verify_output',
                        lambda *a, **k: (True, 'File verified'))
    monkeypatch.setattr(convert_module, '_fetch_thumbnail', lambda url: None)

    with client.application.app_context():
        job = ConversionHistory(url='https://www.youtube.com/watch?v=dead',
                                format='FLAC', output_path=str(tmp_path),
                                status='downloading')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'flac', str(tmp_path), job)

    assert result['success'] is True
    assert result['via'] == 'https://soundcloud.com/artist/song'
    assert calls[0] == 'https://www.youtube.com/watch?v=dead'
    assert calls[-1] == 'https://soundcloud.com/artist/song'


def test_download_skips_mismatched_alternate(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'extract_metadata', lambda url: {
        'title': 'Song' if 'youtube' in url else 'Epic Gaming Montage',
        'artist': 'Artist', 'duration': 200, 'thumbnail': ''})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: False)

    def fake_download(url, temp_audio, job=None):
        return {'success': False, 'error': 'yt-dlp download failed'}

    monkeypatch.setattr(convert_module, 'download_audio', fake_download)
    monkeypatch.setattr(convert_module, 'find_alternate_sources',
                        lambda url, meta, expected='', limit=4:
                        ['https://soundcloud.com/a/montage'])

    with client.application.app_context():
        job = ConversionHistory(url='https://www.youtube.com/watch?v=dead',
                                format='FLAC', output_path=str(tmp_path),
                                status='downloading', expected_title='Artist - Song')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'flac', str(tmp_path), job)

    assert result['success'] is False
    assert 'via' not in result


def test_alternates_interleave_platforms(monkeypatch):
    monkeypatch.setattr(convert_module, '_search_candidates', lambda *a, **k: [
        {'url': 'https://www.youtube.com/watch?v=y1', 'engine': 'ytsearch5'},
        {'url': 'https://www.youtube.com/watch?v=y2', 'engine': 'ytsearch5'},
        {'url': 'https://soundcloud.com/a/s', 'engine': 'scsearch1'},
    ])
    urls = convert_module.find_alternate_sources(
        'https://www.youtube.com/watch?v=dead', {'title': 'Song'})
    assert urls == [
        'https://www.youtube.com/watch?v=y1',
        'https://soundcloud.com/a/s',
        'https://www.youtube.com/watch?v=y2',
    ]


def test_home_top_row_columns_are_siblings(client):
    """Regression: the info column must sit beside the form column, not inside it."""
    from html.parser import HTMLParser as _HP

    class Walk(_HP):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.rows = []

        def handle_starttag(self, tag, attrs):
            if tag == 'div':
                self.depth += 1
                cls = dict(attrs).get('class', '')
                if 'row' in cls.split() and not any('col-' in c for c in cls.split()):
                    self.rows.append({'depth': self.depth, 'kids': []})
                for row in self.rows:
                    if self.depth == row['depth'] + 1:
                        row['kids'].append(dict(attrs).get('class', ''))

        def handle_endtag(self, tag):
            if tag == 'div':
                self.depth -= 1

    page = client.get('/')
    assert page.status_code == 200
    walk = Walk()
    walk.feed(page.data.decode('utf-8'))
    top = walk.rows[0]
    assert any('col-lg-5' in k for k in top['kids'])
    assert any('col-lg-7' in k for k in top['kids'])


# ------------------------------------------------------- tidal ----

def _tidal_settings(client, **overrides):
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        for key, value in overrides.items():
            setattr(settings, key, value)
        db.session.commit()


def test_tidal_status_disconnected(client):
    assert client.get('/api/tidal/status').get_json() == {
        'ok': True, 'connected': False}


def test_tidal_login_needs_client_id(client):
    resp = client.get('/api/tidal/login')
    assert resp.status_code == 302


def test_tidal_login_redirects_with_state(client):
    _tidal_settings(client, tidal_client_id='abc123')
    resp = client.get('/api/tidal/login')
    assert resp.status_code == 302
    assert 'auth.tidal.com' in resp.headers['Location']
    assert 'client_id=abc123' in resp.headers['Location']


def test_tidal_callback_rejects_bad_state(client):
    _tidal_settings(client, tidal_client_id='abc123')
    resp = client.get('/api/tidal/callback?state=nope&code=zzz')
    assert resp.status_code == 302


def test_tidal_callback_exchanges_code(client, monkeypatch):
    import app.routes.tidal as tidal_module
    _tidal_settings(client, tidal_client_id='abc123',
                    tidal_client_secret='shh')

    class FakeResp:
        status_code = 200

        def json(self):
            return {'access_token': 'AT', 'refresh_token': 'RT',
                    'expires_in': 3600}

    class FakeSession:
        def __init__(self):
            self.cookies = type('C', (), {'clear': staticmethod(lambda: None)})()

        def post(self, url, **kwargs):
            assert 'oauth2/token' in url
            return FakeResp()

    monkeypatch.setattr(tidal_module.requests, 'Session', FakeSession)
    with client.session_transaction() as session:
        session['tidal_oauth_state'] = 'good-state'
    resp = client.get('/api/tidal/callback?state=good-state&code=zzz')
    assert resp.status_code == 302

    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        assert settings.tidal_access_token == 'AT'
        assert settings.tidal_refresh_token == 'RT'
        assert client.get('/api/tidal/status').get_json()['connected'] is True


def test_tidal_extract_playlist(client, monkeypatch):
    import app.routes.tidal as tidal_module
    _tidal_settings(client, tidal_client_id='abc123',
                    tidal_access_token='AT', tidal_expires_at=9999999999)

    def fake_api_get(path, params=None):
        if path.endswith('/items'):
            return {'totalNumberOfItems': 2, 'items': [
                {'item': {'title': 'Song One',
                          'artists': [{'name': 'Artist A'}], 'duration': 200}},
                {'item': {'title': 'Song Two',
                          'artists': [{'name': 'B'}, {'name': 'C'}],
                          'duration': 180}},
            ]}
        return {'title': 'Tidal Mix'}

    monkeypatch.setattr(tidal_module, '_api_get', fake_api_get)
    result = tidal_module.extract_tidal_playlist(
        'https://tidal.com/browse/playlist/01854b76-1c45-4c94-9743-92ce6d9aff86')
    assert result['success'] is True
    assert result['title'] == 'Tidal Mix'
    assert result['tracks'] == [
        {'artist': 'Artist A', 'title': 'Song One', 'duration': 200.0},
        {'artist': 'B, C', 'title': 'Song Two', 'duration': 180.0},
    ]


def test_tidal_extract_bad_id():
    import app.routes.tidal as tidal_module
    result = tidal_module.extract_tidal_playlist(
        'https://tidal.com/browse/track/123')
    assert result['success'] is False


# ------------------------------------------------------- library ----

def test_rename_file(client, completed_conversion):
    job_id, path = completed_conversion
    directory = os.path.dirname(path)

    resp = client.post(f'/api/rename/{job_id}', json={'name': 'Renamed Song'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['filename'] == 'Renamed Song.flac'
    assert os.path.isfile(os.path.join(directory, 'Renamed Song.flac'))
    assert not os.path.exists(path)
    os.unlink(os.path.join(directory, 'Renamed Song.flac'))


def test_rename_rejects_traversal_and_blanks(client, tmp_path):
    target = tmp_path / 'track.flac'
    target.write_bytes(b'fLaC')
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(target), status='completed',
                                progress=100)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    # '?' and '/' are sanitized, so this stays inside the folder (200) —
    # the point is nothing escapes `directory`.
    resp = client.post(f'/api/rename/{job_id}', json={'name': '../../evil'})
    assert resp.status_code == 200
    assert os.path.dirname(
        resp.get_json()['output_path']) == str(tmp_path)
    assert client.post(f'/api/rename/{job_id}',
                       json={'name': '   '}).status_code == 400


def test_rename_rejects_collision(client, tmp_path):
    (tmp_path / 'Taken.flac').write_bytes(b'x')
    target = tmp_path / 'Mine.flac'
    target.write_bytes(b'y')
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(target), status='completed',
                                progress=100)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    assert client.post(f'/api/rename/{job_id}',
                       json={'name': 'Taken'}).status_code == 400


def test_edit_metadata_tags(client, tmp_path):
    src = tmp_path / 'song.flac'
    import subprocess as _sp
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=2', '-c:a', 'flac',
             str(src)], check=True)
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(src), status='completed',
                                progress=100)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.post(f'/api/metadata/{job_id}',
                       json={'title': 'New Title', 'artist': 'New Artist'})
    assert resp.status_code == 200
    meta = resp.get_json()['meta']
    assert meta['title'] == 'New Title'
    assert meta['artist'] == 'New Artist'


def test_cover_art_served_and_missing(client, tmp_path):
    import subprocess as _sp
    tagged = tmp_path / 'tagged.flac'
    plain = tmp_path / 'plain.flac'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1', '-c:a', 'flac',
             str(plain)], check=True)
    cover = tmp_path / 'cover.jpg'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'color=c=red:s=16x16:d=1', '-frames:v', '1',
             str(cover)], check=True)
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-i', str(plain),
             '-i', str(cover), '-map', '0:a', '-map', '1:v',
             '-c:a', 'flac', '-c:v', 'copy', '-disposition:v',
             'attached_pic', str(tagged)], check=True)

    with client.application.app_context():
        good = ConversionHistory(url='https://youtu.be/a', format='FLAC',
                                 output_path=str(tagged), status='completed')
        bad = ConversionHistory(url='https://youtu.be/b', format='FLAC',
                                output_path=str(plain), status='completed')
        db.session.add_all([good, bad])
        db.session.commit()
        good_id, bad_id = good.id, bad.id

    resp = client.get(f'/api/cover/{good_id}')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'image/jpeg'
    assert len(resp.data) > 100

    assert client.get(f'/api/cover/{bad_id}').status_code == 404


def test_retry_failed_job(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x', status='failed',
                                progress=40, retry_attempts=3,
                                error='Max retries exceeded')
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.post(f'/api/retry/{job_id}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'retried': 1}

    with client.application.app_context():
        job = db.session.get(ConversionHistory, job_id)
        assert job.status == 'pending'
        assert job.retry_attempts == 0
        assert job.error is None


def test_retry_completed_returns_400(client, completed_conversion):
    job_id, _ = completed_conversion
    assert client.post(f'/api/retry/{job_id}').status_code == 400


def test_retry_playlist_failed_children(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='failed',
                                   is_playlist=True, item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add_all([
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path='/tmp/p', status='failed',
                              parent_id=pid, item_index=0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path='/tmp/p/2.flac', status='completed',
                              parent_id=pid, item_index=1),
        ])
        db.session.commit()

    resp = client.post(f'/api/retry/{pid}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'retried': 1}


def test_tidal_login_uses_pkce_without_secret(client):
    _tidal_settings(client, tidal_client_id='abc123', tidal_client_secret='')
    resp = client.get('/api/tidal/login')
    assert resp.status_code == 302
    location = resp.headers['Location']
    assert 'code_challenge=' in location
    assert 'code_challenge_method=S256' in location
    with client.session_transaction() as session:
        assert session.get('tidal_oauth_verifier')


def test_tidal_callback_without_secret(client, monkeypatch):
    import app.routes.tidal as tidal_module
    _tidal_settings(client, tidal_client_id='abc123', tidal_client_secret='')
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {'access_token': 'AT2', 'expires_in': 100}

    class FakeSession:
        def __init__(self):
            self.cookies = type('C', (), {'clear': staticmethod(lambda: None)})()

        def post(self, url, **kwargs):
            seen['data'] = kwargs.get('data', {})
            return FakeResp()

    monkeypatch.setattr(tidal_module.requests, 'Session', FakeSession)
    with client.session_transaction() as session:
        session['tidal_oauth_state'] = 's3'
        session['tidal_oauth_verifier'] = 'verifier-xyz'
    resp = client.get('/api/tidal/callback?state=s3&code=zzz')
    assert resp.status_code == 302
    assert seen['data'].get('code_verifier') == 'verifier-xyz'
    assert 'client_secret' not in seen['data']

    from app.models import UserSettings
    with client.application.app_context():
        assert UserSettings.query.first().tidal_access_token == 'AT2'


# ------------------------------------------------------- privacy ----

def _privacy_settings(client, privacy):
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        settings.privacy_mode = privacy
        db.session.commit()


def test_privacy_off_skips_sanitize_and_flags(client, monkeypatch, tmp_path):
    _privacy_settings(client, False)
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

    out = tmp_path / 'out'
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/watch?v=abc&si=XYZ',
        'format': 'flac',
        'output_path': str(out),
    })
    assert resp.status_code == 302

    with client.application.app_context():
        job = ConversionHistory.query.order_by(
            ConversionHistory.created_at.desc()).first()
        assert 'si=XYZ' in job.url

        assert convert_module._active_privacy_flags() == []
        seen = {}

        class FakeResult:
            returncode = 0
            stdout = '{}'
            stderr = ''

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd
            return FakeResult()

        monkeypatch.setattr(convert_module.subprocess, 'run', fake_run)
        convert_module._run_ytdlp(['--skip-download', 'https://youtu.be/x'],
                                  'https://youtu.be/x', timeout=5)
        assert '--no-cookies' not in seen['cmd']


def test_privacy_on_by_default(client):
    with client.application.app_context():
        assert convert_module._privacy_on() is True
        for flag in convert_module._YTDLP_PRIVACY_FLAGS:
            assert flag in convert_module._active_privacy_flags()


def test_settings_privacy_round_trip(client):
    _privacy_settings(client, True)
    resp = client.post('/settings', data={
        'output_path': '/tmp/x',
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'retry_count': '3',
        'job_timeout': '300',
        'bandwidth_limit': '0',
        'worker_count': '3',
        # privacy_mode checkbox omitted -> off
    })
    assert resp.status_code == 302

    from app.models import UserSettings
    with client.application.app_context():
        assert UserSettings.query.first().privacy_mode is False
    assert b'id="privacy_mode"' in client.get('/settings').data


# ------------------------------------------------------- removed auto ----

def test_organization_auto_maps_to_folder(client, monkeypatch, tmp_path):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)

    out = tmp_path / 'out'
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/playlist?list=PL123',
        'format': 'flac',
        'output_path': str(out),
        'organization': 'auto',
    })
    assert resp.status_code == 302

    with client.application.app_context():
        job = ConversionHistory.query.order_by(
            ConversionHistory.created_at.desc()).first()
        assert job.playlist_organization == 'folder'


def test_no_auto_option_in_forms(client):
    for path in ('/', '/convert'):
        html = client.get(path).data.decode('utf-8')
        assert 'value="auto">Auto:' not in html


# ------------------------------------------------------- toolbar UI ----

def test_home_has_verify_sort_and_retry_markers(client):
    page = _home_with_history(client)
    for marker in (b'id="verifyFiles"', b'id="historySort"',
                   b'name="csrf-token"'):
        assert marker in page.data

    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/bad', format='FLAC',
            output_path='/tmp/bad.flac', status='failed',
            error='boom'))
        db.session.commit()
    assert b'data-retry' in client.get('/').data


def test_settings_has_speed_tidal_helpers(client):
    page = client.get('/settings')
    for marker in (b'speedGentle', b'speedBalanced', b'speedFast',
                   b'name="worker_count"', b'tidal_client_id',
                   b'id="helpersCheck"', b'id="helpersUpdate"',
                   b'id="updateCheck"'):
        assert marker in page.data


# ------------------------------------------------------- helpers ----

def test_api_helpers_structure(client):
    data = client.get('/api/helpers').get_json()
    assert data['ok'] is True
    assert data['ffmpeg']
    assert data['ytdlp']['current']
    assert 'update_available' in data['ytdlp']


def test_api_update_ytdlp_without_binary(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_find_ytdlp', lambda: None)
    resp = client.post('/api/helpers/update-ytdlp')
    assert resp.status_code == 400


def test_update_check_without_feed(client, monkeypatch):
    import app.routes.settings as settings_module
    monkeypatch.delenv('AUDIO_CONVERTER_UPDATE_FEED', raising=False)

    class GoneResp:
        status_code = 404

    monkeypatch.setattr(settings_module.requests, 'get',
                        lambda url, **kw: GoneResp())
    # Default feed unreachable -> clean error, not a crash.
    resp = client.get('/api/update-check')
    assert resp.status_code == 502


def test_update_check_github_shape(client, monkeypatch):
    import app.routes.settings as settings_module
    monkeypatch.delenv('AUDIO_CONVERTER_UPDATE_FEED', raising=False)

    class FakeResp:
        status_code = 200

        def json(self):
            return {'tag_name': 'v99.0', 'html_url': 'https://example.com/r'}

    monkeypatch.setattr(settings_module.requests, 'get',
                        lambda url, **kw: FakeResp())
    data = client.get('/api/update-check').get_json()
    assert data['update_available'] is True
    assert data['latest'] == 'v99.0'


def test_update_check_newer_and_older(client, monkeypatch):
    import app.routes.settings as settings_module
    monkeypatch.setenv('AUDIO_CONVERTER_UPDATE_FEED', 'https://example.com/feed')

    class FakeResp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    monkeypatch.setattr(settings_module.requests, 'get',
                        lambda url, **kw: FakeResp({'version': '99.0', 'url': 'https://x'}))
    data = client.get('/api/update-check').get_json()
    assert data['update_available'] is True
    assert data['latest'] == '99.0'

    monkeypatch.setattr(settings_module.requests, 'get',
                        lambda url, **kw: FakeResp({'version': '0.0.1'}))
    data = client.get('/api/update-check').get_json()
    assert data['update_available'] is False


def test_pool_resize_grows_and_shrinks(client):
    import threading
    import time as _time

    cm = convert_module

    def living():
        return sum(1 for t in threading.enumerate()
                   if t.name.startswith('audio-worker-') and t.is_alive())

    def wait_for(count, timeout=20):
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if living() == count:
                return True
            _time.sleep(0.2)
        return False

    with client.application.app_context():
        # Hold the queue paused so idle test workers never consume jobs
        # queued by other tests.
        cm._queue_paused.set()
        try:
            cm._ensure_worker()
            assert wait_for(3)
            assert cm.set_worker_count(6) == 6
            assert wait_for(6)
            assert cm.set_worker_count(1) == 1
            assert wait_for(1)
            assert cm.set_worker_count(99) == 8
            assert cm.set_worker_count(0) == 1
        finally:
            cm._desired_workers = 1
            assert wait_for(1)
            # NOTE: the queue stays paused on purpose; no test needs live
            # workers, and this guarantees these idle threads stay idle.


# ------------------------------------------------------- library+ ----

def test_delete_playlist_removes_tracks_and_folder(client, tmp_path):
    folder = tmp_path / 'Mix'
    folder.mkdir()
    one = folder / 'one.flac'
    one.write_bytes(b'x')
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path=str(folder), status='completed',
                                   is_playlist=True, playlist_title='Mix',
                                   item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add_all([
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path=str(one), status='completed',
                              parent_id=pid, item_index=0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path=str(folder / 'two.flac'),
                              status='failed', parent_id=pid, item_index=1),
        ])
        db.session.commit()

    resp = client.post(f'/api/delete-playlist/{pid}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'removed_tracks': 2,
                               'removed_files': 1, 'trashed_files': 1,
                               'kept_files': 0}
    assert not one.exists()
    assert not folder.exists()

    with client.application.app_context():
        assert db.session.get(ConversionHistory, pid) is None
        assert ConversionHistory.query.filter_by(parent_id=pid).count() == 0


def test_delete_playlist_blocked_while_active(client, tmp_path):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path=str(tmp_path), status='downloading',
                                   is_playlist=True, item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/1', format='FLAC',
            output_path=str(tmp_path), status='downloading',
            parent_id=pid, item_index=0))
        db.session.commit()

    resp = client.post(f'/api/delete-playlist/{pid}')
    assert resp.status_code == 400


def test_history_pagination(client):
    from app.models import ConversionHistory as CH
    with client.application.app_context():
        for i in range(25):
            db.session.add(CH(url=f'https://youtu.be/p{i}', format='FLAC',
                              output_path=f'/tmp/p{i}', status='completed'))
        db.session.commit()

    page1 = client.get('/history?page=1&per=10')
    assert page1.status_code == 200
    assert 'page 1 of 3' in page1.data.decode('utf-8')

    page3 = client.get('/history?page=3&per=10')
    assert 'page 3 of 3' in page3.data.decode('utf-8')

    clamped = client.get('/history?page=99&per=10')
    assert 'page 3 of 3' in clamped.data.decode('utf-8')


def test_api_search_finds_unexpanded_child(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, playlist_title='Mix',
                                   item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/obscure', format='FLAC',
            output_path='/tmp/p/Zebra Crossing Anthem.flac',
            status='completed', parent_id=pid, item_index=0))
        db.session.commit()

    data = client.get('/api/search', query_string={'q': 'zebra'}).get_json()
    assert data['ok'] is True
    assert len(data['items']) == 1
    assert data['items'][0]['parent_id'] == pid
    assert data['items'][0]['filename'] == 'Zebra Crossing Anthem.flac'


def test_api_search_min_length(client):
    assert client.get('/api/search', query_string={'q': 'x'}).get_json() == {
        'ok': True, 'query': 'x', 'items': []}


# ------------------------------------------------------- covers+m3u ----

def test_ogg_conversion_writes_cover_sidecar(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, 'extract_metadata', lambda url: {
        'title': 'Song', 'artist': 'A', 'duration': 120, 'thumbnail': 'http://x/y.jpg'})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: False)
    monkeypatch.setattr(convert_module, 'check_ffmpeg', lambda: True)

    cover = tmp_path / 'art.jpg'
    cover.write_bytes(b'fakejpeg')

    def fake_download(url, temp_audio, job=None):
        open(temp_audio, 'wb').write(b'data')
        return {'success': True}

    monkeypatch.setattr(convert_module, 'download_audio', fake_download)
    monkeypatch.setattr(convert_module, '_fetch_thumbnail', lambda url: str(cover))
    monkeypatch.setattr(convert_module, 'convert_audio_file',
                        lambda *a: {'success': True})
    monkeypatch.setattr(convert_module, '_verify_output',
                        lambda *a, **k: (True, 'File verified'))

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='OGG Vorbis',
                                output_path=str(tmp_path), status='downloading')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'ogg_vorbis', str(tmp_path), job)

    assert result['success'] is True
    assert os.path.isfile(os.path.splitext(result['filepath'])[0] + '.cover.jpg')


def test_cover_falls_back_to_sidecar(client, tmp_path):
    track = tmp_path / 'song.ogg'
    track.write_bytes(b'not really audio but present')
    sidecar = tmp_path / 'song.cover.jpg'
    sidecar.write_bytes(b'fakejpeg')
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='OGG Vorbis',
                                output_path=str(track), status='completed',
                                progress=100)
        db.session.add(job)
        db.session.commit()
        job_id = job.id

    resp = client.get(f'/api/cover/{job_id}')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'image/jpeg'
    assert resp.data == b'fakejpeg'


def test_playlist_m3u_download(client, tmp_path):
    one = tmp_path / 'one.flac'
    one.write_bytes(b'fLaC')
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path=str(tmp_path), status='completed',
                                   is_playlist=True, playlist_title='Mix & Match',
                                   item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add_all([
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path=str(one), status='completed',
                              parent_id=pid, item_index=0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path=str(tmp_path / 'missing.flac'),
                              status='pending', parent_id=pid, item_index=1),
        ])
        db.session.commit()

    resp = client.get(f'/api/playlist/{pid}/m3u')
    assert resp.status_code == 200
    body = resp.data.decode('utf-8')
    assert body.startswith('#EXTM3U\n')
    assert str(one) in body
    assert 'missing.flac' not in body
    assert 'Mix & Match.m3u' in resp.headers['Content-Disposition']


def test_playlist_m3u_empty_returns_404(client):
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, item_count=0)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id

    assert client.get(f'/api/playlist/{pid}/m3u').status_code == 404


def test_player_has_option_controls(client):
    page = client.get('/')
    for marker in (b'id="appPlayerSpeed"', b'id="appPlayerSleep"',
                   b'id="appPlayerEQBtn"', b'id="appPlayerEQBox"',
                   b'app-player-eq-slider', b'id="appPlayerShuffle"',
                   b'id="appPlayerRepeat"', b'id="appPlayerCover"'):
        assert marker in page.data


# ------------------------------------------------------- performance ----

def test_job_progress_throttles_commits(client, monkeypatch):
    calls = []
    orig = convert_module._job_update
    monkeypatch.setattr(convert_module, '_job_update',
                        lambda job, **kw: (calls.append(kw.get('progress')), orig(job, **kw)))

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path='/tmp/x', status='downloading')
        db.session.add(job)
        db.session.commit()

        for _ in range(10):
            convert_module._job_progress(job, 42)
        assert calls == [42]

        convert_module._job_progress(job, 44)
        assert calls == [42, 44]


def test_stat_cache_ttl_and_invalidate(tmp_path):
    target = tmp_path / 'f.flac'
    target.write_bytes(b'x')

    path = str(target)
    assert convert_module._cached_stat(path) == (True, 1)
    target.unlink()
    # Still cached as existing until invalidated or the TTL passes.
    assert convert_module._cached_stat(path)[0] is True
    assert convert_module._cached_stat(path, ttl=0)[0] is False
    convert_module._invalidate_stat(path)


def test_storage_cache_invalidation(client, tmp_path):
    one = tmp_path / 'one.flac'
    one.write_bytes(b'12345')
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(one), status='completed',
                                progress=100)
        db.session.add(job)
        db.session.commit()

        first = client.get('/api/storage').get_json()
        assert first['bytes'] == 5

        one.unlink()
        # Cached total is stale-but-fast until something invalidates it.
        assert client.get('/api/storage').get_json()['bytes'] == 5

        convert_module._invalidate_storage()
        assert client.get('/api/storage').get_json()['bytes'] == 0


def test_schema_indexes_created(client):
    from sqlalchemy import inspect
    from app import db
    with client.application.app_context():
        from app import _setup_db
        _setup_db()
        names = {i['name'] for i in inspect(db.engine).get_indexes('conversion_history')}
    for column in ('status', 'parent_id', 'created_at', 'url'):
        assert f'idx_conversion_history_{column}' in names


def test_convert_audio_file_with_thread_cap(tmp_path):
    import subprocess as _sp
    src = tmp_path / 'in.wav'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=1', '-c:a', 'pcm_s16le',
             str(src)], check=True)
    out = str(tmp_path / 'out.flac')
    result = convert_module.convert_audio_file(
        str(src), 'flac', out, {'title': 'T'}, None)
    assert result == {'success': True}
    assert convert_module._is_valid_audio(out)


# ------------------------------------------------------- perf fixes ----

def test_m3u_probes_each_file_once(client, tmp_path, monkeypatch):
    calls = []
    one = tmp_path / 'one.flac'
    one.write_bytes(b'fLaC')
    two = tmp_path / 'two.flac'
    two.write_bytes(b'fLaC')

    def fake_info(path):
        calls.append(path)
        return 120.0, {'title': 'T', 'artist': 'A', 'album': '', 'date': ''}, ''

    monkeypatch.setattr(convert_module, '_ffmpeg_file_info', fake_info)
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path=str(tmp_path), status='completed',
                                   is_playlist=True, playlist_title='Mix',
                                   item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add_all([
            ConversionHistory(url='https://youtu.be/1', format='FLAC',
                              output_path=str(one), status='completed',
                              parent_id=pid, item_index=0,
                              duration=120.0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path=str(two), status='completed',
                              parent_id=pid, item_index=1,
                              duration=120.0),
        ])
        db.session.commit()

    resp = client.get(f'/api/playlist/{pid}/m3u')
    assert resp.status_code == 200
    # Stored durations: exactly one probe per file (tags only), never two.
    assert sorted(calls) == sorted([str(one), str(two)])


def test_track_meta_cached_until_rewrite(client, completed_conversion, monkeypatch):
    job_id, path = completed_conversion
    calls = []
    orig = convert_module._probe_metadata
    monkeypatch.setattr(convert_module, '_probe_metadata',
                        lambda p: (calls.append(p), orig(p))[1])

    assert client.get(f'/api/track/{job_id}').status_code == 200
    assert client.get(f'/api/track/{job_id}').status_code == 200
    assert len(calls) == 1

    # Touching the file (new mtime) re-probes on the next read.
    import time as _time
    _time.sleep(0.05)
    os.utime(path, None)
    assert client.get(f'/api/track/{job_id}').status_code == 200
    assert len(calls) == 2


def test_background_verify_lifecycle(client, tmp_path):
    gone = tmp_path / 'gone.flac'  # missing file: fails fast, no decoding
    with client.application.app_context():
        for i in range(3):
            db.session.add(ConversionHistory(
                url=f'https://youtu.be/v{i}', format='FLAC',
                output_path=str(gone), status='completed', progress=100))
        db.session.commit()

    started = client.post('/api/verify-files').get_json()
    assert started['ok'] is True
    assert started['started'] is True

    import time as _time
    deadline = _time.time() + 15
    state = {}
    while _time.time() < deadline:
        state = client.get('/api/verify-status').get_json()
        if state.get('done'):
            break
        _time.sleep(0.2)
    assert state.get('done') is True
    assert state.get('checked') == 3

    with client.application.app_context():
        pending = ConversionHistory.query.filter_by(status='pending').count()
        assert pending == 3


# ------------------------------------------------------- desktop ----
# NOTE: desktop.py is imported as top-level module `desktop`, not `app.*`.
# These tests load it by path so the launcher stays covered.

def _load_desktop():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), '..', 'src', 'desktop.py')
    spec = importlib.util.spec_from_file_location('desktop_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_find_free_port_prefers_stable_default():
    import socket
    desktop = _load_desktop()

    def port_taken():
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((desktop.DEFAULT_HOST, desktop.DEFAULT_PORT))
            return False
        except OSError:
            return True
        finally:
            probe.close()

    taken = port_taken()
    port = desktop.find_free_port(preferred=desktop.DEFAULT_PORT)
    assert (port != desktop.DEFAULT_PORT) if taken else (port == desktop.DEFAULT_PORT)


def test_find_free_port_falls_back_when_taken():
    import socket
    desktop = _load_desktop()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind((desktop.DEFAULT_HOST, 0))
        taken = holder.getsockname()[1]
        holder.listen(1)
        assert desktop.find_free_port(preferred=taken) != taken
    finally:
        holder.close()


def test_close_guard_enabled():
    with open(os.path.join(os.path.dirname(__file__), '..', 'src',
                           'desktop.py')) as fh:
        source = fh.read()
    # The window close behavior follows the Settings choice (ask by default).
    assert 'confirm_close=confirm_close' in source
    assert 'should_confirm_close(' in source
    assert '_close_behavior()' in source


# ------------------------------------------------------- notify ----

def _notify_settings(client, enabled):
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        settings.desktop_notifications = enabled
        db.session.commit()


def test_notify_darwin_uses_osascript(client, monkeypatch):
    import sys as _sys
    _notify_settings(client, True)
    calls = []
    monkeypatch.setattr(_sys, 'platform', 'darwin')
    monkeypatch.setattr(convert_module.subprocess, 'run',
                        lambda *a, **k: calls.append(a[0]))
    with client.application.app_context():
        convert_module._notify('Done', 'Song.flac')
    assert calls and calls[0][0] == 'osascript'
    assert 'Song.flac' in calls[0][-1]


def test_notify_respects_setting(client, monkeypatch):
    _notify_settings(client, False)
    calls = []
    monkeypatch.setattr(convert_module.subprocess, 'run',
                        lambda *a, **k: calls.append(a[0]))
    with client.application.app_context():
        convert_module._notify('Done', 'Song.flac')
    assert calls == []


def test_parent_completion_notifies_once(client, monkeypatch):
    _notify_settings(client, True)
    notes = []
    monkeypatch.setattr(convert_module, '_notify',
                        lambda t, m: notes.append((t, m)))
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path='/tmp/p', status='downloading',
                                   is_playlist=True, playlist_title='Mix',
                                   item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/1', format='FLAC',
            output_path='/tmp/p/1.flac', status='completed',
            parent_id=pid, item_index=0))
        db.session.commit()

        convert_module._refresh_playlist_parent(pid)
        assert len(notes) == 1
        assert 'Mix' in notes[0][1]
        # Re-refreshing must not notify again.
        convert_module._refresh_playlist_parent(pid)
        assert len(notes) == 1


# ------------------------------------------------------- backup ----

def test_backup_download_is_valid_sqlite(client, tmp_path):
    import sqlite3
    resp = client.get('/api/backup')
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'].startswith('attachment')
    path = str(tmp_path / 'backup-test.db')
    with open(path, 'wb') as f:
        f.write(resp.data)
    try:
        tables = [r[0] for r in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        assert 'conversion_history' in tables
    finally:
        os.unlink(path)


def test_auto_backup_before_migration(tmp_path, monkeypatch):
    import sqlite3
    import sys as _sys
    target = tmp_path / 'old.db'
    conn = sqlite3.connect(str(target))
    conn.execute('CREATE TABLE conversion_history (id INTEGER PRIMARY KEY, url TEXT)')
    conn.commit()
    conn.close()

    # Snapshot module state: importing a second app object below must not
    # leak into the rest of the suite.
    saved = {k: v for k, v in _sys.modules.items()
             if k == 'app' or k.startswith('app.')}
    try:
        monkeypatch.setenv('AUDIO_CONVERTER_DB_PATH', str(target))
        monkeypatch.setenv('AUDIO_CONVERTER_SECRET_KEY', 'backup-test')
        for module in [m for m in list(_sys.modules) if m == 'app' or m.startswith('app.')]:
            del _sys.modules[module]
        import importlib
        sys_path = os.path.join(os.path.dirname(__file__), '..', 'src')
        if sys_path not in _sys.path:
            _sys.path.insert(0, sys_path)
        fresh = importlib.import_module('app')
        with fresh.app.app_context():
            fresh._setup_db()
            cols = {c['name'] for c in
                    __import__('sqlalchemy').inspect(fresh.db.engine).get_columns('conversion_history')}
        assert 'is_playlist' in cols
        backups = list((tmp_path / 'backups').glob('*.db'))
        assert len(backups) == 1
    finally:
        try:
            fresh.db.session.remove()
            fresh.db.engine.dispose()
        except Exception:
            pass
        for module in [m for m in list(_sys.modules) if m == 'app' or m.startswith('app.')]:
            del _sys.modules[module]
        _sys.modules.update(saved)


# ------------------------------------------------------- close ----

def test_should_confirm_close():
    desktop = _load_desktop()
    assert desktop.should_confirm_close('ask') is True
    assert desktop.should_confirm_close('quit') is False
    assert desktop.should_confirm_close('anything-else') is True


def test_settings_close_behavior_round_trip(client):
    resp = client.post('/settings', data={
        'output_path': '/tmp/x',
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'retry_count': '3',
        'job_timeout': '300',
        'bandwidth_limit': '0',
        'worker_count': '3',
        'close_behavior': 'quit',
    })
    assert resp.status_code == 302

    from app.models import UserSettings
    with client.application.app_context():
        assert UserSettings.query.first().close_behavior == 'quit'

    # Invalid values fall back to asking, never to silent quitting.
    client.post('/settings', data={
        'output_path': '/tmp/x',
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'close_behavior': 'nuke-everything',
    })
    with client.application.app_context():
        assert UserSettings.query.first().close_behavior == 'ask'


def test_settings_shows_desktop_card(client):
    page = client.get('/settings')
    for marker in (b'desktop_notifications', b'close_behavior',
                   b'api/backup', b'Library backup'):
        assert marker in page.data


# ------------------------------------------------------- tray ----

def _tray_module():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), '..', 'src', 'tray_icon.py')
    spec = importlib.util.spec_from_file_location('tray_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tray_assets_exist():
    base = os.path.join(os.path.dirname(__file__), '..', 'src', 'static', 'img')
    for name in ('tray.png', 'tray.ico', 'tray.icns'):
        assert os.path.isfile(os.path.join(base, name)), name


def test_tray_icon_image_loads():
    tray = _tray_module()
    image = tray.load_icon_image()
    assert image is not None
    assert image.size[0] >= 64
    assert tray.load_icon_image('/does/not/exist.png') is None


def test_tray_menu_spec_labels():
    tray = _tray_module()
    paused = [e['label'] for e in tray.menu_spec(True)]
    assert paused[0] == 'Resume downloads'
    unpaused = [e['label'] for e in tray.menu_spec(False)]
    assert unpaused[0] == 'Pause downloads'
    assert [e['action'] for e in tray.menu_spec(False)] == [
        'toggle_pause', 'open_folder', 'quit']


def test_tray_toggle_pause_flips_event():
    tray = _tray_module()
    from app.routes.convert import _queue_paused
    initial = _queue_paused.is_set()
    try:
        assert tray.toggle_pause() is (not initial)
        assert _queue_paused.is_set() is (not initial)
        assert tray.toggle_pause() is initial
        assert _queue_paused.is_set() is initial
        assert tray.is_paused() is initial
    finally:
        if _queue_paused.is_set() != initial:
            _queue_paused.clear() if not initial else _queue_paused.set()


def test_tray_open_output_folder(client, tmp_path, monkeypatch):
    tray = _tray_module()
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path=str(tmp_path))
            db.session.add(settings)
        else:
            settings.output_path = str(tmp_path)
        db.session.commit()

        calls = []
        monkeypatch.setattr(tray.subprocess, 'Popen',
                            lambda cmd, **kw: calls.append(cmd))
        assert tray.open_output_folder() is True
        assert calls and calls[0][-1] == str(tmp_path)

        settings.output_path = str(tmp_path / 'nope')
        db.session.commit()
        assert tray.open_output_folder() is False


def test_tray_quit_paths(monkeypatch):
    tray = _tray_module()
    destroyed = []
    assert tray.quit_app(lambda: type('W', (), {
        'destroy': staticmethod(lambda: destroyed.append(True))})()) == 'destroy'
    assert destroyed == [True]

    exited = []
    monkeypatch.setattr(tray.os, '_exit', lambda code: exited.append(code))
    tray.quit_app(lambda: (_ for _ in ()).throw(RuntimeError('nope')))
    assert exited == [0]
    # Restore os._exit automatically via monkeypatch teardown.


def test_tray_build_menu_with_fake_pystray():
    tray = _tray_module()

    class FakeItem:
        def __init__(self, text, action):
            self.text = text
            self.action = action

    class FakeMenu(list):
        def __init__(self, *items):
            super().__init__(items)

    class FakePyStray:
        MenuItem = FakeItem
        Menu = FakeMenu

    calls = []
    menu = tray.build_menu(FakePyStray, {
        'is_paused': lambda: False,
        'toggle_pause': lambda icon, item: calls.append('pause'),
        'open_folder': lambda icon, item: calls.append('folder'),
        'quit': lambda icon, item: calls.append('quit'),
    })
    assert len(menu) == 3
    assert menu[0].text(menu[0]) == 'Pause downloads'
    menu[1].action(None, None)
    assert calls == ['folder']


def test_tray_start_returns_none_without_backend(monkeypatch):
    import sys as _sys
    tray = _tray_module()
    monkeypatch.setitem(_sys.modules, 'pystray', None)
    assert tray.start_tray(lambda: None) is None


def test_tray_start_happy_path():
    tray = _tray_module()

    class FakeIcon:
        def __init__(self, *args):
            self.args = args
            self.detached = False

        def run_detached(self):
            self.detached = True

    class FakePyStray:
        MenuItem = lambda *a: a
        Menu = lambda *a: list(a)
        Icon = FakeIcon

    icon = tray.start_tray(lambda: None, pystray_module=FakePyStray)
    assert icon is not None
    assert icon.detached is True
    assert icon.args[0] == 'AudioConverter'


def test_settings_tray_round_trip(client):
    resp = client.post('/settings', data={
        'output_path': '/tmp/x',
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
    })
    assert resp.status_code == 302

    from app.models import UserSettings
    with client.application.app_context():
        # Checkbox omitted -> tray icon off.
        assert UserSettings.query.first().tray_icon is False
    assert b'id="tray_icon"' in client.get('/settings').data


# ------------------------------------------------------- subscriptions ----

def _make_parent(client, **overrides):
    from app.models import Subscription
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://www.youtube.com/playlist?list=PLx',
            format='FLAC', output_path='/tmp/sub', status='completed',
            is_playlist=True, playlist_title='Sub Mix', item_count=1,
            **overrides)
        db.session.add(parent)
        db.session.commit()
        return parent.id


def test_subscribe_creates_and_links(client):
    from app.models import Subscription
    pid = _make_parent(client)
    resp = client.post(f'/api/subscribe/{pid}', json={'interval_hours': 12})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['ok'] is True

    with client.application.app_context():
        sub = Subscription.query.first()
        assert sub is not None
        assert sub.interval_hours == 12
        assert sub.active is True
        assert db.session.get(ConversionHistory, pid).subscription_id == sub.id


def test_subscribe_idempotent_and_validates_interval(client):
    from app.models import Subscription
    pid = _make_parent(client)
    client.post(f'/api/subscribe/{pid}', json={'interval_hours': 999})
    second = client.post(f'/api/subscribe/{pid}', json={}).get_json()
    assert second['ok'] is True

    with client.application.app_context():
        subs = Subscription.query.all()
        assert len(subs) == 1
        assert subs[0].interval_hours == 24


def test_subscription_toggle_delete_interval(client):
    from app.models import Subscription
    pid = _make_parent(client)
    sid = client.post(f'/api/subscribe/{pid}', json={}).get_json()['id']

    assert client.post(f'/api/subscriptions/{sid}/toggle').get_json() == {
        'ok': True, 'active': False}
    assert client.post(f'/api/subscriptions/{sid}/interval',
                       json={'interval_hours': 6}).get_json() == {
        'ok': True, 'interval_hours': 6}
    assert client.post(f'/api/subscriptions/{sid}/interval',
                       json={'interval_hours': 13}).get_json()['interval_hours'] == 24
    assert client.post(f'/api/subscriptions/{sid}/delete').get_json() == {
        'ok': True}
    with client.application.app_context():
        assert Subscription.query.count() == 0
        # History is kept.
        assert db.session.get(ConversionHistory, pid) is not None


def test_check_appends_only_new_tracks(client, tmp_path, monkeypatch):
    from app.models import Subscription
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    monkeypatch.setattr(convert_module, 'resolve_playlist', lambda url: {
        'success': True, 'title': 'Sub Mix',
        'urls': ['https://youtu.be/old', 'https://youtu.be/new']})

    with client.application.app_context():
        parent = ConversionHistory(
            url='https://www.youtube.com/playlist?list=PLx',
            format='FLAC', output_path=str(tmp_path), status='completed',
            is_playlist=True, playlist_title='Sub Mix', item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/old', format='FLAC',
            output_path=str(tmp_path / 'old.flac'), status='completed',
            parent_id=pid, item_index=0, item_count=1))
        sub = Subscription(url=parent.url, format='FLAC',
                           output_path=str(tmp_path), parent_id=pid)
        db.session.add(sub)
        db.session.commit()
        sid = sub.id

    resp = client.post(f'/api/subscriptions/{sid}/check')
    assert resp.status_code == 200
    assert resp.get_json()['added'] == 1

    with client.application.app_context():
        kids = ConversionHistory.query.filter_by(
            parent_id=pid).order_by(ConversionHistory.item_index).all()
        assert [k.url for k in kids] == ['https://youtu.be/old',
                                         'https://youtu.be/new']
        assert kids[1].item_index == 1
        assert kids[1].subscription_id == sid
        parent = db.session.get(ConversionHistory, pid)
        assert parent.item_count == 2
        assert parent.status == 'downloading'


def test_due_subscriptions_respects_interval_and_active(client):
    from app.models import Subscription
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    with client.application.app_context():
        db.session.add_all([
            Subscription(url='https://a', format='FLAC', output_path='/tmp/a',
                         active=True,
                         last_checked=now - timedelta(hours=30)),
            Subscription(url='https://b', format='FLAC', output_path='/tmp/b',
                         active=True, interval_hours=6,
                         last_checked=now - timedelta(hours=1)),
            Subscription(url='https://c', format='FLAC', output_path='/tmp/c',
                         active=False,
                         last_checked=now - timedelta(days=9)),
        ])
        db.session.commit()
        due = convert_module._due_subscriptions()
        assert [s.url for s in due] == ['https://a']


# ------------------------------------------------------- self-healing ----

@pytest.mark.parametrize('error,expected', [
    ('yt-dlp failed: signature extraction failed', True),
    ('HTTP Error 403: Forbidden', True),
    ('Video unavailable', False),
    ('Not enough free disk space', False),
    ('', False),
])
def test_looks_stale(error, expected):
    assert convert_module._looks_stale(error) is expected


def test_stale_flag_and_health(client, monkeypatch):
    assert client.get('/api/health').get_json()['stale_helper_suspected'] is False
    out = convert_module._note_stale_helper('signature extraction failed badly')
    assert 'Settings' in out
    assert client.get('/api/health').get_json()['stale_helper_suspected'] is True
    convert_module._stale_helper_event.clear()


# ------------------------------------------------------- stats ----

def test_api_stats_counts(client, tmp_path):
    import subprocess as _sp
    two = tmp_path / 'two.flac'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=2', '-c:a', 'flac',
             str(two)], check=True)
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/1', format='FLAC', output_path=str(two),
            status='completed', progress=100))
        db.session.add(ConversionHistory(
            url='https://youtu.be/2', format='WAV', output_path='/tmp/gone.wav',
            status='failed'))
        db.session.commit()

    data = client.get('/api/stats').get_json()
    assert data['ok'] is True
    assert data['tracks'] == 1
    assert data['files'] == 1
    assert data['by_status']['completed'] == 1
    assert data['by_status']['failed'] == 1
    assert data['by_format']['FLAC'] == 1
    assert data['playtime_tracks'] == 1
    assert data['recent_7d'] == 2


# ------------------------------------------------------- updater ----

def test_update_install_blocked_when_not_frozen(client):
    resp = client.post('/api/update-install')
    assert resp.status_code == 400
    assert 'Applications' in resp.get_json()['message']


def test_find_asset_by_suffix():
    release = {'assets': [
        {'name': 'notes.txt', 'browser_download_url': 'https://x/notes'},
        {'name': 'AudioConverter-1.0.0.dmg',
         'browser_download_url': 'https://x/app.dmg'},
        {'name': 'AudioConverter-Setup-1.0.0.exe',
         'browser_download_url': 'https://x/setup.exe'},
    ]}
    assert convert_module._find_asset_by_suffix(release, ('.dmg',)) == (
        'AudioConverter-1.0.0.dmg', 'https://x/app.dmg')
    assert convert_module._find_asset_by_suffix(release, ('.exe',)) == (
        'AudioConverter-Setup-1.0.0.exe', 'https://x/setup.exe')
    assert convert_module._find_asset_by_suffix({}, ('.dmg',)) == (None, None)


def test_running_bundle_dir_unfrozen():
    assert convert_module._running_bundle_dir() is None


def test_home_has_subs_stats_health_markers(client):
    page = _home_with_history(client)
    for marker in (b'id="subsList"', b'id="statsBody"',
                   b'id="healthBanner"'):
        assert marker in page.data

    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/list', format='FLAC',
            output_path='/tmp/p', status='completed',
            is_playlist=True, playlist_title='Mix', item_count=1))
        db.session.commit()
    assert b'data-subscribe' in client.get('/').data


# ------------------------------------------------------- player tab ----

def test_player_page_renders(client):
    page = client.get('/player')
    assert page.status_code == 200
    for marker in (b'id="libGrid"', b'id="viz"', b'id="tabQueue"',
                   b'id="tabPlaylists"', b'player_tab.js',
                   b'href="/player">Player</a>'):
        assert marker in page.data


def test_like_toggle(client, completed_conversion):
    job_id, _ = completed_conversion
    assert client.post(f'/api/like/{job_id}').get_json() == {
        'ok': True, 'liked': True}
    assert client.post(f'/api/like/{job_id}').get_json() == {
        'ok': True, 'liked': False}
    assert client.post('/api/like/999999').status_code == 404


def test_record_played(client, completed_conversion):
    job_id, _ = completed_conversion
    assert client.post(f'/api/played/{job_id}').get_json() == {
        'ok': True, 'play_count': 1}
    assert client.post(f'/api/played/{job_id}').get_json() == {
        'ok': True, 'play_count': 2}
    assert client.post('/api/played/999999').status_code == 404


def test_library_lists_playable_only(client, tmp_path):
    target = tmp_path / 'song.flac'
    target.write_bytes(b'fLaC')
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/ok', format='FLAC',
            output_path=str(target), status='completed'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/gone', format='FLAC',
            output_path=str(tmp_path / 'gone.flac'), status='completed'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/busy', format='FLAC',
            output_path=str(tmp_path), status='downloading'))
        db.session.commit()

    items = client.get('/api/library').get_json()['items']
    assert [i['url'] for i in items] == ['https://youtu.be/ok']


def test_user_playlist_crud(client, completed_conversion):
    job_id, _ = completed_conversion
    created = client.post('/api/playlists', json={'name': 'Gym'}).get_json()
    assert created['ok'] is True
    pid = created['playlist']['id']

    assert client.post('/api/playlists', json={'name': '  '}).status_code == 400
    assert client.post(f'/api/playlists/{pid}/add',
                       json={'conversion_id': 999999}).status_code == 400

    added = client.post(f'/api/playlists/{pid}/add',
                        json={'conversion_id': job_id}).get_json()
    assert added['ok'] is True
    assert added['playlist']['count'] == 1

    items = client.get(f'/api/playlists/{pid}').get_json()
    assert len(items['items']) == 1
    entry = items['items'][0]
    assert entry['id'] == job_id

    listed = client.get('/api/playlists').get_json()['items']
    assert listed[0]['name'] == 'Gym'

    item_id = items['items'][0]['item_id']
    assert client.post(f'/api/playlists/{pid}/move',
                       json={'item_id': item_id, 'direction': 'up'}
                       ).status_code == 400
    assert client.post(f'/api/playlists/{pid}/remove',
                       json={'item_id': item_id}).get_json()['ok'] is True
    assert client.post(f'/api/playlists/{pid}/delete').get_json() == {
        'ok': True}


def test_user_playlist_move_reorders(client, completed_conversion, tmp_path):
    job_id, _ = completed_conversion
    other = tmp_path / 'other.flac'
    other.write_bytes(b'fLaC')
    with client.application.app_context():
        job2 = ConversionHistory(url='https://youtu.be/b', format='FLAC',
                                 output_path=str(other), status='completed')
        db.session.add(job2)
        db.session.commit()
        job2_id = job2.id

    pid = client.post('/api/playlists', json={'name': 'Mix'}).get_json(
    )['playlist']['id']
    client.post(f'/api/playlists/{pid}/add', json={'conversion_id': job_id})
    client.post(f'/api/playlists/{pid}/add', json={'conversion_id': job2_id})

    def order():
        return [t['id'] for t in
                client.get(f'/api/playlists/{pid}').get_json()['items']]

    assert order() == [job_id, job2_id]
    item_id = client.get(f'/api/playlists/{pid}').get_json()['items'][1]['item_id']
    client.post(f'/api/playlists/{pid}/move',
                json={'item_id': item_id, 'direction': 'up'})
    assert order() == [job2_id, job_id]


def test_serialize_reports_likes_and_plays(client, completed_conversion):
    job_id, _ = completed_conversion
    client.post(f'/api/like/{job_id}')
    client.post(f'/api/played/{job_id}')
    item = client.get('/api/conversions', query_string={'limit': 10}).get_json()
    row = next(i for i in item if i['id'] == job_id)
    assert row['liked'] is True
    assert row['play_count'] == 1


# ------------------------------------------------------- durations ----

def test_completion_stores_duration(client, tmp_path, monkeypatch):
    import subprocess as _sp
    src = tmp_path / 'tone.wav'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=4', '-c:a', 'pcm_s16le',
             str(src)], check=True)

    monkeypatch.setattr(convert_module, 'extract_metadata', lambda url: {
        'title': 'Tone', 'duration': 4.0, 'thumbnail': ''})
    monkeypatch.setattr(convert_module, '_should_skip_duplicates', lambda: False)
    monkeypatch.setattr(convert_module, 'check_ffmpeg', lambda: True)
    monkeypatch.setattr(convert_module, '_fetch_thumbnail', lambda url: None)

    def fake_download(url, temp_audio, job=None):
        import shutil as _sh
        _sh.copyfile(str(src), temp_audio)
        return {'success': True}

    monkeypatch.setattr(convert_module, 'download_audio', fake_download)

    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(tmp_path), status='downloading')
        db.session.add(job)
        db.session.commit()

        result = convert_module.download_and_convert(
            job.url, 'flac', str(tmp_path), job)

    assert result['success'] is True
    with client.application.app_context():
        job = db.session.get(ConversionHistory, job.id)
        # Source-stated duration is remembered for the finished-file check.
        assert 3.5 < job.expected_duration <= 4.5


def test_stored_duration_backfills_old_rows(client, tmp_path):
    import subprocess as _sp
    target = tmp_path / 'old.flac'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=6', '-c:a', 'flac',
             str(target)], check=True)
    with client.application.app_context():
        job = ConversionHistory(url='https://youtu.be/x', format='FLAC',
                                output_path=str(target), status='completed',
                                duration=0)
        db.session.add(job)
        db.session.commit()

        assert convert_module._stored_duration(job) == 6.0
        assert db.session.get(ConversionHistory, job.id).duration == 6.0
        # Second read uses the stored value (no re-probe needed).
        assert convert_module._stored_duration(job) == 6.0


def test_m3u_uses_stored_duration_without_probing(client, tmp_path, monkeypatch):
    target = tmp_path / 'song.flac'
    target.write_bytes(b'fLaC')
    calls = []
    monkeypatch.setattr(convert_module, '_ffmpeg_file_info',
                        lambda p: (calls.append(p), (120.0, {}, ''))[1])
    with client.application.app_context():
        parent = ConversionHistory(url='https://youtu.be/list', format='FLAC',
                                   output_path=str(tmp_path), status='completed',
                                   is_playlist=True, playlist_title='Mix',
                                   item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/1', format='FLAC',
            output_path=str(target), status='completed',
            parent_id=pid, item_index=0, duration=120.0))
        db.session.commit()

    resp = client.get(f'/api/playlist/{pid}/m3u')
    assert resp.status_code == 200
    assert '#EXTINF:120,' in resp.data.decode('utf-8')
    # Stored duration used; the single probe left is the tags fetch.
    assert calls == [str(target)]


def test_cover_thumb_variant(client, completed_conversion):
    job_id, _ = completed_conversion
    resp = client.get(f'/api/cover/{job_id}?size=thumb')
    assert resp.status_code in (200, 404)


# ------------------------------------------------------- find dupes ----

def test_api_duplicates_groups_folders(client, tmp_path):
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    (a / 'Same Song.flac').write_bytes(b'x')
    (b / 'Same Song.flac').write_bytes(b'x')
    (a / 'Unique.flac').write_bytes(b'x')
    with client.application.app_context():
        for path in (a / 'Same Song.flac', b / 'Same Song.flac', a / 'Unique.flac'):
            db.session.add(ConversionHistory(
                url='https://youtu.be/x', format='FLAC',
                output_path=str(path), status='completed'))
        db.session.commit()

    data = client.get('/api/duplicates').get_json()
    assert data['ok'] is True
    assert len(data['groups']) == 1
    assert len(data['groups'][0]['items']) == 2


def test_api_recently_played(client, completed_conversion):
    job_id, _ = completed_conversion
    client.post(f'/api/played/{job_id}')
    items = client.get('/api/recently-played').get_json()['items']
    assert [i['id'] for i in items] == [job_id]


def test_api_library_csv(client, completed_conversion):
    job_id, path = completed_conversion
    resp = client.get('/api/library.csv')
    assert resp.status_code == 200
    assert 'attachment' in resp.headers['Content-Disposition']
    text = resp.data.decode('utf-8')
    assert text.splitlines()[0].startswith('filename,format,')
    assert os.path.basename(path) in text


def test_player_tab_has_new_sections(client):
    page = client.get('/player')
    for marker in (b'id="tabRecent"', b'id="tabDupes"', b'library.csv'):
        assert marker in page.data


# ------------------------------------------------------- video tab ----

def test_video_page_video_only(client):
    page = client.get('/video')
    assert page.status_code == 200
    html = page.data.decode('utf-8')
    for value in ('video_mp4', 'video_webm', 'video_mkv'):
        assert f'value="{value}"' in html
    for value in ('value="flac"', 'value="alac"', 'value="wav"',
                  'value="ogg_vorbis"'):
        assert value not in html


def test_audio_forms_have_no_video_options(client):
    html = client.get('/').data.decode('utf-8')
    for value in ('video_mp4', 'video_webm', 'video_mkv'):
        assert value not in html


def test_nav_has_video_tab(client):
    for path in ('/', '/video', '/player', '/settings'):
        assert 'href="/video"' in client.get(path).data.decode('utf-8')


def test_home_form_double_submit_guard(client):
    html = client.get('/').data.decode('utf-8')
    assert 'id="homeConvertForm"' in html
    assert 'Queued... checking status below' in html


# ------------------------------------------------------- disk full ----

@pytest.mark.parametrize('error,expected', [
    ('ffmpeg failed: No space left on device (os error 28)', True),
    ('write error: disk quota exceeded', True),
    ('ENOSPC while merging', True),
    ('yt-dlp download failed', False),
    ('', False),
])
def test_looks_like_nospace(error, expected):
    assert convert_module._looks_like_nospace(error) is expected


def test_failure_plan_disk_never_retries():
    action, attempts, message = convert_module._failure_plan(
        0, 3, 'No space left on device')
    assert action == 'fail'
    assert attempts == 0
    assert 'free space' in message


def test_failure_plan_retry_then_give_up():
    action, attempts, message = convert_module._failure_plan(
        0, 3, 'yt-dlp download failed')
    assert (action, attempts) == ('retry', 1)
    assert 'Retry 1/3' in message
    action, _, message = convert_module._failure_plan(
        3, 3, 'yt-dlp download failed')
    assert action == 'fail'
    assert 'Max retries' in message


# ------------------------------------------------------- diagnostics ----

def test_diagnostics_redacts_secrets(client):
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        settings.proxy = 'socks5://user:pass@127.0.0.1:9050'
        settings.tidal_client_secret = 'super-secret'
        settings.tidal_access_token = 'token-abc'
        db.session.commit()

    data = client.get('/api/diagnostics').get_json()
    assert data['ok'] is True
    assert data['settings']['proxy_configured'] is True
    assert data['settings']['tidal_connected'] is True
    blob = json.dumps(data)
    assert 'super-secret' not in blob
    assert 'token-abc' not in blob
    assert 'user:pass' not in blob
    assert 'app_version' in data
    assert 'workers_desired' in data['queue'] or 'workers' in str(data['queue'])


def test_player_tab_markers_present(client):
    assert client.get('/player').status_code == 200


# ------------------------------------------------------- video tab ----

def test_video_page_video_only(client):
    page = client.get('/video')
    assert page.status_code == 200
    html = page.data.decode('utf-8')
    for value in ('video_mp4', 'video_webm', 'video_mkv'):
        assert f'value="{value}"' in html
    for value in ('value="flac"', 'value="alac"', 'value="wav"',
                  'value="ogg_vorbis"'):
        assert value not in html


def test_theater_markup_present(client):
    for path in ('/', '/player'):
        html = client.get(path).data.decode('utf-8')
        for marker in ('id="appTheater"', 'id="appTheaterVideo"',
                       'id="appTheaterClose"'):
            assert marker in html


# ------------------------------------------------------- bulk UI ----

def test_history_has_bulk_controls(client):
    page = _home_with_history(client)
    for marker in (b'id="selectAll"', b'id="bulkBar"', b'id="bulkRetry"',
                   b'id="bulkDelete"', b'class="form-check-input row-select'):
        assert marker in page.data


# ------------------------------------------------------- failure plan ----

def test_failure_plan_branches():
    plan = convert_module._failure_plan
    assert plan(0, 3, 'yt-dlp download failed')[0] == 'retry'
    assert plan(0, 3, 'No space left on device')[0] == 'fail'
    assert plan(0, 3, 'Not enough free disk space')[0] == 'fail'
    action, attempts, message = plan(3, 3, 'boom')
    assert action == 'fail' and 'Max retries' in message


# ------------------------------------------------------- prune+health ----

def test_prune_missing_removes_only_gone_files(client, tmp_path):
    gone = tmp_path / 'gone.flac'
    here = tmp_path / 'here.flac'
    here.write_bytes(b'fLaC')
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/gone', format='FLAC',
            output_path=str(gone), status='completed'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/here', format='FLAC',
            output_path=str(here), status='completed'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/busy', format='FLAC',
            output_path=str(tmp_path), status='downloading'))
        parent = ConversionHistory(
            url='https://youtu.be/list', format='FLAC',
            output_path=str(tmp_path), status='completed',
            is_playlist=True, item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/gone2', format='FLAC',
            output_path=str(tmp_path / 'gone2.flac'), status='completed',
            parent_id=pid, item_index=0))
        db.session.commit()

    resp = client.post('/api/prune-missing')
    assert resp.status_code == 200
    # gone track + gone child + now-childless parent = 3.
    assert resp.get_json() == {'ok': True, 'removed': 3}

    with client.application.app_context():
        assert ConversionHistory.query.filter_by(
            url='https://youtu.be/here').count() == 1
        assert ConversionHistory.query.filter_by(
            url='https://youtu.be/busy').count() == 1
        assert db.session.get(ConversionHistory, pid) is None


def test_health_reports_helpers_and_output(client):
    data = client.get('/api/health').get_json()
    assert data['ok'] is True
    assert data['ffmpeg'] is True
    assert data['ytdlp'] is True
    assert data['output_writable'] is True
    assert 'stale_helper_suspected' in data
    assert data['disk_free_bytes'] is None or data['disk_free_bytes'] > 0


# ------------------------------------------------------- toggles ----

def test_settings_media_toggles_round_trip(client):
    resp = client.post('/settings', data={
        'output_path': '/tmp/x',
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'subtitles': 'on',
        'normalize_audio': 'on',
        # sponsorblock omitted -> off
    })
    assert resp.status_code == 302

    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        assert settings.subtitles is True
        assert settings.sponsorblock is False
        assert settings.normalize_audio is True

    page = client.get('/settings').data.decode('utf-8')
    for marker in ('name="subtitles"', 'name="sponsorblock"',
                   'name="normalize_audio"'):
        assert marker in page


# ------------------------------------------------------- tab UI ----

def test_player_tab_eq_presets_and_queue_export(client):
    page = client.get('/player').data.decode('utf-8')
    assert 'data-eq-preset' in page
    for preset in ('flat', 'rock', 'pop', 'jazz', 'vocal', 'bass'):
        assert f'data-eq-preset="{preset}"' in page


def test_sponsorblock_flags_present_when_on(client, monkeypatch):
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        settings.sponsorblock = True
        db.session.commit()

        seen = {}

        class FakeProc:
            returncode = 0

            def __init__(self):
                self.stdout = FakeStdout()

            def wait(self, timeout=None):
                return 0

        class FakeStdout:
            def readline(self):
                return ''

        def fake_popen(cmd, **kwargs):
            seen['cmd'] = cmd
            return FakeProc()

        monkeypatch.setattr(convert_module.subprocess, 'Popen', fake_popen)
        monkeypatch.setattr(convert_module, '_has_video_stream', lambda p: False)
        target = '/tmp/opencode-sponsor-test.tmp'
        open(target, 'wb').close()
        try:
            with client.application.app_context():
                video_job = ConversionHistory(
                    url='https://www.youtube.com/watch?v=x',
                    format='MP4 Video', output_path=target,
                    status='downloading')
                db.session.add(video_job)
                db.session.commit()
                job_id = video_job.id
                convert_module.download_audio(
                    'https://www.youtube.com/watch?v=x', target,
                    job=db.session.get(ConversionHistory, job_id))
        finally:
            if os.path.exists(target):
                os.unlink(target)
    assert '--sponsorblock-remove' in seen['cmd']


def test_normalize_adds_loudnorm_filter(client, monkeypatch, tmp_path):
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        settings.normalize_audio = True
        db.session.commit()

        seen = {}

        def fake_run(cmd, **kwargs):
            seen['cmd'] = cmd

            class FakeResult:
                returncode = 0
                stdout = ''
                stderr = ''
            return FakeResult()

        monkeypatch.setattr(convert_module.subprocess, 'run', fake_run)
        src = tmp_path / 'in.wav'
        src.write_bytes(b'x')
        convert_module.convert_audio_file(
            str(src), 'flac', str(tmp_path / 'out.flac'),
            {'title': 'T'}, None)
    assert '-filter:a' in seen['cmd']
    assert 'loudnorm' in seen['cmd']


def test_delete_keeps_file_shared_with_another_row(client, tmp_path):
    shared = tmp_path / 'shared.flac'
    shared.write_bytes(b'fLaC')
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/a1', format='FLAC',
            output_path=str(shared), status='completed'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/a2', format='FLAC',
            output_path=str(shared), status='skipped'))
        db.session.commit()
        first = ConversionHistory.query.filter_by(
            url='https://youtu.be/a1').first().id

    resp = client.post(f'/api/delete/{first}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['removed_file'] is False
    assert 'another entry' in data['message']
    assert shared.is_file()
    with client.application.app_context():
        assert ConversionHistory.query.filter_by(
            url='https://youtu.be/a2').count() == 1


def test_delete_playlist_keeps_externally_shared_file(client, tmp_path):
    shared = tmp_path / 'shared.flac'
    shared.write_bytes(b'fLaC')
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://youtu.be/pl', format='FLAC',
            output_path=str(tmp_path), status='completed',
            is_playlist=True, item_count=1)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/t1', format='FLAC',
            output_path=str(shared), status='completed',
            parent_id=pid, item_index=0))
        db.session.add(ConversionHistory(
            url='https://youtu.be/solo', format='FLAC',
            output_path=str(shared), status='completed'))
        db.session.commit()

    resp = client.post(f'/api/delete-playlist/{pid}')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['removed_files'] == 0
    assert data['kept_files'] == 1
    assert shared.is_file()


# ------------------------------------------------------- hardening ----

def test_supported_url_allowlist():
    good = [
        'https://www.youtube.com/watch?v=x',
        'https://youtu.be/x',
        'https://music.youtube.com/watch?v=x',
        'https://m.youtube.com/watch?v=x',
        'https://www.soundcloud.com/artist/track',
        'https://soundcloud.com/artist/track',
        'https://open.spotify.com/track/x',
        'https://open.spotify.com/playlist/x',
        'https://music.apple.com/us/album/x',
        'https://listen.tidal.com/album/x',
        'https://tidal.com/browse/track/x',
    ]
    bad = [
        'http://localhost:8080/x',
        'http://127.0.0.1/x',
        'http://169.254.169.254/latest/meta-data/',
        'http://192.168.1.1/x',
        'file:///etc/passwd',
        'ftp://example.com/x',
        'https://evil.com/watch?v=x',
        'https://youtube.com.evil.com/watch?v=x',
        'https://notyoutube.com/watch?v=x',
        'https://fakeyoutu.be/x',
        'not a url',
        '',
    ]
    for url in good:
        assert convert_module._is_supported_url(url), url
    for url in bad:
        assert not convert_module._is_supported_url(url), url


def test_convert_rejects_unsupported_url(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    for url in ('http://127.0.0.1:8080/evil',
                'https://evil.com/track',
                'file:///etc/passwd'):
        resp = client.post('/convert', data={
            'url': url, 'format': 'flac', 'output_path': '/tmp/out'})
        assert resp.status_code == 302
    with client.application.app_context():
        assert ConversionHistory.query.count() == 0


def test_thumbnail_fetch_blocks_private_targets(monkeypatch):
    assert not convert_module._is_public_http_url('http://127.0.0.1/x')
    assert not convert_module._is_public_http_url('http://localhost/x')
    assert not convert_module._is_public_http_url('http://169.254.169.254/x')
    assert not convert_module._is_public_http_url('http://10.0.0.1/x')
    assert not convert_module._is_public_http_url('file:///etc/passwd')
    assert not convert_module._is_public_http_url('ftp://example.com/x')
    assert not convert_module._is_public_http_url('')
    assert convert_module._is_public_http_url('https://www.youtube.com/')


def test_security_headers_and_cookie_flags(client):
    resp = client.get('/')
    assert resp.headers.get('X-Content-Type-Options') == 'nosniff'
    assert resp.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    assert resp.headers.get('Referrer-Policy') == 'no-referrer'
    assert client.application.config['SESSION_COOKIE_HTTPONLY'] is True
    assert client.application.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_ytdlp_release_uses_repos_api(monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {'tag_name': '2026.01.01', 'assets': []}

    def fake_get(url, **kwargs):
        seen['url'] = url
        return FakeResp()

    monkeypatch.setattr(convert_module.requests, 'get', fake_get)
    assert convert_module._latest_ytdlp_release() == {
        'version': '2026.01.01', 'assets': {}}
    assert seen['url'] == (
        'https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest')


def test_asset_sha256_parses_sums_file(monkeypatch):
    sums = ('aaaabbbbccccddddeeeeffff0000111122223333444455556666777788889999  yt-dlp_macos\n'
            'deadbeef  some-other-file\n'
            'not-a-hash  yt-dlp_linux\n')

    class FakeResp:
        status_code = 200
        text = sums

    class FakeSession:
        def __init__(self):
            self.cookies = FakeJar()

        def get(self, url, **kwargs):
            return FakeResp()

    class FakeJar:
        def clear(self):
            pass

    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    assets = {'SHA2-256SUMS': 'https://x/sums',
              'yt-dlp_macos': 'https://x/bin'}
    assert convert_module._asset_sha256(assets, 'yt-dlp_macos') == (
        'aaaabbbbccccddddeeeeffff0000111122223333444455556666777788889999')
    assert convert_module._asset_sha256(assets, 'yt-dlp_linux') is None
    assert convert_module._asset_sha256(assets, 'missing') is None
    assert convert_module._asset_sha256({}, 'yt-dlp_macos') is None


# ------------------------------------------------------- install verify ----

def test_verify_installer_bytes(monkeypatch):
    import hashlib
    content = b'fake-installer-bytes'
    digest = hashlib.sha256(content).hexdigest()
    release = {'assets': [
        {'name': 'AudioConverter-1.0.0.dmg',
         'browser_download_url': 'https://x/app.dmg'},
        {'name': 'SHA256SUMS.txt',
         'browser_download_url': 'https://x/sums'},
    ]}
    monkeypatch.setattr(
        convert_module, '_asset_sha256',
        lambda assets, name, sums_asset='SHA2-256SUMS', timeout=60: (
            digest if name == 'AudioConverter-1.0.0.dmg' else None))
    # Matching checksum passes silently.
    convert_module._verify_installer_bytes(
        release, 'AudioConverter-1.0.0.dmg', digest)
    # Tampered bytes raise.
    try:
        convert_module._verify_installer_bytes(
            release, 'AudioConverter-1.0.0.dmg', '0' * 64)
    except ValueError as e:
        assert 'mismatch' in str(e)
    else:
        raise AssertionError('tampered installer accepted')
    # Missing checksums entry refuses rather than skipping verification.
    try:
        convert_module._verify_installer_bytes(release, 'other.exe', digest)
    except ValueError as e:
        assert 'checksum' in str(e)
    else:
        raise AssertionError('unchecksummed installer accepted')


def test_stream_download_hashes_incrementally(tmp_path, monkeypatch):
    import types
    body = b'x' * (3 * 1024 * 1024)

    class FakeResp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size=1):
            for i in range(0, len(body), chunk_size):
                yield body[i:i + chunk_size]

    class FakeSession:
        def __init__(self):
            self.cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, url, **kwargs):
            assert kwargs.get('stream') is True
            return FakeResp()

    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    dest = str(tmp_path / 'big.bin')
    digest = convert_module._stream_download('https://x/big.bin', dest)
    import hashlib
    assert digest == hashlib.sha256(body).hexdigest()
    assert os.path.getsize(dest) == len(body)


# ------------------------------------------------------- logging ----

def test_setup_logging_rotates_and_captures(tmp_path):
    import logging
    sys.path.insert(0, 'src')
    try:
        from desktop import _setup_logging
    finally:
        sys.path.remove('src')
    log_path = _setup_logging(str(tmp_path))
    assert log_path is not None and log_path.endswith('app.log')
    try:
        logging.getLogger('test-harness').warning('hello-log-marker')
        with open(log_path, encoding='utf-8') as f:
            assert 'hello-log-marker' in f.read()
    finally:
        root = logging.getLogger()
        for h in [h for h in root.handlers
                  if getattr(h, 'baseFilename', '') == log_path]:
            root.removeHandler(h)
            h.close()


def test_diagnostics_redacts_log_credentials(client, tmp_path):
    from app.models import db as _db
    with client.application.app_context():
        db_path = _db.engine.url.database
        log_dir = os.path.join(os.path.dirname(db_path), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, 'app.log'), 'w',
                  encoding='utf-8') as f:
            f.write('proxy retry via https://user:s3cret@proxy:8080/x\n')
            f.write('plain line\n')
    bundle = client.get('/api/diagnostics').get_json()
    tail = bundle['recent_log']
    assert any('***@proxy' in line for line in tail)
    assert not any('s3cret' in line for line in tail)


def test_backup_uses_consistent_snapshot(tmp_path):
    import sqlite3
    from app import _backup_database
    src_path = str(tmp_path / 'config.db')
    con = sqlite3.connect(src_path)
    con.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
    con.execute("INSERT INTO t (v) VALUES ('one')")
    con.commit()
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("INSERT INTO t (v) VALUES ('two')")
    con.commit()
    con.close()

    dest = _backup_database(src_path, keep=2)
    assert dest and os.path.isfile(dest)
    check = sqlite3.connect(dest)
    try:
        rows = check.execute('SELECT v FROM t ORDER BY id').fetchall()
        assert rows == [('one',), ('two',)]
        assert check.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        check.close()


def test_helper_watch_starts_once():
    convert_module._ensure_helper_watch()
    convert_module._ensure_helper_watch()
    watchers = [t for t in __import__('threading').enumerate()
                if t.name == 'helper-watch']
    assert len(watchers) == 1
    assert convert_module._helper_watch_started is True


# ------------------------------------------------------- setup flow ----

def _setup_flow_fixture(tmp_path, binary=b'fake-setup-bytes', tamper=False):
    """Fake a release + network for _windows_update_install."""
    import hashlib
    import types
    digest = hashlib.sha256(binary).hexdigest()
    sums = f'{digest}  AudioConverter-Setup-9.9.9.exe\n'
    release = {'assets': [
        {'name': 'AudioConverter-Setup-9.9.9.exe',
         'browser_download_url': 'https://x/setup.exe'},
        {'name': 'SHA256SUMS.txt',
         'browser_download_url': 'https://x/SHA256SUMS.txt'},
    ]}

    class FeedResp:
        status_code = 200

        def json(self):
            return release

    def fake_get(url, **kwargs):
        assert url == 'https://feed/releases/latest'
        return FeedResp()

    def fake_stream(url, dest, timeout=600):
        with open(dest, 'wb') as f:
            f.write(b'tampered-bytes' if tamper else binary)
        import hashlib as _hl
        return _hl.sha256(b'tampered-bytes' if tamper else binary).hexdigest()

    class FakeSession:
        def __init__(self):
            self.cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, url, **kwargs):
            class SumsResp:
                status_code = 200
                text = sums
            return SumsResp()

    return release, fake_get, FakeSession, fake_stream


def test_windows_setup_flow_verifies_then_launches(client, tmp_path, monkeypatch):
    import types
    monkeypatch.setenv('HOME', str(tmp_path))
    release, fake_get, FakeSession, fake_stream = _setup_flow_fixture(tmp_path)
    monkeypatch.setattr(convert_module.requests, 'get', fake_get)
    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    monkeypatch.setattr(convert_module, '_stream_download', fake_stream)
    monkeypatch.setattr('app.routes.settings._default_update_feed',
                        lambda: 'https://feed/releases/latest')
    launched = []
    monkeypatch.setattr(convert_module.subprocess, 'Popen',
                        lambda cmd: launched.append(cmd) or types.SimpleNamespace(pid=1))

    with client.application.test_request_context():
        rv = convert_module._windows_update_install()
        resp, code = rv if isinstance(rv, tuple) else (rv, 200)
    assert code == 200
    assert resp.get_json()['ok'] is True
    assert len(launched) == 1
    dest = launched[0][0]
    assert dest.startswith(str(tmp_path))
    assert os.path.isfile(dest)


def test_windows_setup_flow_refuses_tampered_binary(client, tmp_path, monkeypatch):
    import types
    monkeypatch.setenv('HOME', str(tmp_path))
    release, fake_get, FakeSession, fake_stream = _setup_flow_fixture(tmp_path, tamper=True)
    monkeypatch.setattr(convert_module.requests, 'get', fake_get)
    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    monkeypatch.setattr(convert_module, '_stream_download', fake_stream)
    monkeypatch.setattr('app.routes.settings._default_update_feed',
                        lambda: 'https://feed/releases/latest')
    launched = []
    monkeypatch.setattr(convert_module.subprocess, 'Popen',
                        lambda cmd: launched.append(cmd))

    with client.application.test_request_context():
        resp, code = convert_module._windows_update_install()
    assert code == 500
    assert 'mismatch' in resp.get_json()['message']
    assert launched == []


def test_windows_setup_flow_refuses_missing_checksums(client, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))

    class FeedResp:
        status_code = 200

        def json(self):
            return {'assets': [
                {'name': 'AudioConverter-Setup-9.9.9.exe',
                 'browser_download_url': 'https://x/setup.exe'},
            ]}

    def fake_get(url, **kwargs):
        class R:
            status_code = 200
            content = b'whatever'
        return R() if url != 'https://feed/releases/latest' else FeedResp()

    monkeypatch.setattr(convert_module.requests, 'get', fake_get)
    monkeypatch.setattr('app.routes.settings._default_update_feed',
                        lambda: 'https://feed/releases/latest')

    def fake_stream(url, dest, timeout=600):
        with open(dest, 'wb') as f:
            f.write(b'whatever')
        import hashlib as _hl
        return _hl.sha256(b'whatever').hexdigest()

    monkeypatch.setattr(convert_module, '_stream_download', fake_stream)
    launched = []
    monkeypatch.setattr(convert_module.subprocess, 'Popen',
                        lambda cmd: launched.append(cmd))

    with client.application.test_request_context():
        resp, code = convert_module._windows_update_install()
    assert code == 500
    assert 'checksum' in resp.get_json()['message']
    assert launched == []


def test_spawn_workers_replaces_dead_ones(monkeypatch):
    import threading
    import time
    orig_desired = convert_module._desired_workers
    orig_active = convert_module._active_workers
    convert_module._desired_workers = 2
    convert_module._active_workers = 0
    try:
        convert_module._spawn_workers(2)
        deadline = time.time() + 5
        while time.time() < deadline:
            living = [t for t in threading.enumerate()
                      if t.name.startswith('audio-worker-')]
            if len(living) >= 2 and convert_module._active_workers >= 2:
                break
            time.sleep(0.05)
        assert convert_module._active_workers >= 2
    finally:
        convert_module._desired_workers = 0
        deadline = time.time() + 10
        while time.time() < deadline and convert_module._active_workers > 0:
            time.sleep(0.05)
        convert_module._desired_workers = orig_desired
        convert_module._active_workers = orig_active


def test_csv_export_neutralizes_formulas(client, tmp_path):
    evil = tmp_path / "=SUM(1+1).flac"
    evil.write_bytes(b'fLaC')
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/evil', format='FLAC',
            output_path=str(evil), status='completed'))
        db.session.commit()
    resp = client.get('/api/library.csv')
    assert resp.status_code == 200
    body = resp.data.decode('utf-8')
    assert "'=SUM(1+1).flac" in body
    assert '\n=SUM' not in body


def test_m3u_export_strips_newlines(client, tmp_path, monkeypatch):
    track = tmp_path / 'track.flac'
    track.write_bytes(b'fLaC')
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://youtu.be/pl', format='FLAC',
            output_path=str(tmp_path), status='completed',
            is_playlist=True, item_count=1, playlist_title='PL')
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/t', format='FLAC',
            output_path=str(track), status='completed',
            parent_id=pid, item_index=0))
        db.session.commit()
    monkeypatch.setattr(convert_module, '_stored_duration', lambda *a: 10)
    monkeypatch.setattr(
        convert_module, '_cached_metadata',
        lambda path: {'title': 'Evil\n/injected/path', 'artist': 'A\r\nB'})
    resp = client.get(f'/api/playlist/{pid}/m3u')
    assert resp.status_code == 200
    lines = resp.data.decode('utf-8').split('\n')
    assert not any(l.startswith('/injected') for l in lines)
    assert any('Evil /injected/path' in l for l in lines)


def test_search_escapes_like_wildcards(client):
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/abc', format='FLAC',
            output_path='/tmp/abc.flac', status='completed',
            playlist_title='100% hits'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/def', format='FLAC',
            output_path='/tmp/def.flac', status='completed',
            playlist_title='100X hits'))
        db.session.commit()
    data = client.get('/api/search?q=100%25').get_json()
    titles = [i['playlist_title'] for i in data['items']]
    assert '100% hits' in titles
    assert '100X hits' not in titles


def test_conversions_limit_clamped(client):
    for bad in ('-5', '0', '9999', 'abc'):
        data = client.get(f'/api/conversions?limit={bad}').get_json()
        assert isinstance(data, list) and len(data) <= 100


def test_file_chapters_parsed(tmp_path):
    import shutil
    import subprocess
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        __import__('pytest').skip('ffmpeg not on PATH')
    base = tmp_path / 'base.mp4'
    r = subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-f', 'lavfi',
         '-i', 'testsrc=duration=4:size=128x128:rate=10',
         '-c:v', 'mpeg4', str(base)],
        capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    meta = tmp_path / 'chapters.txt'
    meta.write_text(
        ';FFMETADATA1\n'
        '[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=Intro\n'
        '[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\ntitle=Main\n')
    out = tmp_path / 'chapters.mp4'
    r = subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-i', str(base), '-i', str(meta),
         '-map_metadata', '1', '-c', 'copy', str(out)],
        capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr

    chapters = convert_module._file_chapters(str(out))
    assert len(chapters) == 2
    assert chapters[0]['start'] == 0
    assert chapters[0]['end'] == 2
    assert chapters[0]['title'] == 'Intro'
    assert chapters[1]['title'] == 'Main'
    assert convert_module._file_chapters(str(base)) == []
    assert convert_module._file_chapters(str(tmp_path / 'nope.mp4')) == []


def test_chapters_endpoint(client, tmp_path):
    import shutil
    import subprocess
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        __import__('pytest').skip('ffmpeg not on PATH')
    out = tmp_path / 'ep.mp4'
    r = subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-f', 'lavfi',
         '-i', 'testsrc=duration=2:size=128x128:rate=10',
         '-c:v', 'mpeg4', str(out)],
        capture_output=True, timeout=120)
    assert r.returncode == 0
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/ch', format='MP4 Video',
            output_path=str(out), status='completed'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/ch').first()
    data = client.get(f'/api/chapters/{row.id}').get_json()
    assert data['ok'] is True
    assert data['chapters'] == []
    assert client.get('/api/chapters/999999').status_code == 404


def test_lrc_parsing():
    synced = ("[00:12.34] Hello\n"
              "[01:02.50] World\n"
              "[malformed line]\n"
              "[02:00.00]   \n")
    lines = convert_module._parse_lrc(synced)
    assert lines == [{'t': 12.34, 'text': 'Hello'},
                     {'t': 62.5, 'text': 'World'}]
    assert convert_module._parse_lrc('') == []
    assert convert_module._parse_lrc(None) == []


def test_lyrics_endpoint_uses_stored_tags(client, tmp_path, monkeypatch):
    track = tmp_path / 'song.flac'
    track.write_bytes(b'fLaC')
    seen = {}

    def fake_fetch(artist, title, album='', duration=0):
        seen.update(artist=artist, title=title, album=album)
        return {'plain': 'la la', 'synced': [{'t': 1.0, 'text': 'la'}]}

    monkeypatch.setattr(convert_module, '_fetch_lyrics', fake_fetch)
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/ly', format='FLAC',
            output_path=str(track), status='completed',
            tag_title='Song', tag_artist='Band', tag_album='Rec'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/ly').first()
    data = client.get(f'/api/lyrics/{row.id}').get_json()
    assert data['ok'] is True
    assert data['plain'] == 'la la'
    assert data['synced'] == [{'t': 1.0, 'text': 'la'}]
    assert seen == {'artist': 'Band', 'title': 'Song', 'album': 'Rec'}
    assert client.get('/api/lyrics/999999').status_code == 404


def test_rate_endpoint_clamps(client, tmp_path):
    track = tmp_path / 'r.flac'
    track.write_bytes(b'fLaC')
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/rt', format='FLAC',
            output_path=str(track), status='completed'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/rt').first().id
    assert client.post(f'/api/rate/{row}',
                       json={'rating': 4}).get_json() == {'ok': True, 'rating': 4}
    assert client.post(f'/api/rate/{row}',
                       json={'rating': 99}).get_json()['rating'] == 5
    assert client.post(f'/api/rate/{row}',
                       json={'rating': -3}).get_json()['rating'] == 0
    assert client.post('/api/rate/999999',
                       json={'rating': 3}).status_code == 404


def test_smart_mixes_and_group_endpoints(client, tmp_path):
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/s1', format='FLAC',
            output_path='/tmp/s1.flac', status='completed',
            tag_artist='Band', tag_album='Rec', play_count=5, rating=5,
            liked=True))
        db.session.add(ConversionHistory(
            url='https://youtu.be/s2', format='FLAC',
            output_path='/tmp/s2.flac', status='completed',
            tag_artist='Band', tag_album='Rec'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/s3', format='FLAC',
            output_path='/tmp/s3.flac', status='pending'))
        db.session.commit()
    for key in ('played', 'recent', 'unplayed', 'liked', 'rated'):
        data = client.get(f'/api/smart/{key}').get_json()
        assert data['ok'] is True and data['key'] == key
    played = client.get('/api/smart/played').get_json()['items']
    assert [i['url'] for i in played] == ['https://youtu.be/s1']
    assert client.get('/api/smart/bogus').status_code == 404

    albums = client.get('/api/albums').get_json()['albums']
    assert albums[0]['name'] == 'Rec' and albums[0]['tracks'] == 2
    artists = client.get('/api/artists').get_json()['artists']
    assert artists[0]['name'] == 'Band'
    tracks = client.get('/api/album/tracks?name=Rec&by=album').get_json()
    assert len(tracks['items']) == 2
    assert client.get('/api/album/tracks?name=Nope&by=album').get_json() == {
        'ok': True, 'items': []}


def test_stats_listening_keys(client):
    data = client.get('/api/stats').get_json()
    for key in ('total_plays', 'listened', 'top_artists', 'top_tracks'):
        assert key in data


def test_tag_columns_migrate():
    from app import _SCHEMA_MIGRATIONS
    cols = dict(_SCHEMA_MIGRATIONS['conversion_history'])
    for col in ('tag_title', 'tag_artist', 'tag_album', 'rating'):
        assert col in cols


def test_adopt_folder_and_skip_existing(client, tmp_path):
    music = tmp_path / 'music'
    (music / 'sub').mkdir(parents=True)
    (music / 'a.flac').write_bytes(b'fLaC')
    (music / 'sub' / 'b.mp3').write_bytes(b'ID3')
    (music / 'notes.txt').write_bytes(b'nope')
    (music / '.hidden.flac').write_bytes(b'fLaC')
    data = client.post('/api/adopt', json={'path': str(music)}).get_json()
    assert data['ok'] is True
    assert data['added'] == 2
    assert data['scanned'] >= 3
    again = client.post('/api/adopt', json={'path': str(music)}).get_json()
    assert again['added'] == 0 and again['skipped'] == 2
    assert client.post('/api/adopt', json={'path': '/nope'}).status_code == 400
    with client.application.app_context():
        row = ConversionHistory.query.filter(
            ConversionHistory.output_path.like('%b.mp3')).first()
        assert row is not None and row.format == 'MP3'
        assert row.status == 'completed'


def test_upload_imports_files(client, tmp_path):
    import io
    data = {
        'files': [(io.BytesIO(b'fLaC'), 'song.flac'),
                  (io.BytesIO(b'x'), 'evil.exe')],
    }
    resp = client.post('/api/upload', data=data,
                       content_type='multipart/form-data')
    body = resp.get_json()
    assert body['ok'] is True and body['added'] == 1
    assert body['skipped'] == ['evil.exe']


def test_bulk_tags(client, tmp_path):
    import subprocess as _sp
    one = tmp_path / 'one.flac'
    two = tmp_path / 'two.flac'
    for p in (one, two):
        _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
                 '-i', 'sine=frequency=440:duration=1', '-c:a', 'flac',
                 str(p)], check=True)
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/b1', format='FLAC',
            output_path=str(one), status='completed'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/b2', format='FLAC',
            output_path=str(two), status='completed'))
        db.session.commit()
        ids = [r.id for r in ConversionHistory.query.filter(
            ConversionHistory.url.like('https://youtu.be/b%')).all()]
    data = client.post('/api/tags/bulk',
                       json={'ids': ids, 'artist': 'Band'}).get_json()
    assert data == {'ok': True, 'updated': 2, 'failed': 0}
    assert client.post('/api/tags/bulk',
                       json={'ids': ids}).status_code == 400
    with client.application.app_context():
        assert ConversionHistory.query.get(ids[0]).tag_artist == 'Band'


def test_tidy_moves_into_artist_album(client, tmp_path, monkeypatch):
    track = tmp_path / 'song.flac'
    track.write_bytes(b'fLaC')
    outdir = tmp_path / 'out'
    outdir.mkdir()
    import app.routes.convert as cm
    monkeypatch.setattr(cm, 'effective_output_path', lambda: str(outdir))
    monkeypatch.setattr(cm, '_stored_tags',
                        lambda row: ('Song', 'Band', 'Rec'))
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/td', format='FLAC',
            output_path=str(track), status='completed'))
        db.session.commit()
    data = client.post('/api/tidy').get_json()
    assert data == {'ok': True, 'moved': 1, 'skipped': 0, 'already': 0}
    assert (outdir / 'Band' / 'Rec' / 'song.flac').is_file()


def test_pause_resume_cycle(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/pz', format='FLAC',
            output_path='/tmp/pz.flac', status='downloading'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/pz').first().id
    assert client.get(f'/api/pause/{row}').get_json() == {
        'ok': True, 'paused': 1}
    with client.application.app_context():
        assert db.session.get(ConversionHistory, row).status == 'paused'
        assert row in convert_module._paused_jobs
    assert client.get(f'/api/resume/{row}').get_json() == {
        'ok': True, 'resumed': 1}
    with client.application.app_context():
        assert db.session.get(ConversionHistory, row).status == 'pending'
        assert row not in convert_module._paused_jobs
    # Resume drains the requeued job through the (mocked) worker top-check.
    assert client.get(f'/api/resume/{row}').status_code == 400
    assert client.get('/api/pause/999999').status_code == 404


def test_pause_rejects_finished(client, tmp_path):
    track = tmp_path / 'done.flac'
    track.write_bytes(b'fLaC')
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/dn', format='FLAC',
            output_path=str(track), status='completed'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/dn').first().id
    assert client.get(f'/api/pause/{row}').status_code == 400


def test_download_uses_continue_flag(client, monkeypatch, tmp_path):
    seen = {}

    class FakeStdout:
        def readline(self):
            return ''

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen['cmd'] = cmd
        return FakeProc()

    monkeypatch.setattr(convert_module.subprocess, 'Popen', fake_popen)
    target = str(tmp_path / 'part.tmp')
    convert_module.download_audio(
        'https://www.youtube.com/watch?v=x', target, job=None)
    assert '--continue' in seen['cmd']


def test_transcode_creates_sibling_row(client, tmp_path):
    import subprocess as _sp
    src = tmp_path / 'song.flac'
    _sp.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=2', '-c:a', 'flac',
             str(src)], check=True)
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/tc', format='FLAC',
            output_path=str(src), status='completed'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/tc').first().id
    data = client.post('/api/transcode',
                       json={'ids': [row], 'format': 'wav'}).get_json()
    assert data == {'ok': True, 'converted': 1, 'failed': []}
    wav = tmp_path / 'song.wav'
    assert wav.is_file()
    with client.application.app_context():
        new = ConversionHistory.query.filter_by(
            output_path=str(wav)).first()
        assert new is not None and new.format == 'WAV'
        assert new.duration > 0
    assert client.post('/api/transcode',
                       json={'ids': [row], 'format': 'mp3'}).status_code == 400
    assert client.post('/api/transcode',
                       json={'ids': [999999], 'format': 'wav'}).get_json() == {
        'ok': True, 'converted': 0, 'failed': [999999]}


def test_resume_interrupted_requeues(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/z1', format='FLAC',
            output_path='/tmp/z1.flac', status='downloading'))
        db.session.add(ConversionHistory(
            url='https://youtu.be/z2', format='FLAC',
            output_path='/tmp/z2.flac', status='completed'))
        db.session.commit()
    assert convert_module._resume_interrupted() == 1
    with client.application.app_context():
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/z1').first()
        assert row.status == 'pending'
    assert convert_module._resume_interrupted() == 0


def test_bandwidth_offpeak_window(client):
    from datetime import datetime
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        if not settings:
            settings = UserSettings(output_path='/tmp/x')
            db.session.add(settings)
        settings.bandwidth_limit = 500
        settings.offpeak_limit = 100
        settings.offpeak_start = 22
        settings.offpeak_end = 7
        db.session.commit()
        assert convert_module._active_bandwidth_limit(
            datetime(2026, 1, 1, 23, 0)) == 100
        assert convert_module._active_bandwidth_limit(
            datetime(2026, 1, 1, 3, 0)) == 100
        assert convert_module._active_bandwidth_limit(
            datetime(2026, 1, 1, 12, 0)) == 500
        settings.offpeak_limit = 0
        db.session.commit()
        assert convert_module._active_bandwidth_limit(
            datetime(2026, 1, 1, 23, 0)) == 500


def test_settings_new_fields_round_trip(client):
    resp = client.post('/settings', data={
        'output_path': '/tmp/x',
        'offpeak_limit': '250',
        'offpeak_start': '21',
        'offpeak_end': '6',
        'auto_update_ytdlp': 'on',
    })
    assert resp.status_code == 302
    from app.models import UserSettings
    with client.application.app_context():
        settings = UserSettings.query.first()
        assert settings.offpeak_limit == 250
        assert settings.offpeak_start == 21
        assert settings.offpeak_end == 6
        assert settings.auto_update_ytdlp is True
    page = client.get('/settings').data.decode('utf-8')
    for marker in ('name="offpeak_limit"', 'name="offpeak_start"',
                   'name="offpeak_end"', 'name="auto_update_ytdlp"'):
        assert marker in page


def test_quality_line_parsing():
    line = convert_module._quality_line(
        '  Stream #0:0: Audio: flac, 44100 Hz, stereo, s16 (default)')
    assert line == 'FLAC · 44.1 kHz · stereo'
    assert convert_module._quality_line('no streams here') == ''
    assert convert_module._quality_line('') == ''


def test_notices_logged_and_served(client):
    from app.models import Notice
    with client.application.app_context():
        convert_module._log_notice('Hello', 'World')
        assert Notice.query.count() == 1
    data = client.get('/api/notices').get_json()
    assert data['ok'] is True and data['unread'] == 1
    assert data['items'][0]['title'] == 'Hello'
    assert client.post('/api/notices/read').get_json() == {'ok': True}
    assert client.get('/api/notices').get_json()['unread'] == 0
    assert client.post('/api/notices/read?clear=1').get_json() == {'ok': True}
    with client.application.app_context():
        assert Notice.query.count() == 0


def test_first_run_endpoint(client):
    assert client.get('/api/first-run').get_json() == {
        'ok': True, 'first_run': True}
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/x', format='FLAC',
            output_path='/tmp/x.flac', status='completed'))
        db.session.commit()
    assert client.get('/api/first-run').get_json() == {
        'ok': True, 'first_run': False}


def test_default_format_setting(client):
    from app.models import UserSettings
    with client.application.app_context():
        if not UserSettings.query.first():
            db.session.add(UserSettings(output_path='/tmp/x'))
            db.session.commit()
    page = client.get('/').data.decode('utf-8')
    assert 'option value="flac" selected' in page
    resp = client.post('/settings', data={
        'output_path': '/tmp/x', 'default_format': 'wav'})
    assert resp.status_code == 302
    page = client.get('/').data.decode('utf-8')
    assert 'option value="wav" selected' in page
    resp = client.post('/settings', data={
        'output_path': '/tmp/x', 'default_format': 'bogus'})
    assert resp.status_code == 302
    with client.application.app_context():
        assert UserSettings.query.first().default_format == 'flac'


def test_library_paging_and_search(client, tmp_path):
    for i in range(3):
        (tmp_path / f'p{i}.flac').write_bytes(b'fLaC')
    with client.application.app_context():
        for i in range(3):
            db.session.add(ConversionHistory(
                url=f'https://youtu.be/p{i}', format='FLAC',
                output_path=str(tmp_path / f'p{i}.flac'), status='completed',
                tag_title=f'Song {i}', tag_artist='Band'))
        db.session.commit()
    data = client.get('/api/library?limit=2').get_json()
    assert data['ok'] is True and data['total'] == 3
    assert len(data['items']) == 2
    data = client.get('/api/library?limit=2&offset=2').get_json()
    assert len(data['items']) == 1
    data = client.get('/api/library?q=Song 1').get_json()
    assert data['total'] == 1
    data = client.get('/api/library?q=100%').get_json()
    assert data['total'] == 0


def test_cover_cache_pruned(tmp_path, monkeypatch):
    import os as _os
    import tempfile
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path))
    target = _os.path.join(str(tmp_path), 'audio-converter-covers')
    _os.makedirs(target, exist_ok=True)
    for i in range(5):
        with open(_os.path.join(target, f'{i}.jpg'), 'wb') as f:
            f.write(b'x')
    monkeypatch.setattr(convert_module, '_COVER_CACHE_CAP', 3)
    removed = convert_module._prune_cover_cache()
    assert removed == 2
    assert len(_os.listdir(target)) == 3


def test_vacuum_endpoint(client, tmp_path):
    data = client.post('/api/maintenance/vacuum').get_json()
    assert data['ok'] is True
    assert data['db_bytes'] > 0
    assert 'covers_pruned' in data


def test_autostart_toggle_linux(client, tmp_path, monkeypatch):
    import sys as _sys
    monkeypatch.setattr(_sys, 'platform', 'linux')
    monkeypatch.setenv('HOME', str(tmp_path))
    data = client.get('/api/autostart').get_json()
    assert data == {'ok': True, 'supported': True, 'enabled': False,
                    'platform': 'linux'}
    body = client.post('/api/autostart', json={'enabled': True}).get_json()
    assert body['ok'] is True and body['enabled'] is True
    desktop_file = (tmp_path / '.config' / 'autostart'
                    / 'audioconverter.desktop')
    assert desktop_file.is_file()
    assert 'Exec=' in desktop_file.read_text()
    body = client.post('/api/autostart', json={'enabled': False}).get_json()
    assert body['ok'] is True and body['enabled'] is False
    assert not desktop_file.exists()


def test_clear_covers_endpoint(client, tmp_path, monkeypatch):
    import os as _os
    import tempfile
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path))
    target = _os.path.join(str(tmp_path), 'audio-converter-covers')
    _os.makedirs(target, exist_ok=True)
    for i in range(3):
        with open(_os.path.join(target, f'{i}.jpg'), 'wb') as f:
            f.write(b'x')
    data = client.post('/api/maintenance/clear-covers').get_json()
    assert data == {'ok': True, 'cleared': 3}
    assert _os.listdir(target) == []


def test_subscription_check_notifies_on_new_tracks(client, monkeypatch):
    from app.models import Notice, Subscription
    from datetime import datetime, timedelta
    monkeypatch.setattr(
        convert_module, 'check_subscription',
        lambda sub_id: {'ok': True, 'added': 2})
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://youtu.be/pl', format='FLAC',
            output_path='/tmp/pl', status='completed',
            is_playlist=True, item_count=0)
        db.session.add(parent)
        db.session.commit()
        sub = Subscription(
            url='https://youtu.be/pl', format='FLAC',
            output_path='/tmp/pl', playlist_title='Mix',
            parent_id=parent.id, interval_hours=24, active=True,
            last_checked=datetime.utcnow() - timedelta(hours=25))
        db.session.add(sub)
        db.session.commit()
        convert_module._check_due_subscriptions()
        assert Notice.query.filter(
            Notice.title == 'New tracks available').count() == 1


def test_perform_ytdlp_update_success(tmp_path, monkeypatch):
    import hashlib
    import sys as _sys
    import types
    new_bytes = b'fresh-ytdlp-binary'
    digest = hashlib.sha256(new_bytes).hexdigest()
    if _sys.platform == 'darwin':
        asset = 'yt-dlp_macos'
    elif _sys.platform.startswith('win'):
        asset = 'yt-dlp.exe'
    else:
        asset = 'yt-dlp_linux'
    exe = tmp_path / 'yt-dlp'
    exe.write_bytes(b'stale-binary')

    monkeypatch.setattr(convert_module, '_find_ytdlp', lambda: str(exe))
    monkeypatch.setattr(
        convert_module, '_latest_ytdlp_release',
        lambda: {'version': '2099.01.01',
                 'assets': {asset: 'https://x/bin',
                            'SHA2-256SUMS': 'https://x/sums'}})

    sums = f'{digest}  {asset}\n'

    class FakeResp:
        status_code = 200
        text = sums

    class FakeSession:
        def __init__(self):
            self.cookies = types.SimpleNamespace(clear=lambda: None)

        def get(self, url, **kwargs):
            return FakeResp()

    def fake_stream(url, dest, timeout=300):
        with open(dest, 'wb') as f:
            f.write(new_bytes)
        return hashlib.sha256(new_bytes).hexdigest()

    monkeypatch.setattr(convert_module.requests, 'Session', FakeSession)
    monkeypatch.setattr(convert_module, '_stream_download', fake_stream)
    ok, message, version = convert_module._perform_ytdlp_update()
    assert ok is True and version == '2099.01.01'
    assert '2099.01.01' in message
    assert exe.read_bytes() == new_bytes


def test_version_tuple_edges():
    from app import _version_tuple
    assert _version_tuple('v1.2.3') == (1, 2, 3)
    assert _version_tuple('1.2.3') == (1, 2, 3)
    assert _version_tuple('2026.08.19') == (2026, 8, 19)
    assert _version_tuple('garbage') == ()
    assert _version_tuple('') == ()
    assert _version_tuple(None) == ()
    assert _version_tuple('v1.2.3-beta') == ()
    assert (1, 2) < (1, 2, 3)
    assert _version_tuple('v1.10.0') > _version_tuple('v1.9.9')


def test_find_asset_by_suffix_edges():
    assert convert_module._find_asset_by_suffix({}, ('.dmg',)) == (None, None)
    assert convert_module._find_asset_by_suffix({'assets': None}, ('.dmg',)) == (None, None)
    assert convert_module._find_asset_by_suffix(
        {'assets': [{'name': 'a.dmg'}]}, ('.dmg',)) == (None, None)
    assert convert_module._find_asset_by_suffix(
        {'assets': [{'name': '', 'browser_download_url': 'https://x'}]},
        ('.dmg',)) == (None, None)
    assert convert_module._find_asset_by_suffix('not-a-dict', ('.dmg',)) == (None, None)


def test_macos_update_install_unfrozen(client):
    with client.application.test_request_context():
        resp, code = convert_module._macos_update_install()
    assert code == 400
    assert resp.get_json()['ok'] is False


def test_update_check_custom_shape(client, monkeypatch):
    import app.routes.settings as settings_module
    monkeypatch.setenv('AUDIO_CONVERTER_UPDATE_FEED',
                       'https://example.com/feed.json')

    class FakeResp:
        status_code = 200

        def json(self):
            return {'version': 'v99.0', 'url': 'https://example.com/dl'}

    monkeypatch.setattr(settings_module.requests, 'get',
                        lambda url, **kw: FakeResp())
    data = client.get('/api/update-check').get_json()
    assert data['update_available'] is True
    assert data['latest'] == 'v99.0'
    assert data['url'] == 'https://example.com/dl'


def test_concurrent_update_gets_409(client):
    assert convert_module._claim_update() is True
    try:
        resp = client.post('/api/helpers/update-ytdlp')
        assert resp.status_code == 409
        assert 'already in progress' in resp.get_json()['message']
        resp = client.post('/api/update-install')
        # Unfrozen dev run answers 400 before touching the lock; a frozen
        # build mid-update would answer 409. Either is a safe refusal.
        assert resp.status_code in (400, 409)
    finally:
        convert_module._release_update()
    # Lock released again afterwards.
    assert convert_module._claim_update() is True
    convert_module._release_update()


def test_skip_paused_cleans_up(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    outdir = tmp_path / 'out'
    outdir.mkdir()
    with client.application.app_context():
        db.session.add(ConversionHistory(
            url='https://youtu.be/sp', format='FLAC',
            output_path=str(outdir), status='paused'))
        db.session.commit()
        row = ConversionHistory.query.filter_by(
            url='https://youtu.be/sp').first()
        row_id = row.id
        convert_module._paused_jobs.add(row_id)
    partial = outdir / f'.Me at the zoo_{row_id}.tmp.part'
    partial.write_bytes(b'partial-bytes')
    assert client.get(f'/api/skip/{row_id}').get_json() == {
        'ok': True, 'skipped': 1}
    with client.application.app_context():
        assert db.session.get(ConversionHistory, row_id).status == 'skipped'
        assert row_id not in convert_module._paused_jobs
    assert not partial.exists()


def test_cleanup_partials_only_job_tmps(tmp_path):
    outdir = tmp_path / 'out'
    outdir.mkdir()
    (outdir / '.song_99.tmp').write_bytes(b'a')
    (outdir / '.song_99.tmp.part').write_bytes(b'b')
    (outdir / '.song_99.tmp.en.vtt').write_bytes(b'c')
    (outdir / '.other_100.tmp').write_bytes(b'd')
    (outdir / 'song.flac').write_bytes(b'fLaC')
    (outdir / '.hidden').write_bytes(b'h')
    convert_module._cleanup_partials(str(outdir), 99)
    remaining = sorted(p.name for p in outdir.iterdir())
    assert remaining == ['.hidden', '.other_100.tmp', 'song.flac']
    convert_module._cleanup_partials(str(tmp_path / 'nope'), 99)
    convert_module._cleanup_partials('', 99)


def test_playlist_pause_resume_children(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    with client.application.app_context():
        parent = ConversionHistory(
            url='https://youtu.be/pp', format='FLAC',
            output_path='/tmp/pp', status='downloading',
            is_playlist=True, item_count=2)
        db.session.add(parent)
        db.session.commit()
        pid = parent.id
        db.session.add(ConversionHistory(
            url='https://youtu.be/pp1', format='FLAC',
            output_path='/tmp/pp1.flac', status='downloading',
            parent_id=pid, item_index=0))
        db.session.add(ConversionHistory(
            url='https://youtu.be/pp2', format='FLAC',
            output_path='/tmp/pp2.flac', status='completed',
            parent_id=pid, item_index=1))
        db.session.commit()
    assert client.get(f'/api/pause/{pid}').get_json() == {
        'ok': True, 'paused': 1}
    with client.application.app_context():
        active = ConversionHistory.query.filter_by(parent_id=pid).all()
        assert {c.status for c in active} == {'paused', 'completed'}
    assert client.get(f'/api/resume/{pid}').get_json() == {
        'ok': True, 'resumed': 1}
    with client.application.app_context():
        active = ConversionHistory.query.filter_by(parent_id=pid).all()
        assert {c.status for c in active} == {'pending', 'completed'}
    # Pending children are pausable too.
    assert client.get(f'/api/pause/{pid}').get_json() == {
        'ok': True, 'paused': 1}
    assert client.get(f'/api/pause/999999').status_code == 404


def test_app_release_check_remembers_newer(client, monkeypatch):
    import app.routes.settings as settings_module
    monkeypatch.setattr(settings_module, '_default_update_feed',
                        lambda: 'https://example.com/feed.json', raising=False)

    class FakeResp:
        status_code = 200

        def json(self):
            return {'version': 'v99.0', 'url': 'https://example.com/dl'}

    monkeypatch.setattr(convert_module.requests, 'get',
                        lambda url, **kw: FakeResp())
    from app import APP_VERSION
    convert_module._check_app_release('https://example.com/feed.json',
                                      APP_VERSION)
    data = client.get('/api/health').get_json()
    assert data['app_update']['version'] == 'v99.0'
    assert data['app_update']['url'] == 'https://example.com/dl'
    # Current-or-newer feed clears the nudge.
    convert_module._check_app_release('https://example.com/feed.json',
                                      'v99.0')
    data = client.get('/api/health').get_json()
    assert data['app_update']['version'] == ''


def test_app_release_check_silent_on_failure(monkeypatch):
    def boom(url, **kw):
        raise ConnectionError('offline')
    monkeypatch.setattr(convert_module.requests, 'get', boom)
    convert_module._check_app_release('https://example.com/feed.json', '1.0.0')
    with convert_module._latest_app_lock:
        assert convert_module._latest_app['version'] == ''


def test_video_quality_selector(client):
    from app.models import UserSettings

    class FakeJob:
        format = 'MP4 Video'

    with client.application.app_context():
        if not UserSettings.query.first():
            db.session.add(UserSettings(output_path='/tmp/x'))
            db.session.commit()
        settings = UserSettings.query.first()
        settings.video_quality = '1080p'
        db.session.commit()
        assert convert_module._download_selector(FakeJob()) == (
            'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best')
        settings.video_quality = '720p'
        db.session.commit()
        assert convert_module._download_selector(FakeJob()) == (
            'bestvideo[height<=720]+bestaudio/best[height<=720]/best')
        settings.video_quality = 'best'
        db.session.commit()
        assert convert_module._download_selector(FakeJob()) == (
            'bestvideo+bestaudio/best')
        settings.video_quality = 'bogus'
        db.session.commit()
        assert convert_module._download_selector(FakeJob()) == (
            'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best')
        assert convert_module._download_selector(None) == 'bestaudio/best'


def test_video_submit_accepted(client, monkeypatch):
    monkeypatch.setattr(convert_module, '_ensure_worker', lambda: None)
    resp = client.post('/convert', data={
        'url': 'https://www.youtube.com/watch?v=abc',
        'format': 'video_mp4',
        'output_path': '/tmp/out',
    })
    assert resp.status_code == 302
    with client.application.app_context():
        job = ConversionHistory.query.order_by(
            ConversionHistory.created_at.desc()).first()
        assert job is not None and job.format == 'MP4 Video'


def test_home_links_to_video_page(client):
    page = client.get('/').data.decode('utf-8')
    assert 'href="/video"' in page
    assert 'Download the full video' in page
    page = client.get('/settings').data.decode('utf-8')
    assert 'name="video_quality"' in page


def _make_media(tmp_path):
    import shutil
    import subprocess
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        __import__('pytest').skip('ffmpeg not on PATH')
    video = tmp_path / 'clip.mp4'
    subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-f', 'lavfi',
         '-i', 'testsrc=duration=1:size=128x128:rate=10',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
         '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', str(video)],
        check=True, timeout=120)
    audio = tmp_path / 'tone.wav'
    subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-f', 'lavfi',
         '-i', 'sine=frequency=440:duration=1', '-c:a', 'pcm_s16le',
         str(audio)],
        check=True, timeout=120)
    return video, audio


def test_normalize_prefers_media_over_subs(tmp_path):
    video, _audio = _make_media(tmp_path)
    work = tmp_path / 'dl'
    work.mkdir()
    import shutil
    shutil.copy(str(video), str(work / '.t.tmp.webm'))
    (work / '.t.tmp.de.vtt').write_text('WEBVTT\n')
    (work / '.t.tmp.en.vtt').write_text('WEBVTT\n')
    (work / '.t.tmp.part').write_bytes(b'junk')
    got = convert_module._normalize_download(str(work / '.t.tmp'))
    assert got == str(work / '.t.tmp')
    assert os.path.isfile(str(work / '.t.tmp'))
    assert not (work / '.t.tmp.webm').exists()
    assert (work / '.t.tmp.de.vtt').is_file()


def test_normalize_video_requires_video_stream(tmp_path):
    _video, audio = _make_media(tmp_path)
    work = tmp_path / 'dl'
    work.mkdir()
    import shutil
    # Only an audio part arrived: video jobs must fail, not ship audio.
    shutil.copy(str(audio), str(work / '.t.tmp.f251.webm'))
    assert convert_module._normalize_download(
        str(work / '.t.tmp'), want_video=True) is None
    # With a real video candidate present it is chosen.
    shutil.copy(str(_video), str(work / '.t.tmp.mkv'))
    got = convert_module._normalize_download(
        str(work / '.t.tmp'), want_video=True)
    assert got == str(work / '.t.tmp')
    assert convert_module._has_video_stream(str(work / '.t.tmp')) is True
    assert convert_module._has_video_stream(str(audio)) is False


def test_verify_output_rejects_audioless_video(tmp_path):
    video, audio = _make_media(tmp_path)
    ok, _reason = convert_module._verify_output(str(video), want_video=True)
    assert ok is True
    ok, reason = convert_module._verify_output(str(audio), want_video=True)
    assert ok is False
    assert 'video stream' in reason
    ok, _reason = convert_module._verify_output(str(audio))
    assert ok is True


def test_stream_types_parsing():
    kinds = convert_module._stream_types(
        '  Stream #0:0: Video: h264, yuv420p\n'
        '  Stream #0:1(eng): Audio: aac, 44100 Hz\n')
    assert kinds == {'video', 'audio'}
    assert convert_module._stream_types('no streams') == set()


def test_video_subs_use_bounded_auto_langs(client, monkeypatch, tmp_path):
    import types
    seen = {}

    class FakeStdout:
        def readline(self):
            return ''

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen['cmd'] = cmd
        return FakeProc()

    monkeypatch.setattr(convert_module.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(convert_module, '_has_video_stream', lambda p: False)
    with client.application.app_context():
        job = ConversionHistory(
            url='https://www.youtube.com/watch?v=x', format='MP4 Video',
            output_path=str(tmp_path), status='downloading')
        db.session.add(job)
        db.session.commit()
        job_id = job.id
        convert_module.download_audio(
            'https://www.youtube.com/watch?v=x', str(tmp_path / 't.tmp'),
            job=db.session.get(ConversionHistory, job_id))
    cmd = seen['cmd']
    assert '--write-auto-subs' in cmd
    assert 'all,-live_chat' in cmd  # manual subs stay complete
    auto_idx = [i for i, a in enumerate(cmd) if a == '--write-auto-subs']
    assert auto_idx, 'auto subs flag present'
    auto_langs = cmd[cmd.index('--sub-langs', auto_idx[0]) + 1]
    assert auto_langs != 'all,-live_chat'
    assert len(auto_langs.split(',')) <= 20


def test_politeness_flags_in_commands(client, monkeypatch, tmp_path):
    seen = {}

    class FakeStdout:
        def readline(self):
            return ''

    class FakeProc:
        returncode = 0

        def __init__(self):
            self.stdout = FakeStdout()

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        seen['cmd'] = cmd
        return FakeProc()

    monkeypatch.setattr(convert_module.subprocess, 'Popen', fake_popen)
    convert_module.download_audio(
        'https://www.youtube.com/watch?v=x', str(tmp_path / 't.tmp'),
        job=None)
    cmd = seen['cmd']
    assert '--sleep-subtitles' in cmd
    assert cmd[cmd.index('--sleep-subtitles') + 1] == '1'
    assert '--retry-sleep' in cmd


def test_host_guard(client):
    assert client.get('/', base_url='http://localhost/').status_code == 200
    assert client.get('/', base_url='http://127.0.0.1:57600/').status_code == 200
    resp = client.get('/', base_url='http://evil.com/')
    assert resp.status_code == 403
    assert resp.get_json()['ok'] is False
    resp = client.get('/', base_url='http://127.0.0.1.evil.com/')
    assert resp.status_code == 403
