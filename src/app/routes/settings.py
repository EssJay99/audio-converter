from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from app.models import db, UserSettings
from app import APP_VERSION
import os
import requests

bp = Blueprint('settings', __name__)

SETTING_FIELDS = ('output_path', 'wav_sample_rate', 'wav_bit_depth', 'ogg_quality', 'flac_compression')
BOOLEAN_FIELDS = ('skip_existing', 'privacy_mode', 'desktop_notifications',
                  'tray_icon',)


def _int_from_form(request, name, default, minimum, maximum):
    # Number inputs post strings; fall back to the default when blank/invalid.
    try:
        value = int(request.form.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _bool_from_form(request, name):
    # A checked checkbox posts its value; an unchecked checkbox posts nothing.
    value = request.form.get(name, '')
    if value == 'on' or value in ('1', 'true', 'True'):
        return True
    if value == '':
        return False
    return value not in ('0', 'false', 'False')


def get_default_output_path():
    home = os.path.expanduser('~')
    return os.path.join(home, 'Audio-Converter', 'output')


def get_settings_dict():
    """Current settings as a plain dict, with defaults if none are saved yet."""
    defaults = {
        'output_path': get_default_output_path(),
        'wav_sample_rate': 'auto',
        'wav_bit_depth': '16',
        'ogg_quality': '8',
        'flac_compression': '5',
        'skip_existing': True,
        'privacy_mode': True,
        'retry_count': 3,
        'job_timeout': 300,
        'bandwidth_limit': 0,
        'proxy': '',
        'worker_count': 3,
        'tidal_client_id': '',
        'tidal_client_secret': '',
        'tidal_connected': False,
        'desktop_notifications': True,
        'close_behavior': 'ask',
        'tray_icon': True,
    }
    user_settings = UserSettings.query.first()
    if user_settings:
        for field in SETTING_FIELDS:
            value = getattr(user_settings, field, None)
            if value:
                defaults[field] = value
        for field in BOOLEAN_FIELDS:
            value = getattr(user_settings, field, None)
            if value is not None:
                defaults[field] = bool(value)
        if getattr(user_settings, 'retry_count', None) is not None:
            defaults['retry_count'] = user_settings.retry_count
        if getattr(user_settings, 'job_timeout', None) is not None:
            defaults['job_timeout'] = user_settings.job_timeout
        if getattr(user_settings, 'bandwidth_limit', None) is not None:
            defaults['bandwidth_limit'] = user_settings.bandwidth_limit
        defaults['proxy'] = getattr(user_settings, 'proxy', None) or ''
        if getattr(user_settings, 'worker_count', None) is not None:
            defaults['worker_count'] = user_settings.worker_count
        defaults['tidal_client_id'] = getattr(user_settings, 'tidal_client_id', None) or ''
        defaults['tidal_client_secret'] = getattr(user_settings, 'tidal_client_secret', None) or ''
        defaults['tidal_connected'] = bool(getattr(user_settings, 'tidal_access_token', None))
        defaults['close_behavior'] = getattr(user_settings, 'close_behavior', None) or 'ask'
    return defaults


@bp.route('/settings')
def settings_page():
    return render_template('settings.html', request_path='/settings',
                           app_version=APP_VERSION, **get_settings_dict())


@bp.route('/settings', methods=['POST'])
def save_settings():
    data = {field: request.form.get(field, '').strip() for field in SETTING_FIELDS}
    data.update({field: _bool_from_form(request, field) for field in BOOLEAN_FIELDS})
    data['retry_count'] = _int_from_form(request, 'retry_count', 3, 0, 10)
    data['job_timeout'] = _int_from_form(request, 'job_timeout', 300, 30, 3600)
    data['bandwidth_limit'] = _int_from_form(request, 'bandwidth_limit', 0, 0, 100000)
    data['proxy'] = request.form.get('proxy', '').strip()
    data['worker_count'] = _int_from_form(request, 'worker_count', 3, 1, 8)
    data['tidal_client_id'] = request.form.get('tidal_client_id', '').strip()
    data['tidal_client_secret'] = request.form.get('tidal_client_secret', '').strip()
    data['close_behavior'] = request.form.get('close_behavior', 'ask').strip()
    if data['close_behavior'] not in ('ask', 'quit'):
        data['close_behavior'] = 'ask'

    if not data['output_path']:
        flash('Output path cannot be empty', 'error')
        return redirect(url_for('settings.settings_page'))

    user_settings = UserSettings.query.first()
    if user_settings:
        for field in SETTING_FIELDS:
            setattr(user_settings, field, data[field])
        for field in BOOLEAN_FIELDS:
            setattr(user_settings, field, data[field])
        user_settings.retry_count = data['retry_count']
        user_settings.job_timeout = data['job_timeout']
        user_settings.bandwidth_limit = data['bandwidth_limit']
        user_settings.proxy = data['proxy']
        user_settings.worker_count = data['worker_count']
        # New Tidal credentials wipe stored tokens (they belong to the old app).
        if (user_settings.tidal_client_id != data['tidal_client_id']
                or (data['tidal_client_secret'] and user_settings.tidal_client_secret != data['tidal_client_secret'])):
            user_settings.tidal_access_token = ''
            user_settings.tidal_refresh_token = ''
            user_settings.tidal_expires_at = 0
        user_settings.tidal_client_id = data['tidal_client_id']
        if data['tidal_client_secret']:
            user_settings.tidal_client_secret = data['tidal_client_secret']
        user_settings.close_behavior = data['close_behavior']
    else:
        user_settings = UserSettings(**data)

    try:
        db.session.add(user_settings)
        db.session.commit()
        flash('Settings saved successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error saving settings: {str(e)}', 'error')
        return redirect(url_for('settings.settings_page'))

    # Apply the pool size live; growing starts threads now, shrinking lets
    # busy workers finish their current track first.
    try:
        from app.routes.convert import set_worker_count
        set_worker_count(data['worker_count'])
    except Exception:
        pass

    return redirect(url_for('settings.settings_page'))


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).strip().lstrip('v').split('.'))
    except (TypeError, ValueError):
        return ()


