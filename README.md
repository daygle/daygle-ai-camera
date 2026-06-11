# Daygle AI Camera

Daygle AI Camera is a self-hosted AI camera platform with a FastAPI backend, SQLite storage, session authentication, a browser dashboard, multi-camera ONVIF/RTSP support, monitoring zones, ONNX YOLO object detection, ANPR, push notifications, and an audit log.

The app is designed to be configured entirely from the web UI. `config.yaml` is only a small bootstrap file for settings the app must know before the database and dashboard can load.

## Features

- Protected browser dashboard with event search, alert history, recordings, playback, and object stats.
- First-run setup flow for creating the initial administrator.
- Admin/viewer roles with admin-only settings and user management.
- Profile page for timezone, date/time format, and password changes.
- **Multi-camera management**: add, configure, and monitor multiple RTSP/ONVIF cameras from the web UI.
- **Monitoring zones**: draw polygon zones on the live view and assign per-zone object rules, motion rules, cooldowns, email recipients, and ANPR areas.
- Web-managed AI settings for ONNX detection. Supports YOLOv8 Nano through Extra-Large (n/s/m/l/x).
- Modular ANPR/ALPR pipeline for vehicle detections, plate OCR, plate search, and plate alerts.
- Web-managed alert rules with optional SMTP email delivery and push notification delivery (ntfy-compatible).
- **Push notifications**: send alerts to any ntfy-compatible server with optional authentication and priority.
- Web-managed system settings for recording policy, retention, storage directories, and login security.
- SQLite persistence for events, detections, alerts, users, sessions, runtime settings, alert rules, and audit log.
- **Audit log**: tamper-evident log of admin actions (logins, settings changes, user management) available at `/audit`.
- **Over-the-air software updates**: check for new releases and apply updates directly from the browser settings page.
- **Database backup & restore**: download a SQLite backup or upload a previous backup to restore from the browser.
- **Timeline playback**: visual day-view timeline of recording clips by camera.
- Background live AI alert checks continue polling configured RTSP/ONVIF cameras even when no Live Cameras page is open.
- Debian install script plus a systemd unit for Linux server deployment.

## Requirements

### Local Development

- Python 3.10 or newer.
- `pip` and optionally `python3-venv`.
- A modern browser.
- Optional: an ONNX YOLO model file for object detection.
- EasyOCR for ANPR OCR (included in `requirements.txt`).

### Debian Server Deployment

- A Linux server running Debian or a Debian-like distribution.
- Root or `sudo` access. The service installer must be run with `sudo` because it installs packages, writes under `/opt` and `/etc`, and registers a systemd service.
- Network access during installation for `apt` and `pip`.
- Optional: HTTPS reverse proxy or VPN for devices exposed beyond a private LAN.

On a fresh Debian-like host:

```bash
apt update && apt install -y --no-install-recommends git python3 python3-pip python3-dev python3-venv sqlite3 ca-certificates rsync ffmpeg v4l-utils libgl1 libglib2.0-0
```

## Installation

### Local Install

1. Clone the repository and enter it:

   ```bash
   cd /opt/
   git clone https://github.com/daygle/daygle-ai-camera.git
   cd daygle-ai-camera
   ```

2. Create and activate a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   On Windows PowerShell:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```bash
   ./scripts/install_python_deps.sh python requirements.txt
   pip install --no-cache-dir pytest
   ```

   The helper defaults to CPU-only PyTorch and disables pip's download cache so local installs do not pull or duplicate large CUDA wheels such as `nvidia-*` packages. If you intentionally want pip's default PyTorch/CUDA resolution, run:

   ```bash
   DAYGLE_TORCH_VARIANT=default ./scripts/install_python_deps.sh python requirements.txt
   ```

   On Windows PowerShell, install the same CPU-first dependency set manually:

   ```powershell
   python -m pip install --no-cache-dir --upgrade pip wheel
   python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision
   python -m pip install --no-cache-dir -r requirements.txt pytest
   ```

   `pytest` is only needed for local test runs.

4. Create the minimal bootstrap config:

   ```bash
   cp config.example.yaml config.yaml
   ```

5. Start the web server:

   ```bash
   DAYGLE_CONFIG=config.yaml uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
   ```

6. Open <http://127.0.0.1:8080/>.

7. Complete first-run setup:

   - A fresh database redirects to `/setup`.
   - Create the first administrator account.
   - Sign in at `/login`.
   - Use the dashboard menu to configure the app from the browser.

