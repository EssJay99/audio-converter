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
    resp = client.get('/convert')
    assert resp.status_code == 200


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
    assert '--proxy' in args
    assert 'socks5://127.0.0.1:9050' in args
    assert '--limit-rate' in args
    assert '500K' in args


def test_ytdlp_net_args_empty_by_default(client):
    with client.application.app_context():
        assert convert_module._ytdlp_net_args() == []


def test_delete_completed_file_and_row(client, completed_conversion):
    job_id, path = completed_conversion
    assert os.path.isfile(path)

    resp = client.post(f'/api/delete/{job_id}')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'removed_file': True}
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
                               'removed_files': 1}
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
        return 120.0, {'title': 'T', 'artist': 'A', 'album': '', 'date': ''}

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
                              parent_id=pid, item_index=0),
            ConversionHistory(url='https://youtu.be/2', format='FLAC',
                              output_path=str(two), status='completed',
                              parent_id=pid, item_index=1),
        ])
        db.session.commit()

    resp = client.get(f'/api/playlist/{pid}/m3u')
    assert resp.status_code == 200
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
    desktop = _load_desktop()
    port = desktop.find_free_port(preferred=desktop.DEFAULT_PORT)
    assert port == desktop.DEFAULT_PORT


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

def test_backup_download_is_valid_sqlite(client):
    import sqlite3
    resp = client.get('/api/backup')
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'].startswith('attachment')
    path = '/tmp/opencode/backup-test.db'
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
