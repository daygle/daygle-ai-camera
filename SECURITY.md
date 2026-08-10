# Security Policy

Daygle AI Camera is a self-hosted application; most deployments are on a
private LAN or behind a Cloudflare Tunnel / reverse proxy. Security depends on
both the application code and how you expose it to the network, so please
include deployment context when reporting.

## Supported versions

Only the **latest tagged release** is supported. Releases are published as
`v1.0.x` git tags, and updates are normally applied through the in-app updater
(Settings → System → Software Updates) or `scripts/update.sh`. Fixes are not
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

- The affected version — the git tag, or the output of
  `git describe --tags --abbrev=0` / the version shown in Settings → System.
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
code bugs — for example exposing the dashboard without authentication or a
reverse proxy. When in doubt, report it anyway.

## Disclosure

We practice coordinated disclosure: fixes are released (and, where appropriate,
assigned a CVE) before details are made public. Please allow time for a fix to
ship before discussing the vulnerability publicly.
