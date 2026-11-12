# Security Policy

Daygle AI Camera is a self-hosted application; most deployments are on a
private LAN or behind a Cloudflare Tunnel / reverse proxy. Security depends on
both the application code and how you expose it to the network, so please
include deployment context when reporting.

## RTSP camera network isolation

Only connect to trusted camera servers. An RTSP server can redirect FFmpeg or
OpenCV to a different host or port; FFmpeg offers no option to disable RTSP
redirects (`-max_redirects` is an HTTP-only option), and the stream protocol
allowlist does not prevent a redirect to another RTSP server. To close that
SSRF pivot in code, every live RTSP connection in this application — the
shared ingest/prebuffer, continuous and event-clip recording, RTSP sound
capture, the OpenCV capture, and the camera connection test — is routed
through the loopback broker in `app/rtsp_broker.py`. The broker re-resolves
the configured camera host on each new connection (falling back to the
last-known-good addresses on transient DNS failure, so DHCP address changes
are picked up on reconnect), only ever connects to those resolved addresses,
and drops any 3xx redirect whose `Location` changes the scheme, host, or
port; same-authority redirects are rewritten back to the broker. Capture
fails closed if the broker cannot start. As defence in depth, also apply
outbound network restrictions to the service and its child processes so they
can reach only the camera endpoints and other services the application
needs. Account for DNS and required application traffic when configuring
those rules; private LAN addresses are not inherently unsafe. Keep both the
system FFmpeg/ffprobe and OpenCV's bundled FFmpeg updated. The FFmpeg fixes
for CVE-2026-107698 reject non-RTSP redirect schemes, but do not prevent
redirects to another RTSP server. FFmpeg through 9.0.2 is also affected by
the redirect-loop issue CVE-2026-107696; existing worker timeouts limit some
loops, but are not a network security boundary. OpenCV and direct sound
capture are separate paths and may use different FFmpeg builds.

## Supported versions

Only the **latest tagged release** is supported. Releases are published as
`v1.0.x` git tags, and updates are normally applied through the in-app updater
(Settings → Maintenance → Software Updates) or `scripts/update.sh`. Fixes are not
backported to older releases.

If you are on an older version, update before reporting: the issue you found
may already be fixed.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.** Please use
GitHub's **private vulnerability reporting** instead:

1. Open <https://github.com/daygle/daygle-ai-camera/security/advisories/new>.
2. Describe the vulnerability, including a proof of concept where possible.

Reports are acknowledged within **2 business days**, triaged for severity, and
you will be kept informed as a fix is prepared. We aim to release fixes as soon
as a patch is ready and to coordinate public disclosure with you.

### What to include

To help reproduce and triage the issue, please include:

- The affected version - the git tag, or the output of
  `git describe --tags --abbrev=0` / the version shown in Settings → Maintenance.
- Deployment type: Debian service install, Docker-style/dev clone, or local
  development, and whether it is exposed via LAN, a reverse proxy, or Cloudflare
  Tunnel.
- Camera make/model and firmware version if the issue involves a camera,
  ONVIF, RTSP, or audio.
- Steps to reproduce, relevant configuration (redact secrets), and any logs
  from `data/logs/app.log` or `journalctl -u daygle-ai-camera`.

### Scope

Security reports are welcome for the application code in this repository. Note
that many findings on a self-hosted system are deployment issues rather than
code bugs - for example exposing the dashboard without authentication or a
reverse proxy. When in doubt, report it anyway.

## Disclosure

We practice coordinated disclosure: fixes are released (and, where appropriate,
assigned a CVE) before details are made public. Please allow time for a fix to
ship before discussing the vulnerability publicly.