### Service Install

Run the installer from the repository root with `sudo`:

```bash
sudo ./scripts/install_debian.sh
```

By default, the installer uses the same Python dependency helper as local installs: it installs CPU-only PyTorch first and passes `--no-cache-dir` to pip to reduce temporary disk pressure. Set `DAYGLE_TORCH_VARIANT=default` before running the installer only if you intentionally want pip to resolve the default PyTorch/CUDA wheels.

The installer will:

- Install required system packages.
- Create a `daygle` maintenance user unless `DAYGLE_USER` overrides it.
- Copy the app to `/opt/daygle-ai-camera` unless `DAYGLE_APP_DIR` overrides it.
- Create `/etc/daygle-ai-camera/config.yaml` unless `DAYGLE_CONFIG_DIR` overrides it.
- Store SQLite data and generated files under `/opt/daygle-ai-camera/data` unless `DAYGLE_DATA_DIR` overrides it.
- Create `/opt/daygle-ai-camera/models` for downloaded/exported ONNX/PT models and preserve existing `*.onnx`/`*.pt` files during reinstall.
- Install `daygle-ai-camera.service` as a root-running service with systemd write access to the config, data, and models directories.
- Keep app code/config root-owned while granting the `daygle` maintenance group write access to the data and models directories.
- Create and start `daygle-ai-camera.service`.

Check the service:

```bash
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Open:

```text
http://<server-ip>:8080/
```

Then create the first admin user and configure the app in the web UI.

## Minimal Bootstrap Config

`config.yaml` should stay small. Start from:

```yaml
server:
  host: 0.0.0.0
  port: 8080

auth:
  enabled: true
  cookie_name: daygle_session

storage:
  database: data/daygle_ai_camera.sqlite3
```

These values remain in YAML because they are needed before the web app can fully load:

- `server.host` and `server.port`: where Uvicorn listens.
- `auth.enabled`: whether auth middleware is enabled at startup.
- `auth.cookie_name`: session cookie name.
- `storage.database`: SQLite database path.

Most other settings are stored in SQLite through the web UI.

## Web Settings

Admins can access settings from the dashboard user menu.

### Profile

Route: `/profile`

- View username and role.
- Set timezone.
- Set date format.
- Set time format.
- Change password.

### Cameras

Route: `/cameras`

Add and manage multiple RTSP/ONVIF cameras. Each camera can have:

- Name, backend (`onvif` / `rtsp`), stream URL or ONVIF credentials, width, height, and FPS.
- Per-camera motion detection toggle and email notification toggle.
- Per-camera object detection and ANPR enable/disable.
- Per-camera recording policy (alert-triggered or continuous).

After saving cameras, configure per-camera monitoring areas from the Zones page.

### Zones

Route: `/zones`

Draw polygon monitoring zones directly on the live camera view. For each zone you can set:

- Zone name and enable/disable state.
- Motion monitoring: minimum confidence and cooldown.
- Object rules per label: enable/disable, record on detect, alert on detect, minimum confidence, cooldown, email recipients, and active time window.
- ANPR monitoring enable/disable.

Zone-based rules replace the global single-camera alert rules when zones are configured for a camera.

### AI Settings

Route: `/ai`

- AI enabled state.
- Backend: `onnx`.
- Confidence threshold, IOU threshold, and input size.
- Model path and labels path.
- Model selector: download and switch between YOLOv8 model sizes:
  - **YOLOv8n · Nano** (~6 MB) — fastest inference, lowest accuracy.
  - **YOLOv8s · Small** (~22 MB) — good balance of speed and accuracy.
  - **YOLOv8m · Medium** (~52 MB) — significantly better accuracy; recommended for IR/night-vision cameras.
  - **YOLOv8l · Large** (~87 MB) — high accuracy; requires a capable CPU or GPU.
  - **YOLOv8x · Extra Large** (~131 MB) — best accuracy; GPU strongly recommended.
- Check model, reload detector, test detector.
- Check for and apply model updates from the remote manifest.

AI settings are stored in SQLite under `app_settings.key = ai`.

### System Settings

Route: `/settings`

- Email delivery: SMTP host, port, username, password, from address, STARTTLS, and SSL.
- Push notifications: ntfy-compatible server URL, topic, priority, and optional username/password.
- ANPR: enable/disable, OCR backend, confidence threshold, and vehicle labels.
- Recording clips: pre-event seconds, post-event seconds, extension step, max clip seconds, and file format.
- Retention: auto purge, retention days, max storage GB, and manual purge.
- Storage: data, snapshots, events, recordings, and plates directories.
- Login security: session timeout, max login attempts, lockout minutes.
- Software updates: check for new releases and apply updates from GitHub directly from the browser.
- Database: download a SQLite backup or restore from a previously downloaded backup file.

Camera settings are now managed from the Cameras page (`/cameras`). Recording and alert-triggering are configured per camera and per zone.

#### P6S / ONVIF stream testing

Most ONVIF-compatible cameras expose the video itself as an RTSP stream. Open `/cameras`, add a camera, set **Camera backend** to `onvif / RTSP`, and either:

- paste the complete `stream_url`, for example `rtsp://username:password@192.168.1.50:554/stream1`; or
- enter `host`, `username`, `password`, optional `port` (default `554`), and `path` so Daygle can build the RTSP URL.

