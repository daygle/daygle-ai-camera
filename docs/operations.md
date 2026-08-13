# Operations Guide

This guide summarizes the admin pages and operational checks that help keep Daygle AI Camera healthy after installation.

## Camera health

Use **Cameras** (`/cameras`) to review each configured camera and the camera health summary. The health endpoint tracks online and offline state so administrators can quickly identify streams that need attention.

Use the camera connection test before saving a new stream URL or ONVIF configuration. If a camera supports PTZ, enable PTZ in the camera editor and verify the protocol, port, address, and speed before sending movement commands.

## Camera log

Use **Camera Log** (`/camera-log`) to investigate operational issues. The log includes:

- `camera_offline` and `camera_online` transitions.
- `detection_backoff` and `detection_recovered` events.
- `capture_failed` events.
- `prebuffer_fallback`, `prebuffer_short_preroll`, `prebuffer_degenerate`, and `prebuffer_restart` recording events. A `prebuffer_short_preroll` warning means an event clip captured less pre-event footage than configured because the rolling buffer had not filled yet (common right after saving recording settings, a camera reconnect, or the camera first coming online); it recovers on its own once the buffer refills.
- `audio_mux_disk_full` warnings mean an event clip was saved **without audio** because the recordings filesystem ran out of space while the audio mux was being prepared or written. The video is kept; free up space on the recordings drive and future clips get sound again. While the disk stays full the diagnostic is emitted at most once per camera per 30 minutes.

Filter by camera ID, event type, or severity when investigating a specific stream. The newest diagnostic events are listed first.

## Settings layout

**Settings** (`/settings`) is organised into four tabs:

- **Detection & Live** - live refresh rates, detection interval, event merge window, background detection, and (under *Advanced Motion Tuning*) the low-level motion-gate values documented in [motion-detection.md](motion-detection.md).
- **Recording** - event clip timing (pre/post-event, keep-recording-after-motion, max clip length), retention/auto-purge, and storage directories.
- **Zones** - draw zones directly in the zone editor with **Add Zone**, manage zone rules, and use the per-zone visibility controls to show or hide overlays while configuring the scene.
- **Notifications** - push (ntfy), camera offline alerts, and email (SMTP) delivery, each with a test action.
- **System** - software updates, Cloudflare Tunnel, database backup/restore, login security, and the Danger Zone. Software Updates appears above Cloudflare Tunnel; the tunnel card shows whether the service is running, stopped, unconfigured, or needs attention.

## Cloudflare Tunnel

Open **Settings → System → Cloudflare Tunnel** to manage the optional built-in `cloudflared` connector. Paste a token from Cloudflare Zero Trust and save it; the token field is cleared after saving and the **Saved securely** indicator confirms that a token is present without exposing it. The card also shows the connector state and provides **Start Tunnel**, **Stop Tunnel**, and **Restart Tunnel** actions.

A token saved through the UI is stored in a protected file next to the SQLite database, while SQLite and the status API contain only non-secret metadata. A token supplied through `DAYGLE_CLOUDFLARED_TOKEN` or bootstrap configuration is labelled as externally configured and is not necessarily stored in that file. If the connector exits, the status card reports the problem while the local Daygle service remains available.

