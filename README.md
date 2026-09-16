# Audio Converter

![build](https://github.com/EssJay99/audio-converter/actions/workflows/build.yml/badge.svg)

Turn YouTube, SoundCloud, Spotify, Apple Music, and Tidal links into
high-quality local audio files (FLAC, ALAC, WAV, OGG Vorbis) — with a
built-in player, a real library, and no account or cloud involved.

## Features

- **Single tracks and full playlists.** Paste a YouTube playlist, channel,
  `@handle`, or SoundCloud set/artist link; every track lands in one folder
  with live progress per track.
- **Streaming-service imports.** Spotify and Apple Music playlist/album links
  are read without any login; each song is matched on YouTube (SoundCloud
  fallback) and saved into one folder. Tidal works after a free developer
  login in Settings.
- **Library management.** Search, filter by status or folder, sort, paginate,
  rename files, edit embedded tags, delete tracks or whole playlists from
  disk, storage readout, and one-click integrity verification.
- **In-app player.** Play/pause, next/previous, seek, volume, cover art,
  persistent queue with shuffle and repeat, keyboard shortcuts, dark mode.
- **Reliability.** Parallel downloads with pause-all, per-track timeouts,
  automatic retries, duplicate prevention, resume after restarts, corruption
  and wrong-song verification on every finished file, skip/ retry controls.
- **Privacy.** Tracking identifiers stripped from links, cookieless
  cacheless downloads with no watch-history reporting, optional proxy
  routing, local-only server, no analytics or CDN calls.

## Install (no command line needed)

Download the installer for your system from
[Releases](https://github.com/EssJay99/audio-converter/releases/latest):

| System  | File | Steps |
|---|---|---|
| macOS | `AudioConverter-1.0.0.dmg` | Open, drag to Applications, double-click. First launch: right-click → Open (the app isn't Apple-notarized). |
| Windows | `AudioConverter-Setup-1.0.0.exe` | Run, click through the wizard, launch from the Start menu. |
| Linux | `AudioConverter-1.0.0-linux.tar.gz` | Extract, run `./install.sh`, launch from the app menu. Optional: `sudo apt install gir1.2-webkit2-4.1` for the embedded window (otherwise it opens in your browser). |

Everything needed (downloader, converter) is inside. Your music and
library stay on your machine only.

## Usage

1. Paste a link, pick a format, pick a folder, hit Convert.
2. Watch progress on the Home page; expand playlists for per-track rows.
3. Play in the bottom-bar player, or Download / Copy / Open folder per file.
4. Settings covers output path, formats, organization, duplicates, retries,
   timeouts, speed, workers, proxy, privacy, Tidal login, and helper updates.

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
