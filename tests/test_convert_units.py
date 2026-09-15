import os

from app.routes.convert import (
    VALID_FORMATS,
    LABEL_TO_KEY,
    unique_file_path,
    sanitize_filename,
    is_valid_format,
    _source_name,
)


def test_sanitize_filename_removes_reserved_chars():
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == 'a_b_c_d_e_f_g_h_i_j'


def test_sanitize_filename_handles_empty():
    assert sanitize_filename('') == 'audio'
    assert sanitize_filename('...') != ''


def test_sanitize_filename_collapses_whitespace():
    assert sanitize_filename('  hello   world  ') == 'hello world'


def test_is_valid_format():
    assert is_valid_format('flac')
    assert is_valid_format('WAV')
    assert is_valid_format('ogg_vorbis')
    assert not is_valid_format('mp3')
    assert not is_valid_format('')


def test_label_to_key_roundtrip():
    for key, fmt in VALID_FORMATS.items():
        assert LABEL_TO_KEY[fmt['label']] == key


def test_unique_file_path_appends_suffix(tmp_path):
    first = unique_file_path(str(tmp_path), 'song.flac')
    with open(first, 'w'):
        pass

    second = unique_file_path(str(tmp_path), 'song.flac')
    assert second.endswith('song (2).flac')
    with open(second, 'w'):
        pass

    third = unique_file_path(str(tmp_path), 'song.flac')
    assert third.endswith('song (3).flac')


def test_unique_file_path_no_collision(tmp_path):
    path = unique_file_path(str(tmp_path), 'new song.flac')
    assert path.endswith('new song.flac')


def test_source_name():
    assert _source_name('https://youtube.com/watch?v=abc') == 'YouTube Audio'
    assert _source_name('https://youtu.be/abc') == 'YouTube Audio'
    assert _source_name('https://soundcloud.com/user/track') == 'SoundCloud Audio'
    assert _source_name('https://example.com/x') == 'Audio'