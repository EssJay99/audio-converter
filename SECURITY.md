# Security Policy

The YouTube / SoundCloud Audio Converter is a single-user desktop app: the
Flask server binds to `127.0.0.1` only, ships with CSRF protection on every
state-changing request, and stores your downloads + history in a per-user
data directory. The threat model for v1 is *local, single-trust* — you are
the only network principal, and the protections below defend against a
malicious web page in another tab.

If you discover a vulnerability, please follow the responsible-disclosure
process below. **Do not file a public GitHub issue for security bugs.**

## Supported versions

| Version | Supported |
| ------- | --------- |
| latest release on the `main` branch | yes |
| any older release | no — please upgrade |

Only the most recent tagged release receives security fixes. We do not
backport patches to older versions.

## Reporting a vulnerability

**Channel:** GitHub Security Advisories on this repository
(<https://github.com/EssJay99/audio-converter/security/advisories/new>).
This routes the report to the maintainers *privately* and lets us
coordinate a fix before public disclosure.

**What to include:**

- A clear description of the issue and the impact you observed.
- A reproducer (URL, command, screenshot, or payload).
- The build you tested against (commit hash, release tag, OS, Python
  version).
- Your handle / contact for follow-up questions.

**Response targets:**

- Initial acknowledgement: within **3 business days**.
- Triage + impact assessment: within **10 business days**.
- Fix and disclosure: usually within **30 days**, depending on severity
  and coordination with downstream projects (e.g. yt-dlp, Flask).

We follow coordinated disclosure. Once a fix lands we publish a GitHub
Security Advisory with the CVE assigned by GitHub and credit the finder
in `THANKS.md` unless they prefer to remain anonymous.

## What is in scope

Anything that lets a third party **read your local files, write to your
local files, run code on your machine, or bypass the app's own
controls** despite the protections listed below. Examples:

- A URL that, when submitted to *Convert*, escapes the listener sandbox
  and reaches `127.0.0.1`, `localhost`, or a private RFC1918 host
  (SSRF).
- A CSRF bypass — a request from another origin that successfully
  changes state (download, delete, factory reset, settings save).
- A path-traversal bug — a video URL or filename that escapes the
  configured output folder.
- An update payload that executes without integrity verification, or
  fails to verify a SHA256 / signature match.
- A weak default in the secret key, database, or settings that lets a
  local attacker impersonate the user.

## What is out of scope

- YouTube, SoundCloud, Spotify, Apple Music, or Tidal rate-limiting /
  blocking. Those services can change their terms at any time; if a
  download fails because the upstream service blocked it, that's their
  policy, not a security bug.
- The contents of downloaded media. We never inspect, transcribe, or
  upload audio; the app is a local pipe.
- Bugs that require the attacker to already have shell access to the
  host. The app trusts the local user.
- Compiled-binary release issues that require code-signing / notarization
  on macOS or Windows; we ship builds without paid certificates, so
  Gatekeeper / SmartScreen warnings on first launch are expected. See
  `docs/SECURITY.md` for what we do and don't sign.

## Hardening this release

A non-exhaustive list of the controls in the v1.0.x line. These are
defense-in-depth measures — the local-only bind + CSRF + same-origin
check are the primary defenses.

- Server binds `127.0.0.1` only; never reachable from the LAN without a
  deliberate proxy.
- CSRF tokens on every state-changing request (`flask_wtf.CSRFProtect`).
- `SameSite=Lax` + `HttpOnly` session cookies.
- `_same_origin_required` on local-side GETs that mutate state (e.g.
  notifications, proxy-check).
- Per-IP rate limits on the heaviest endpoints (`/api/playable/<id>`,
  helper installs, uploads).
- SHA-256 verification on every downloaded helper binary (yt-dlp,
  ffmpeg) and on every self-update payload.
- URL allowlist + SSRF guard (`_is_public_http_url`) for user-submitted
  URLs.
- Path-traversal guard (`_safe_output_path`) on user-supplied output
  paths.
- On macOS, success lifts a `codesign --verify --deep --strict` banner
  warning when the running bundle is not signed, so users know what to
  expect.
- Database writes are SQL-parameterized via SQLAlchemy; no string
  concatenation.

## Acknowledgements

Researchers who have reported issues and agreed to be credited are
listed in `THANKS.md`.

## Changes to this policy

This file may be edited as the project evolves. The current policy is
the authoritative version; older commits remain in git history.