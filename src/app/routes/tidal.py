"""Tidal login (OAuth authorization code flow) plus API track-listing reads.

Why login at all: a Tidal playlist page is an empty JavaScript shell until
Tidal's own code runs inside it (verified: every URL variant and crawler
user-agent returns the same track-less shell, and api.tidal.com answers
401 without a token). The browser view works because Tidal's code brings
its own baked-in keys, which aren't stably reusable. So importing needs
either the user's own free API credentials from developer.tidal.com, or
nothing at all — there is no reliable anonymous middle ground.
"""
import base64
import hashlib
import time
import secrets
from urllib.parse import urlencode

import requests
from flask import (Blueprint, request, redirect, url_for, flash, jsonify,
                   session as flask_session)

from app.models import db, UserSettings

bp = Blueprint('tidal', __name__)

AUTH_URL = 'https://auth.tidal.com/v1/oauth2/authorize'
TOKEN_URL = 'https://auth.tidal.com/v1/oauth2/token'
API_BASE = 'https://api.tidal.com/v1'
SCOPES = 'playlists.read'


def _settings():
    return UserSettings.query.first()


def _redirect_uri():
    # Matches the redirect URL shown on the Settings page.
    return request.host_url.rstrip('/') + '/api/tidal/callback'


def _session():
    session = requests.Session()
    session.cookies.clear()
    return session


def is_connected():
    settings = _settings()
    return bool(settings and settings.tidal_access_token)


def _valid_token():
    """A usable access token, refreshing it first when expired."""
    settings = _settings()
    if not settings or not settings.tidal_access_token:
        return None
    if settings.tidal_expires_at and settings.tidal_expires_at - time.time() > 60:
        return settings.tidal_access_token
    if not settings.tidal_refresh_token:
        return None
    refresh = {
        'grant_type': 'refresh_token',
        'refresh_token': settings.tidal_refresh_token,
        'client_id': settings.tidal_client_id,
    }
    if settings.tidal_client_secret:
        refresh['client_secret'] = settings.tidal_client_secret
    try:
        resp = _session().post(TOKEN_URL, timeout=20, data=refresh)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    settings.tidal_access_token = data.get('access_token', '')
    if data.get('refresh_token'):
        settings.tidal_refresh_token = data['refresh_token']
    try:
        settings.tidal_expires_at = int(time.time()) + int(data.get('expires_in', 0))
    except (TypeError, ValueError):
        settings.tidal_expires_at = 0
    db.session.commit()
    return settings.tidal_access_token or None


@bp.route('/api/tidal/login')
def tidal_login():
    """Start the Tidal login: redirect to Tidal's consent page."""
    settings = _settings()
    if not settings or not settings.tidal_client_id:
        flash('Add your Tidal client ID in Settings first.', 'error')
        return redirect(url_for('settings.settings_page'))
    state = secrets.token_urlsafe(24)
    flask_session['tidal_oauth_state'] = state
    params = {
        'response_type': 'code',
        'client_id': settings.tidal_client_id,
        'redirect_uri': _redirect_uri(),
        'scope': SCOPES,
        'state': state,
    }
    if not settings.tidal_client_secret:
        # No secret? Use PKCE instead so a plain client ID is enough.
        verifier = secrets.token_urlsafe(64)
        flask_session['tidal_oauth_verifier'] = verifier
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params['code_challenge'] = challenge
        params['code_challenge_method'] = 'S256'
    return redirect(f'{AUTH_URL}?{urlencode(params)}')


