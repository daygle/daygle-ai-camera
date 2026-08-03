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

Filter by camera ID, event type, or severity when investigating a specific stream. The newest diagnostic events are listed first.

## Settings layout

**Settings** (`/settings`) is organised into four tabs:

- **Detection & Live** - live refresh rates, detection interval, event merge window, background detection, and (under *Advanced Motion Tuning*) the low-level motion-gate values documented in [motion-detection.md](motion-detection.md).
- **Recording** - event clip timing (pre/post-event, keep-recording-after-motion, max clip length), retention/auto-purge, and storage directories.
- **Notifications** - push (ntfy), camera offline alerts, and email (SMTP) delivery, each with a test action.
- **System** - software updates, database backup/restore, login security, and the Danger Zone.

## Offline camera alerts

Open **Settings → Notifications → Camera Offline Notifications** to configure offline alert behavior. Offline alerts are useful when cameras are deployed remotely or when recordings are expected to be continuous. They can be delivered over both email and push channels, and the offline delay avoids alerts from brief connection blips.

## Application log

Open **Application Log** (`/application-log`) to follow the service journal in the browser. The viewer filters benign noise such as successful access requests and repeated sound detection notifications so the displayed entries stay focused on warnings, errors, and administrative events.

## Audit log

Open **Audit Log** (`/audit`) to review admin actions, including user creation, settings changes, login events, and system updates. The audit log is append-only and preserves a tamper-evident record of administrative activity.

## Recordings timeline

Use **Recordings Timeline** (`/recordings/timeline`) to view clip segments in a day-style timeline. Click a segment to play the associated recording directly from the dashboard.

## YAMNet TFLite status

Open **YAMNet TFLite** (`/yamnet-tflite`) to confirm whether the sound detection backend is available and the YAMNet assets have been downloaded. If the TensorFlow Lite runtime or model files are missing, the page reports the issue.

## Logs and backups

- Application logs are written to `data/logs/app.log` with rotation.
- SQLite backups can be downloaded from **Settings → System → Database Backup & Restore → Download Database Backup**.
- **Download Full Backup** in the same section produces a zip with the database, recordings, and snapshots. The database-only backup does **not** contain video; back up the `recordings/` and `snapshots/` directories separately (or use the full backup) if you need the media itself.
- Restores accept a previously downloaded `.sqlite` backup and create a safety backup of the current database before replacing it.

## Update checks

Admins can check for and apply application updates from **Settings → System → Software Updates**. The current version is shown at the top of the section. Service installs restart automatically after a successful browser-initiated update.

## Start Clean (Danger Zone)

**Settings → System → Danger Zone → Start Clean** deletes all **events, recordings, and alert history** so you can begin fresh. Settings, users, sessions, and alert rules are preserved. The action is irreversible and requires typing `START CLEAN` to confirm.