For headless deployments, prefer a protected systemd environment/drop-in for `DAYGLE_CLOUDFLARED_TOKEN`. The environment token takes precedence over a token saved in the UI. See the [README Cloudflare Tunnel section](../README.md#remote-access-with-cloudflare-tunnel) for Cloudflare Zero Trust and Access setup.

## Offline camera alerts

Open **Settings → Notifications → Camera Offline Notifications** to configure offline alert behavior. Offline alerts are useful when cameras are deployed remotely or when recordings are expected to be continuous. They can be delivered over both email and push channels, and the offline delay avoids alerts from brief connection blips.

## Application log

Open **Application Log** (`/application-log`) to follow the service journal in the browser. The viewer filters benign noise such as successful access requests and repeated sound detection notifications so the displayed entries stay focused on warnings, errors, and administrative events.

## Audit log

Open **Audit Log** (`/audit`) to review admin actions, including user creation, settings changes, login events, and system updates. The audit log is append-only and preserves a tamper-evident record of administrative activity.

## Events and recordings

Use **Events** (`/events`) as the single activity feed for object, motion, and sound detections. Filter by event type and time range. Alerted events show a notification badge; when available, **Snapshot** opens an annotated still image and **Recording** opens the linked clip.

A single scene can produce multiple event rows in one recording. Use **Recordings** (`/recordings`) for clip search, playback, download, and deletion, or **Recordings Timeline** (`/recordings/timeline`) to view clips across a day. The inline player can show object overlays and detection details.

## Recordings timeline

Use **Recordings Timeline** (`/recordings/timeline`) to view clip segments in a day-style timeline. Click a segment to play the associated recording directly from the dashboard.

## YAMNet TFLite status

Open **YAMNet TFLite** (`/yamnet-tflite`) to confirm whether the sound detection backend is available and the YAMNet assets have been downloaded. If the TensorFlow Lite runtime or model files are missing, the page reports the issue.

## Logs and backups

- Application logs are written to `data/logs/app.log` with rotation.
- SQLite backups can be downloaded from **Settings → System → Database Backup & Restore → Download Database Backup**.
- **Download Full Backup** in the same section produces a zip with the database, recordings, snapshots, legacy event artifacts, and installed model assets. The database-only backup does **not** contain media; use the full backup when you need a portable recovery bundle.
- Restores accept either a previously downloaded `.sqlite` database backup or a `.zip` full backup. Full restores validate the archive, protect against traversal and symbolic links, remap media paths to the current storage directories, restore media/models, and create a full safety backup first.
- Configuration supplied through environment variables, external `config.yaml` files, protected secret files (including the Cloudflare Tunnel token), and external service credentials must be configured separately after recovery. These are intentionally not copied into a downloadable archive.

## Update checks

Admins can check for and apply application updates from **Settings → System → Software Updates**. The current version is shown at the top of the section. The updater verifies the canonical repository origin, refreshes Python dependencies, provisions `cloudflared`, and can migrate the systemd launcher to `python -m app.server`. Service installs schedule a restart after a successful browser-initiated update when permissions allow; otherwise restart the service manually.

For manual service updates, run `./scripts/update.sh` from the configured application directory (the default is `/opt/daygle-ai-camera`), then restart `daygle-ai-camera` if it was not restarted automatically. The updater can install `cloudflared` system-wide or fall back to the application virtual environment for unprivileged GUI updates.

## Operating system updates

The in-app updater only updates the application. The underlying Debian host should be patched separately with `unattended-upgrades`, which installs security and point-release package updates on a daily schedule. Commands below assume a root shell.

```bash
apt-get update
apt-get install -y unattended-upgrades

tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::Unattended-Upgrade "1";
// AutocleanInterval 7 = run `apt-get autoclean` weekly to trim the .deb cache.
APT::Periodic::AutocleanInterval "7";
EOF
```

Replace `/etc/apt/apt.conf.d/50unattended-upgrades` with a Debian-only origins pattern and conservative defaults:

```bash
tee /etc/apt/apt.conf.d/50unattended-upgrades >/dev/null <<'EOF'
// Debian archives only - third-party/vendor repos are never auto-upgraded.
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian";
    "origin=Debian,codename=${distro_codename}-updates";
    "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};

Unattended-Upgrade::Package-Blacklist {
};

// Email only when an upgrade fails ("always" and "on-change" are louder).
Unattended-Upgrade::Mail "root";
Unattended-Upgrade::MailReport "only-on-error";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
Unattended-Upgrade::Remove-Unused-Dependencies "false";
// Reboot automatically, but only when an update requires it, at this local time.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
// Reboot even if a user is logged in (default true; false skips the reboot).
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
EOF
```

Validate with `unattended-upgrade --dry-run --debug`.

Packages that must never be auto-upgraded (on GPU boxes, the NVIDIA driver and the kernel - see [tesla-p4-gpu-setup.md](tesla-p4-gpu-setup.md)) are protected with `apt-mark hold`, which `unattended-upgrades` respects. A hold is stronger than a `Package-Blacklist` entry because it also blocks a manual `apt upgrade`.

### Email upgrade reports via msmtp

`Unattended-Upgrade::Mail` sends nothing unless the host can deliver mail. `msmtp` relays the report through your own SMTP server without a full MTA:

```bash
apt-get install -y --no-install-recommends msmtp msmtp-mta mailutils
```

Write `/etc/msmtprc` (mode `600`) with your server, credentials, and envelope sender (the `example.com` values below are placeholders):

```bash
tee /etc/msmtprc >/dev/null <<'EOF'
defaults
auth           on
tls            on
tls_starttls   on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        relay
host           mail.example.com
port           587
from           notifications@example.com
user           notifications@example.com
password       REPLACE_WITH_PASSWORD_OR_APP_TOKEN

account default : relay
EOF

chmod 600 /etc/msmtprc
touch /var/log/msmtp.log && chmod 600 /var/log/msmtp.log
```

Adjust the TLS lines to the server: port 465 uses implicit TLS (`tls on`, `tls_starttls off`), a trusted-LAN relay uses `tls off`, and a self-signed certificate uses `tls_fingerprint <SHA256>`. Then set the visible `From:` header and point `unattended-upgrades` at the recipient:

```bash
sh -c 'echo "set from=notifications@example.com" > /etc/mail.rc'
sed -i 's|^Unattended-Upgrade::Mail "root";|Unattended-Upgrade::Mail "admin@example.com";|' /etc/apt/apt.conf.d/50unattended-upgrades
```

Test the relay directly (`--debug` prints the SMTP conversation), then through the `mailx` path that `unattended-upgrades` uses:

```bash
printf 'Subject: unattended-upgrades test\n\nThis is a test.\n' | msmtp --debug admin@example.com
printf 'mailx test body\n' | mailx -s 'mailx test' admin@example.com
```

`/var/log/msmtp.log` records every send.

## Memory tuning

The detector keeps model weights and frame buffers in RAM while ffmpeg streams
recording buffers, so the default Debian `vm.swappiness` of 60 can swap out warm
process memory under pressure, causing stutter or slower detection. Lower it so
the kernel prefers keeping running processes in RAM:

```bash
printf 'vm.swappiness=10\nvm.vfs_cache_pressure=50\n' > /etc/sysctl.d/99-swappiness.conf
sysctl --system
```

`vm.swappiness=10` swaps only under real memory pressure - avoid `0`, which can
starve swap and trigger the OOM killer instead. `vm.vfs_cache_pressure=50` keeps
the directory/inode cache slightly longer, which helps a filesystem that is
constantly creating recording files. Verify with
`cat /proc/sys/vm/swappiness` and `free -h`.

## Start Clean (Danger Zone)

**Settings → System → Danger Zone → Start Clean** deletes all **events, recordings, and alert history** so you can begin fresh. Settings, users, sessions, and alert rules are preserved. The action is irreversible and requires typing `START CLEAN` to confirm.
