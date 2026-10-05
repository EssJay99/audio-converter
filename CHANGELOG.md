# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project does *not* follow semantic versioning strictly — see the
GitHub Releases page for actual version tags.

## [Unreleased]

### Security

- The bundled install update is now **signed**: the app refuses a release
  whose `SHA256SUMS.txt` isn't signed with the maintainer's minisign key.
  If the bundled pubkey is missing or the signature fails, the self-
  installer aborts instead of falling back to an unverified checksum.
- Every destructive action (clear history, factory reset, prune, vacuum,
  update yt-dlp, clear cover cache, tidy library, clear recently-played)
  now requires you to type a confirmation phrase in a modal.
- Per-IP rate limit on the heaviest paths (`/api/playable/<id>` plus the
  upload and helper-install endpoints) keeps a runaway tab from
  flooding ffmpeg or GitHub.
- URLs pasted into *Convert* are re-validated after redirects, so a
  `https://attacker/redirect` → `http://10.0.0.5/...` is rejected before
  yt-dlp runs.
- The output-path setting rejects path-traversal tricks, embedded NUL
  bytes, and symlinks pointing outside the writable tree.
- The session secret is persisted to a `0o600` file in the data
  directory; if it can't be written you see a warning banner instead of
  a silently-rotating key.
- HSTS only appears on HTTPS requests, never on the localhost HTTP the
  desktop launcher binds.
- Local file uploads are now probed with ffmpeg before they land in
  history — a script masquerading as `.mp3` no longer creates a
  permanently-failed row.
- The hourly janitor skips permanently-failed rows (private videos,
  removed tracks) so users don't lose them to `prune_failed_days`.
- The rotating log file scrubs bearer tokens, API keys, `user:pass@`
  credentials, and tracker query strings before writing to disk.
- Settings writes are now optimistically-concurrent: a second tab
  editing the same form gets a banner asking it to reload instead of
  clobbering changes silently.
- SECURITY.md now lives in the repo with a private disclosure channel
  and a 30-day response SLA.

### Added

- **Radio mode**: when the queue runs out, the player picks the next
  similar track — same artist, then same album, then same folder,
  then least-played overall. Server-stored toggle in Settings › Playback.
- **Storage quota**: opt-in cap in GB. The janitor trims oldest-played
  finished tracks to stay under the cap.
- **`prune_failed_days`**: opt-in age cutoff for transient failures
  (default 0 = keep forever; permanent failures always kept).
- **`import_engine` + `import_strict`**: Spotify/Apple Music imports can
  prefer YouTube, SoundCloud, or both, and "strict matching" trades
  wrong songs for fewer misses.
- **`pref_sub_lang`**: chosen subtitle language appears first in video
  downloads.
- **Subtitle backfill**: one-click button fetches subtitles for videos
  that have none.
- **Per-row "Audio" button** on video history entries: rewraps the audio
  stream losslessly into a FLAC next to the video.
- **Per-row "Info" button**: opens a tech-details modal (format,
  duration, size, tags, play count).
- **Settings › Helpers › Report issue**: opens GitHub with a prefilled
  bug template including diagnostics JSON.
- **`/changelog` page**: surfaced after every packaged upgrade until
  visited.
- **Migration banner**: appears once when the database is upgraded by
  this build.

### Improved

- yt-dlp auto-update falls back to PyPI when GitHub's API is rate-
  limited; SHA-256 verification runs on whichever source advertised the
  version.
- The macOS updater surfaces a banner when the running bundle isn't
  signed or notarized so users know to expect Gatekeeper warnings.
- yt-dlp's auto-sub language list is now a curated shortlist (not
  `all`), avoiding the IP-throttling storms that `?si=…` tracking
  fan-outs trigger.

### Fixed

- A user-submitted URL that 302's to a private address is now rejected
  *before* yt-dlp runs (SSRF defence).
- The settings save no longer clobbers a newer tab's edits thanks to
  optimistic concurrency.
- Watch-history clearing doesn't drop play counts.
- Sort folders by file count, not just name.

### Developer

- App lints clean under `bandit` and `pip-audit`; a CI job runs both.
- `pytest` runs in CI on every PR.
- The test suite has **470 passing** checks, including all of the
  hardening scenarios above.
- Threat-model document at `docs/SECURITY.md` describes what the app
  does and does not protect against.

## Earlier

The original changelog entries (everything before this section) live in
[`docs/HISTORY.md`](docs/HISTORY.md) and the [GitHub release
notes](https://github.com/EssJay99/audio-converter/releases). The
pre-1.0 history covers the hard-won lessons that shaped this project:

- Original YouTube-only FLAC converter (Python 3 + requests).
- First Flask UI with a single drag-and-drop target.
- SoundCloud support via yt-dlp.
- Multi-format audio (MP3, M4A, Opus) with passthrough.
- Video downloads with browser-playable proxy + chapter support.
- Theater-mode playback in the desktop launcher.
- Subscription scheduler (auto re-check playlists/channels).
- Database management UI with backup/restore/factory-reset.
- Player tab with Smart Mixes, Albums/Artists/Folders views, EQ,
  visualizer.
- Welcome tour for first-time users.