@bp.route('/api/tidal/callback')
def tidal_callback():
    """Tidal redirects back here; exchange the code for tokens."""
    settings = _settings()
    if not settings:
        flash('Save your Tidal settings first.', 'error')
        return redirect(url_for('settings.settings_page'))
    if request.args.get('state') != flask_session.pop('tidal_oauth_state', None):
        flash('Tidal login was interrupted. Please try again.', 'error')
        return redirect(url_for('settings.settings_page'))
    code = request.args.get('code', '')
    if not code:
        flash('Tidal did not approve the login.', 'error')
        return redirect(url_for('settings.settings_page'))
    exchange = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': _redirect_uri(),
        'client_id': settings.tidal_client_id,
    }
    verifier = flask_session.pop('tidal_oauth_verifier', None)
    if verifier:
        exchange['code_verifier'] = verifier
    if settings.tidal_client_secret:
        exchange['client_secret'] = settings.tidal_client_secret
    try:
        resp = _session().post(TOKEN_URL, timeout=20, data=exchange)
    except Exception as e:
        flash(f'Could not reach Tidal: {str(e)}', 'error')
        return redirect(url_for('settings.settings_page'))
    if resp.status_code != 200:
        flash('Tidal rejected the login. Check the client ID and secret.', 'error')
        return redirect(url_for('settings.settings_page'))
    try:
        data = resp.json()
    except ValueError:
        flash('Tidal returned an unreadable response.', 'error')
        return redirect(url_for('settings.settings_page'))
    settings.tidal_access_token = data.get('access_token', '')
    settings.tidal_refresh_token = data.get('refresh_token', '')
    try:
        settings.tidal_expires_at = int(time.time()) + int(data.get('expires_in', 0))
    except (TypeError, ValueError):
        settings.tidal_expires_at = 0
    db.session.commit()
    if settings.tidal_access_token:
        flash('Tidal connected. You can now import Tidal playlists.', 'success')
    else:
        flash('Tidal login did not return a token.', 'error')
    return redirect(url_for('settings.settings_page'))


@bp.route('/api/tidal/logout')
def tidal_logout():
    """Forget Tidal tokens (the client ID/secret stay saved)."""
    settings = _settings()
    if settings:
        settings.tidal_access_token = ''
        settings.tidal_refresh_token = ''
        settings.tidal_expires_at = 0
        db.session.commit()
    flash('Tidal disconnected.', 'info')
    return redirect(url_for('settings.settings_page'))


@bp.route('/api/tidal/status')
def tidal_status():
    """Whether Tidal is currently connected."""
    return jsonify({'ok': True, 'connected': is_connected()})


def _api_get(path, params=None):
    """Authenticated Tidal API GET. Returns parsed JSON or None."""
    token = _valid_token()
    if not token:
        return None
    try:
        resp = _session().get(
            API_BASE + path, timeout=25,
            headers={'Authorization': f'Bearer {token}',
                     'Accept': 'application/json'},
            params=params or {})
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def extract_tidal_playlist(url, max_tracks=50):
    """Read a Tidal playlist via the user's login.

    Returns {'success': True, 'title', 'tracks': [{'artist','title',
    'duration'}]} or {'success': False, 'error': str}.
    """
    from urllib.parse import urlparse
    try:
        segs = [s for s in urlparse(url).path.split('/') if s]
        uuid = segs[segs.index('playlist') + 1].split('?')[0]
        if not uuid:
            raise IndexError
    except (ValueError, IndexError):
        return {'success': False, 'error': 'Could not find a Tidal playlist ID in that link'}
    if not is_connected():
        return {'success': False,
                'error': 'Tidal hides track listings from anonymous readers, so '
                         'connect Tidal in Settings first (free client ID from '
                         'developer.tidal.com is enough).'}
    info = _api_get(f'/playlists/{uuid}', params={'countryCode': 'US'})
    if not info:
        return {'success': False,
                'error': 'Tidal did not return that playlist (it may be private or removed)'}
    title = str(info.get('title') or 'Tidal Playlist').strip()
    tracks = []
    offset = 0
    while len(tracks) < max_tracks:
        page = _api_get(f'/playlists/{uuid}/items',
                        params={'countryCode': 'US', 'limit': 100, 'offset': offset})
        if not page:
            break
        items = page.get('items') or []
        if not items:
            break
        for wrapper in items:
            item = wrapper.get('item') if isinstance(wrapper, dict) else None
            if not isinstance(item, dict):
                continue
            song = str(item.get('title') or '').strip()
            artists = [str(a.get('name') or '').strip()
                       for a in (item.get('artists') or [])
                       if isinstance(a, dict) and a.get('name')]
            try:
                duration = float(item.get('duration') or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if song:
                tracks.append({'artist': ', '.join(artists),
                               'title': song, 'duration': duration})
            if len(tracks) >= max_tracks:
                break
        offset += len(items)
        if offset >= int(page.get('totalNumberOfItems') or offset or 0):
            break
    if not tracks:
        return {'success': False, 'error': 'No tracks found on that Tidal playlist'}
    return {'success': True, 'title': title or 'Tidal Playlist', 'tracks': tracks}