Then open `/live`; `/api/live/snapshot` will return a JPEG pulled from the stream.

### ANPR

Route: `/anpr`

ANPR runs as a separate pipeline after object detection:

```text
Camera/Image -> YOLO Object Detection -> Vehicle Detection -> Plate Detection -> Plate OCR -> Plate Event -> Search / Alerts
```

Vehicle labels are configurable and default to `car`, `truck`, `bus`, and `motorcycle`. When one is detected, the ANPR pipeline writes a plate crop artifact, runs OCR, stores a plate event, and links it back to the original event and any recording.

ANPR can be enabled or disabled per camera and per monitoring zone from the Cameras and Zones pages.

Supported OCR backends:

- `easyocr`: EasyOCR backend (default, included in `requirements.txt`).

Search examples:

```text
ABC123
1ABC2D
XYZ999
```

Plate alert examples:

- Specific plate: `ABC123`
- Unknown plate: any plate that is not whitelisted or blacklisted.
- Blacklisted plate: any plate marked blacklisted.

Admins can whitelist or blacklist plates and add notes such as `Family Car`, `Delivery Driver`, or `Blacklisted`.

### Audit Log

Route: `/audit`

The audit log records admin actions with timestamp, username, IP address, action type, resource, and outcome. Filterable by action, username, and resource. Useful for compliance and incident investigation.

## ONNX YOLO Setup

The default setup expects ONNX detection and a real ONVIF/RTSP camera.

To enable ONNX inference:

1. Sign in as an admin.
2. Open `/ai`.
3. Select a model size, then click **Download** to export it locally. YOLOv8n is the default starting point; YOLOv8m or larger is recommended for IR or night-vision cameras.
4. Save AI settings.
5. Use **Check model**, **Reload detector**, and **Test detector**.

If the model is missing, the dashboard reports `MODEL MISSING` and upload inference returns a clear API error.

Manual model export remains available. For local development, install the export dependencies first if they are not already present:

```bash
pip install ultralytics onnx
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
```

For service installs, the dashboard download/export writes to `/opt/daygle-ai-camera/models/`. The installer creates that directory, preserves existing `*.onnx` and `*.pt` files during reinstall, and allows the root-running service to write there.

## Users and Roles

First setup:

1. Start with an empty SQLite database.
2. Browse to `/setup` or `/`.
3. Create the first `admin` user.
4. After any user exists, `/setup` is disabled and redirects to `/login`.

Admins can open `/users` to create users, disable users, change roles, and reset passwords.

Roles:

- `admin`: dashboard, events, alerts, recordings, cameras, zones, users, profile, AI settings, system settings, and audit log.
- `viewer`: dashboard, events, alert history, recordings, and profile.

Password policy requires at least 8 characters with uppercase, lowercase, numeric, and symbol characters.

