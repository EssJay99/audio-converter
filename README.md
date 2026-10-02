# Audio Converter

![build](https://github.com/EssJay99/audio-converter/actions/workflows/build.yml/badge.svg)

Turn YouTube, SoundCloud, Spotify, Apple Music, and Tidal links into
high-quality local audio files (FLAC, ALAC, WAV, OGG Vorbis) — or full
videos (MP4, WebM, MKV) — with a built-in player, a real library, and no
account or cloud involved.

## Features

- **Single tracks and full playlists.** Paste a YouTube playlist, channel,
  `@handle`, or SoundCloud set/artist link; every track lands in one folder
  with live progress per track. Home converts audio; the Video tab grabs
  full videos (music videos included) with a quality cap you set.
- **Streaming-service imports.** Spotify and Apple Music playlist/album links
  are read without any login; each song is matched on YouTube (SoundCloud
  fallback) and saved into one folder. Tidal works after a free developer
  login in Settings.
- **Library management.** Search across filenames and tags, album/artist
  browsers, smart mixes (most played, unplayed, liked, top rated), star
  ratings, bulk tag editing, adopt-an-existing-folder, drag-and-drop import,
  tidy-into-Artist/Album, cover backfill, transcode between formats,
  CSV/M3U export, prune-missing, and one-click integrity verification.
  Deletes go to the Trash, never straight to oblivion.
- **In-app player.** Play/pause, next/previous, seek, volume, cover art,
  persistent queue with shuffle, repeat, and crossfade, 5-band EQ with
  presets, visualizer, theater mode with subtitles/chapters/lyrics,
  sleep timer (minutes or end-of-track/queue), resume positions, OS media
  keys, keyboard shortcuts, command palette (Ctrl/⌘K), dark mode.
- **Reliability.** Parallel downloads with per-track pause/resume, global
  pause-all, per-track timeouts, automatic retries, duplicate prevention,
  resume-after-restart, corruption/length/song-match verification on every
  finished file, checksum-verified self-updates for the app and yt-dlp,
  background freshness checks with health-banner nudges.
- **Privacy.** Tracking identifiers stripped from links, cookieless
  cacheless downloads with no watch-history reporting, proxy routed via
  environment (never visible in process lists), localhost-only server with
  Host validation and hardened headers, owner-only data directory, no
  analytics or CDN calls.

## Install (no command line needed)

Download the installer for your system from
[Releases](https://github.com/EssJay99/audio-converter/releases/latest):

| System  | File | Steps |
|---|---|---|
| macOS | `AudioConverter-<version>.dmg` | Open, drag to Applications, double-click. First launch: right-click → Open. |
| Windows | `AudioConverter-Setup-<version>.exe` | Run, click through the wizard, launch from the Start menu. |
| Linux | `AudioConverter-<version>-linux.tar.gz` | Extract, run `./install.sh`, launch from the app menu. Optional: `sudo apt install gir1.2-webkit2-4.1` for the embedded window (otherwise it opens in your browser). |

Everything needed (downloader, converter) is inside. Your music and
library stay on your machine only.

## Usage

1. Paste a link, pick a format, pick a folder, hit Convert (audio on Home,
   full video on the Video tab).
2. Watch progress on the Home page; pause/resume per track, expand
   playlists for per-track rows.
3. Play in the bottom-bar player or the Player tab (library, mixes,
   playlists, lyrics); Download / Copy / Open folder per file.
4. Press Ctrl/⌘K anywhere for the command palette.
5. Settings covers output path, formats, video quality, organization,
   duplicates, retries, timeouts, speed (plus off-peak schedule), workers,
   proxy, privacy, Tidal login, helper updates, maintenance, and login-start.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -q        # full suite
python src/desktop.py             # run the desktop app
```

- `src/app/` — Flask app (routes, models, worker pool)
- `src/static/js/` — UI (`main.js`), player (`player.js`), folder browser
- `src/templates/` — pages and the player partial
- `scripts/build_macos.sh`, `scripts/build_linux.sh`,
  `scripts/build_windows.bat` — per-OS bundles (each vendors standalone
  yt-dlp/ffmpeg helpers)
- `.github/workflows/` — `build.yml` (test + build all three installers,
  publish on `v*` tags), `release-auto.yml` (weekly releases when main moves)

## Privacy notes

Downloading from a streaming site inherently shows it a request from your
network — no downloader can avoid that. Everything else is minimized (see
Features), and routing through Tor/a proxy in Settings hides even that.