def _default_update_feed():
    override = os.environ.get('AUDIO_CONVERTER_UPDATE_FEED', '').strip()
    if override:
        return override
    return 'https://api.github.com/EssJay99/audio-converter/releases/latest'


def _parse_feed_payload(data):
    """Accept our {version, url} shape and GitHub's {tag_name, html_url}."""
    if not isinstance(data, dict):
        return '', ''
    version = str(data.get('version') or data.get('tag_name') or '')
    url = str(data.get('url') or data.get('html_url') or '')
    return version, url


@bp.route('/api/update-check')
def update_check():
    """Compare this build against the release feed, if one is configured.

    Set AUDIO_CONVERTER_UPDATE_FEED to a JSON endpoint returning
    {"version": "1.2.0", "url": "https://..."} (e.g. a GitHub release).
    With no feed configured the answer says so instead of failing.
    """
    feed = _default_update_feed()
    if not feed:
        return jsonify({'ok': True, 'current': APP_VERSION, 'latest': None,
                        'update_available': False,
                        'message': 'No update feed configured yet.'})
    try:
        resp = requests.get(feed, timeout=15, headers={'Accept': 'application/json'})
    except Exception as e:
        return jsonify({'ok': False, 'message': f'Could not reach update feed: {str(e)}'}), 502
    if resp.status_code != 200:
        return jsonify({'ok': False, 'message': 'Update feed returned an error'}), 502
    try:
        data = resp.json()
    except ValueError:
        return jsonify({'ok': False, 'message': 'Update feed was unreadable'}), 502
    latest, url = _parse_feed_payload(data)
    if _version_tuple(latest) > _version_tuple(APP_VERSION):
        return jsonify({'ok': True, 'current': APP_VERSION, 'latest': latest,
                        'update_available': True, 'url': url,
                        'message': f'Version {latest} is available.'})
    return jsonify({'ok': True, 'current': APP_VERSION, 'latest': latest or APP_VERSION,
                    'update_available': False,
                    'message': 'You are on the latest version.'})