## Important Routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET/POST` | `/setup` | First admin setup |
| `GET/POST` | `/login` | Login |
| `GET/POST` | `/logout` | End session |
| `GET` | `/` | Dashboard |
| `GET` | `/live` | Live camera view |
| `GET` | `/cameras` | Admin camera management |
| `GET` | `/zones` | Admin monitoring zone editor |
| `GET` | `/profile` | Current user profile |
| `GET` | `/users` | Admin user management |
| `GET` | `/ai` | Admin AI settings |
| `GET` | `/settings` | Admin system settings (email, push, recording, storage, updates, backup) |
| `GET` | `/anpr` | Plate search, history, details, and alert rules |
| `GET` | `/audit` | Admin audit log |
| `GET` | `/recordings` | Recordings list |
| `GET` | `/recordings/timeline` | Day-view recording timeline |
| `GET` | `/api/auth/me` | Current user, CSRF token, and session expiry |
| `PUT` | `/api/profile` | Update profile preferences |
| `POST` | `/api/profile/password` | Change current user's password |
| `GET` | `/api/status` | Camera and detector status |
| `GET` | `/api/status/ai` | Detailed AI status |
| `GET` | `/api/live/snapshot` | Live ONVIF/RTSP JPEG snapshot |
| `GET` | `/api/events` | List/search events |
| `GET` | `/api/alerts` | Alert history |
| `GET` | `/api/cameras` | List cameras |
| `PUT` | `/api/cameras` | Admin update all cameras |
| `PUT` | `/api/cameras/{camera_id}` | Admin update a single camera |
| `GET` | `/api/recordings` | List recordings |
| `GET` | `/api/recordings/{id}/stream` | Stream recording media |
| `POST` | `/api/recordings/purge` | Admin purge using retention settings |
| `GET` | `/api/plates` | Recent vehicle plates |
| `GET` | `/api/plates/{id}` | Plate details and history |
| `GET` | `/api/plates/search` | Search plate events |
| `POST` | `/api/plates/whitelist` | Admin whitelist plate |
| `POST` | `/api/plates/blacklist` | Admin blacklist plate |
| `GET/POST` | `/api/plate-alerts` | View/create plate alert rules |
| `PUT/DELETE` | `/api/plate-alerts/{id}` | Edit/delete plate alert rules |
| `GET/POST` | `/api/users` | Admin list/create users |
| `PATCH` | `/api/users/{id}` | Admin role/status/password updates |
| `GET/PUT` | `/api/settings/ai` | Admin AI settings |
| `GET` | `/api/settings/ai/models` | List available YOLO models and install status |
| `POST` | `/api/settings/ai/download-model` | Download and export a YOLO model |
| `GET` | `/api/settings/ai/check-model-updates` | Check for model updates |
| `POST` | `/api/settings/ai/update-model` | Update an installed model |
| `POST` | `/api/settings/ai/reload` | Reload the active detector |
| `POST` | `/api/settings/ai/test-detector` | Test inference on a blank image |
| `GET/PUT` | `/api/settings/alert-email` | Admin SMTP email settings |
| `POST` | `/api/settings/alert-email/test` | Send a test email |
| `GET/PUT` | `/api/settings/alert-push` | Admin push notification settings |
| `POST` | `/api/settings/alert-push/test` | Send a test push notification |
| `GET` | `/api/settings/system` | Admin system settings summary |
| `GET` | `/api/settings/system/database/backup` | Download SQLite backup |
| `POST` | `/api/settings/system/database/restore` | Restore from a SQLite backup |
| `PUT` | `/api/settings/system/camera` | Admin single-camera settings (legacy) |
| `GET/PUT` | `/api/settings/anpr` | Admin ANPR settings |
| `PUT` | `/api/settings/system/live` | Admin live detection settings |
| `PUT` | `/api/settings/system/recording` | Admin recording settings |
| `PUT` | `/api/settings/system/storage` | Admin storage settings |
| `PUT` | `/api/settings/system/auth` | Admin login security settings |
| `GET` | `/api/update/check` | Check for a new software release |
| `POST` | `/api/update/apply` | Apply a software update via git pull |
| `GET` | `/api/audit` | Admin audit log entries |

## Database

Tables are created automatically at startup. Runtime settings are stored in `app_settings` as JSON values.

Core tables:

- `users`
- `user_sessions`
- `login_attempts`
- `app_settings`
- `events`
- `detections`
- `alert_history`
- `recordings`
- `vehicle_plates`
- `plate_events`
- `plate_alert_rules`
- `audit_log`

Useful `app_settings` keys:

- `ai`
- `alert_email`
- `alert_push`
- `camera`
- `cameras`
- `anpr`
- `live`
- `recording`
- `storage`
- `auth`

## Recording and Playback

The recording layer creates event-linked clips and dashboard playback wiring for live camera detections and uploaded-image tests.

Recording policy is managed per camera from the Cameras page (`/cameras`) and per zone from the Zones page (`/zones`):

- **Alert-triggered**: record when an alert rule matches (default for new cameras).
- **Continuous**: record every detection event.

Global clip settings are managed at `/settings`:

