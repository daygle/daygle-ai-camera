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

## Offline camera alerts

Open **Settings** (`/settings`) to configure camera offline alert behavior. Offline alerts are useful when cameras are deployed remotely or when recordings are expected to be continuous.

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
- SQLite backups can be downloaded from **Settings** → **Database**.
- Restores create a safety backup of the current database before replacing it.

## Update checks

Admins can check for application updates from **Settings** → **Software Updates**. Service installs restart automatically after a successful browser-initiated update.
