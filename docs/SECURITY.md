# Threat Model

This document describes what the YouTube / SoundCloud Audio Converter
is — and is not — designed to defend against. It complements
[SECURITY.md](../SECURITY.md) (which describes how to report a
vulnerability) by spelling out the assumptions we *built on*, so a
reviewer can decide whether they still hold in 2026.

## What the app does

A single-user desktop application that:

1. Accepts a YouTube / SoundCloud / Spotify / Apple Music / Tidal /
   generic-vodcast URL.
2. Downloads the audio (or video) track via `yt-dlp`.
3. Converts the audio to FLAC / MP3 / M4A / Opus / WAV, or the video
   to MP4 / WebM / MKV, using FFmpeg.
4. Stores the result in a user-chosen output folder.
5. Records the conversion in a local SQLite database so the user can
   search / re-encode / re-play.

The Flask web app is bound to `127.0.0.1` only. The user opens it in
their default browser (or a `pywebview` desktop window). The database
lives in the OS per-user data directory.

## Threat model in one paragraph

The threat model is a *local, single-trust* app: the local user is
trusted; remote network principals are untrusted; the only remote
principal expected to communicate with the app is the user themselves
(via their browser on the same machine). All defenses are calibrated
to that assumption.

## What we defend against

### Cross-site request forgery (CSRF) from another web origin

A user has Audio Converter open in one tab, and a malicious page in
another tab tries to call `/convert`, `/api/db/factory-reset`, etc.

Defenses:

- `flask_wtf.CSRFProtect` on every state-changing endpoint.
- `SameSite=Lax` + `HttpOnly` session cookies.
- `_same_origin_required` on local-side GETs that mutate state
  (`/api/notify-test`, `/api/proxy-check`).
- Typed-phrase confirmation on every destructive endpoint — same-
  origin XHR can't silently type "RESET" in a hidden iframe.

### Same-origin abuse (browser extensions, dev-tools)

A browser extension on the same origin can post requests with a valid
CSRF token. It cannot, however, intercept the typed-phrase confirmations
without being detected (the modal is on-screen and the user sees the
prompt). Destructive actions therefore require user-visible consent.

### Server-side request forgery (SSRF)

A user pastes a URL into *Convert* that points at an internal network
resource (cloud metadata service, local admin page, private IP).

Defenses:

- URL allowlist on the import paths (YouTube, SoundCloud, Spotify, etc.).
- `_is_public_http_url` resolves hostnames and rejects any non-global
  IP, blocking loopback / private / link-local addresses.
- `_safe_after_redirects` follows redirects manually and re-validates
  the final URL, defeating `https://attacker/redirect` →
  `http://10.0.0.5/...` tricks.
- `URL head` requests don't issue from the server itself; cover art is
  fetched with a cookieless session.

### Path traversal

A user-supplied output path tries to land downloads outside the
configured directory (`/etc/`, `~/../../tmp/x`).

Defenses:

- `_safe_output_path` strips NUL bytes, expands `~`, resolves `..` and
  symlinks, and rejects anything not writable.
- `sanitize_filename` strips `/`, `\\`, control bytes, etc. from track
  filenames.

### Update-tampering

A malicious release (compromised GitHub account, MITM in the future)
tries to ship a backdoored binary as Audio Converter.

Defenses:

- The bundled installer is published with `SHA256SUMS.txt`.
- `SHA256SUMS.txt` carries a minisign signature (`SHA256SUMS.txt.minisig`).
- The bundled minisign public key (`src/release-signing-pubkey.txt`)
  is verified before any install; the verifier refuses to install when
  the key is missing OR the signature fails OR the file is missing —
  silent downgrade to unverified checksum is impossible.

### Resource exhaustion

A runaway tab tries to flood the server with `/api/playable` polls.

Defenses:

- Per-IP rate limit on `/api/playable` (12/min/IP).
- Concurrent transcode cap (3 at a time); the 4th gets 503 "busy".
- Per-IP rate limit on uploads and helper installs (60/min/IP).
- 1 GB upload cap (Flask `MAX_CONTENT_LENGTH`).

### Misconfigured / expired secrets

- The session secret is persisted to a `0o600` file in the per-user
  data directory; if write fails, the user sees a banner instead of a
  silently-rotating key.
- Default-empty secret keys (`""`, `password`, `changeme`) are rejected.
- The pubkey for update verification is bundled; absence fails closed.

### Accidental destructive actions

A user clicks "Factory reset" thinking they're cancelling a download.

Defenses:

- Every destructive action requires typing a confirmation phrase
  (`CONFIRM` for most; `FACTORY` for full reset).
- The Settings UI gates these behind a typed-phrase modal.
- Toast banners confirm every state change.

### Local privilege escalation

A local user on the same machine tries to read the database, the secret
key, or downloaded files via the running app's defect.

Defenses:

- Server binds `127.0.0.1` only (`_local_host_only` Host guard refuses
  any other host header).
- The data directory is created with mode `0o700`.
- Secret key file is written with mode `0o600`.
- All state-changing endpoints require CSRF + same-origin.
- SQLAlchemy parameterizes all queries; no string concatenation.

## What we do NOT defend against

Be honest with your users. The following threats are *out of scope* for
this app:

1. **Compromise of the local machine.** If a local user has shell
   access, they can read the database directly. The app trusts the
   local user.
2. **Browser extensions with broad origin access.** They can call any
   GET / POST that the local user could. The typed-phrase confirmation
   makes destructive actions hard to silently abuse.
3. **YouTube / SoundCloud / Spotify / Apple Music / Tidal policy
   changes.** The app may stop downloading at any time. That's a
   service-side decision, not a bug.
4. **Compiled-binary signing on Apple Windows and notarization on
   macOS.** The app ships without paid Developer ID / Authenticode
   certs; first launch on a fresh install requires `Right-click › Open`
   to bypass Gatekeeper / SmartScreen warnings. This is documented
   explicitly in SECURITY.md.
5. **The contents of downloaded media.** The app never inspects,
   transcribes, or uploads audio / video. It is a local pipe.
6. **Compromise of upstream toolchains** (yt-dlp, FFmpeg, Flask).
   We trust the binary we downloaded, but a malicious upstream release
   would compromise the app. The release-signing pipeline assumes
   `yt-dlp` itself is honest.
7. **Long-running network exposure.** This app is *not* a server.
   Deploying it on a publicly reachable host without a TLS-terminating
   reverse proxy is unsupported and unrecommended. The codebase assumes
   `127.0.0.1` only.

## What's not in the codebase but could be

These were considered but deferred for the v1 line:

- **Compiled-binary code-signing.** Adding a Developer ID requires
  purchasing through Apple's Developer Program; SmartScreen requires
  an EV cert. The maintainer did not have these at v1 release.
- **Telemetry.** No usage data leaves the machine. No crash reports.
  No opt-in yet — left for a later release.
- **Content-hash deduplication.** Two identical tracks in the library
  occupy two slots. Deferred to a future optimization pass.

## Threat-model maintenance

- This file is reviewed on each hardening PR.
- Every change to the network surface, secrets handling, or
  destructive endpoints triggers a new "Defenses" section entry.
- Reviewers should ask: "Is the local-user-only assumption still
  valid? If not, what changes?"

## See also

- [SECURITY.md](../SECURITY.md) — disclosure policy and hardening
  summary.
- [README.md](../README.md) — install and run instructions.
- [docs/SECURITY.md](../SECURITY.md) — the public-facing security
  page referenced from the in-app "What's new" notes.