- `pre_event_seconds`: seconds of pre-buffer captured before the event.
- `post_event_seconds`: seconds to continue recording after the event.
- `extension_step_seconds`: seconds to extend an active recording when a new event arrives during the post-event window.
- `max_clip_seconds`: maximum clip length.

Retention is managed on the same page:

- `auto_purge_enabled`: purge on recording creation.
- `retention_days`: delete clips older than this age.
- `max_storage_gb`: delete oldest clips until the recording folder is under the cap.
- **Purge now**: run retention purge manually.

### Timeline

The Timeline page (`/recordings/timeline`) shows a visual day-view of recording clips per camera. Click a coloured segment to open and play that clip.

Fresh installs use the current schema directly. The app does not carry migration code for older local development databases; delete the old SQLite file when schema-breaking changes are made during development.

Playback routes:

| Method | Route | Role |
| --- | --- | --- |
| `GET` | `/api/recordings` | admin/viewer |
| `GET` | `/api/recordings/{recording_id}` | admin/viewer |
| `GET` | `/api/recordings/{recording_id}/stream` | admin/viewer |
| `POST` | `/api/recordings/purge` | admin |
| `DELETE` | `/api/recordings/{recording_id}` | admin |

Current limitations:

- Uploaded-image events create generated footage, not real camera footage.
- RTSP/ONVIF camera events create linked recording artifacts from the live event path.
- Retention runs when clips are created or when an admin clicks **Purge now**; there is no background scheduler yet.

## Push Notifications

Daygle supports sending alert notifications to any ntfy-compatible push notification server. Configure the server URL, topic, optional credentials, and priority from `/settings` under **Push Notifications**.

To test your configuration, click **Send Test Notification**. Notifications include the detected label, confidence score, camera name, and alert rule name.

## Software Updates

Admins can check for new releases and apply updates without SSH access:

1. Open `/settings` → **Software Updates**.
2. Click **Check for Updates** to compare the current version against the latest GitHub release.
3. If an update is available, click **Apply Update** to pull the latest code and reinstall dependencies.
4. For service installs, the service restarts automatically after a successful update.

The update mechanism runs `scripts/update.sh`, which does a `git pull` and `pip install -r requirements.txt` in the app directory.

## Database Backup and Restore

The SQLite database contains all events, detections, recordings metadata, settings, users, alert rules, and audit history.

- **Backup**: open `/settings` → **Database** → **Download Backup** to download a timestamped SQLite file.
- **Restore**: upload a previously downloaded backup to replace the live database. A safety backup of the current database is created automatically before the restore completes.

## ANPR Limitations

- Plate crop extraction is a modular placeholder in uploaded-image workflows; camera backends can replace it with real image crops.
- EasyOCR is included as a dependency and may require additional platform packages on some systems.
- Plate alerts are stored and matched in-process for cooldown behavior; persistent alert history can be added later if needed.

## Updating an Existing Install

### Local

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Install `pytest` too if you run tests locally:

```bash
pip install pytest
```

### Service Install

```bash
sudo ./scripts/install_debian.sh
sudo systemctl restart daygle-ai-camera
```

The installers preserve existing config and downloaded/exported `models/*.onnx` and `models/*.pt` files unless you remove or replace them manually.

## Tests

```bash
python -m compileall app
python -m pytest
```

## Troubleshooting

- **Cannot log in after first start**: open `/setup` and create the initial admin user.
- **Setup redirects to login**: a user already exists; sign in with an admin account.
- **Dashboard shows MODEL MISSING**: open `/ai`, download a YOLO model or set a readable model path, then reload the detector.
- **ONNX fails to load**: confirm model and labels paths are readable and ONNX Runtime is installed.
- **Email alerts do not send**: open `/settings`, check SMTP host/port/auth/from address, and confirm the alert rule has email enabled and recipients. Confirm **Background detection** is enabled in Live settings so rules continue checking when the Live Cameras page is closed.
- **Push notifications not arriving**: open `/settings` → **Push Notifications**, verify server URL and topic, then send a test notification.
- **Camera not connecting**: open `/cameras`, check the stream URL or ONVIF credentials, and verify the camera is reachable on the network.
- **Service cannot write data or models**: check storage paths in `/settings`, then verify `/opt/daygle-ai-camera/models` and the configured data directory exist. The installer runs the systemd service as root and grants write access to config, data, and models paths.
- **Need service logs**: run `sudo journalctl -u daygle-ai-camera -f`.
- **Need application logs**: the app writes rotating logs to `data/logs/app.log` (up to 10 MB × 5 files).
