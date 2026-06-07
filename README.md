# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B / Armbian-friendly AI camera platform with a FastAPI backend, SQLite storage, session authentication, a browser dashboard, ONVIF/RTSP camera support, and ONNX YOLO object detection.

The app is now designed to be configured from the web UI. `config.yaml` is only a small bootstrap file for settings the app must know before the database and dashboard can load.

## Features

- Protected browser dashboard with event search, alert history, recordings, playback, and object stats.
- First-run setup flow for creating the initial administrator.
- Admin/viewer roles with admin-only settings and user management.
- Profile page for timezone, date/time format, and password changes.
- Web-managed AI settings for ONNX detection.
- Modular ANPR/ALPR pipeline for vehicle detections, plate OCR, plate search, and plate alerts.
- Web-managed alert rules with optional SMTP email delivery.
- Web-managed system settings for camera, recording policy, retention, storage directories, and login security.
- SQLite persistence for events, detections, alerts, users, sessions, runtime settings, and alert rules.
- ONVIF/RTSP live snapshot support for testing P6S-style IP cameras before CSI camera hardware arrives.
- Background live AI alert checks continue polling configured RTSP/ONVIF cameras even when no Live Cameras page is open.
- Debian and Armbian install scripts plus a systemd unit for Linux/Orange Pi deployment.

## Requirements

### Local Development

- Python 3.10 or newer.
- `pip` and optionally `python3-venv`.
- A modern browser.
- Optional: an ONNX YOLO model file for object detection.
- Optional: PaddleOCR or EasyOCR for ANPR OCR.

### Debian / Orange Pi / Armbian Deployment

- Orange Pi 3B or another Linux host running Debian/Armbian-like packages.
- Root or `sudo` access. Both service installers must be run with `sudo` because they install packages, write under `/opt` and `/etc`, and register a systemd service.
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

   On Windows PowerShell:

   ```powershell
   $env:DAYGLE_CONFIG="config.yaml"
   python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
   ```

6. Open <http://127.0.0.1:8080/>.

7. Complete first-run setup:

   - A fresh database redirects to `/setup`.
   - Create the first administrator account.
   - Sign in at `/login`.
   - Use the dashboard menu to configure the app from the browser.

### Service Install

Run the appropriate installer from the repository root. Both installers must be run as root via `sudo`:

```bash
# Debian 13 / generic Debian host
sudo ./scripts/install_debian.sh

# Armbian / Orange Pi host
sudo ./scripts/install_armbian.sh
```

By default, the installers use the same Python dependency helper as local installs: it installs CPU-only PyTorch first and passes `--no-cache-dir` to pip to reduce temporary disk pressure. Set `DAYGLE_TORCH_VARIANT=default` before running an installer only if you intentionally want pip to resolve the default PyTorch/CUDA wheels.

The installers will:

- Install required system packages.
- Create a `daygle` maintenance user unless `DAYGLE_USER` overrides it.
- Copy the app to `/opt/daygle-ai-camera` unless `DAYGLE_APP_DIR` overrides it.
- Create `/etc/daygle-ai-camera/config.yaml` unless `DAYGLE_CONFIG_DIR` overrides it.
- Store SQLite data and generated files under the configured data directory: `/opt/daygle-ai-camera/data` for Debian unless `DAYGLE_DATA_DIR` overrides it, and `/var/lib/daygle-ai-camera` for Armbian unless `DAYGLE_DATA_DIR` overrides it.
- Create `/opt/daygle-ai-camera/models` for downloaded/exported ONNX/PT models and preserve existing `*.onnx`/`*.pt` files during reinstall.
- Install `daygle-ai-camera.service` as a root-running service for camera hardware/device access, with systemd write access to the config, data, and models directories.
- Keep app code/config root-owned while granting the `daygle` maintenance group write access to the data and models directories.
- Create and start `daygle-ai-camera.service`.

Check the service:

```bash
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Open:

```text
http://<orange-pi-ip>:8080/
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

### AI Settings

Route: `/ai` (legacy `/settings` also works)

- AI enabled state.
- Backend: `onnx`.
- Confidence threshold.
- IOU threshold.
- Input size.
- Model path.
- Labels path.
- Check model.
- Download YOLOv8n ONNX (exports locally from `yolov8n.pt`).
- Reload detector.
- Test detector.

AI settings are stored in SQLite under `app_settings.key = ai`.

### Alert Settings

Route: `/alert-settings`

- Configure SMTP host, port, username, password, from address, STARTTLS, and SSL.
- Create, edit, delete, enable, or disable alert rules.
- Set object label, minimum confidence, cooldown, and active time window.
- Enable email delivery per rule.
- Add email recipients per rule, for example cat detected -> email a user.

SMTP settings are stored in SQLite under `app_settings.key = alert_email`. Alert rules are stored in `alert_rules`.

### System Settings

Route: `/settings` (legacy `/system-settings` also works)

- Camera: backend (`onvif` or `rtsp`), device label, stream URL or ONVIF host credentials, width, height, FPS, flip.
- ANPR: enable/disable, OCR backend, confidence threshold, and vehicle labels.
- Recording policy: continuous recording, record on motion, record on human, and selected object labels such as `cat`, `dog`, `package`, and `parcel`.
- Clip settings: pre-event seconds, post-event seconds, max clip seconds, and file format.
- Retention: auto purge, retention days, max storage GB, and manual purge.
- Storage: data, snapshots, events, and recordings directories.
- Login security: session timeout, max login attempts, lockout minutes.

Camera, ANPR, recording, storage, and login security settings are stored in SQLite and applied at runtime where possible. Database path, auth enablement, cookie name, and server bind settings remain bootstrap YAML.

#### P6S / ONVIF stream testing

Most ONVIF-compatible cameras expose the video itself as an RTSP stream. To test a P6S-style camera while waiting for the OV5647, open `/settings`, set **Camera backend** to `onvif / RTSP`, and either:

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

Supported OCR backends:

- `paddleocr`: optional PaddleOCR backend.
- `easyocr`: optional EasyOCR backend.

Optional OCR installs:

```bash
pip install paddleocr
pip install easyocr
```

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

## ONNX YOLO Setup

The default setup expects ONNX detection and a real ONVIF/RTSP camera.

To enable ONNX inference:

1. Sign in as an admin.
2. Open `/settings`.
3. Click **Download YOLOv8n ONNX** to export `yolov8n.pt` to ONNX locally, or enter your own model path.
4. Save AI settings.
5. Use **Check model**, **Reload detector**, and **Test detector**.

If the model is missing, the dashboard reports `MODEL MISSING` and upload inference returns a clear API error.

Manual model export remains available. For local development, install the export dependencies first if they are not already present:

```bash
pip install ultralytics onnx
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
```

For service installs, the dashboard download/export writes to `/opt/daygle-ai-camera/models/yolov8n.onnx`. The Debian and Armbian installers create that directory, preserve existing `*.onnx` and `*.pt` files during reinstall, and allow the root-running service to write there.

## Users and Roles

First setup:

1. Start with an empty SQLite database.
2. Browse to `/setup` or `/`.
3. Create the first `admin` user.
4. After any user exists, `/setup` is disabled and redirects to `/login`.

Admins can open `/users` to create users, disable users, change roles, and reset passwords.

Roles:

- `admin`: dashboard, events, alerts, recordings, users, profile, AI settings, alert settings, and system settings.
- `viewer`: dashboard, events, alert history, recordings, and profile.

Password policy requires at least 8 characters with uppercase, lowercase, numeric, and symbol characters.

## Important Routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET/POST` | `/setup` | First admin setup |
| `GET/POST` | `/login` | Login |
| `GET/POST` | `/logout` | End session |
| `GET` | `/` | Dashboard |
| `GET` | `/profile` | Current user profile |
| `GET` | `/users` | Admin user management |
| `GET` | `/settings` | Admin AI settings |
| `GET` | `/alert-settings` | Admin alert and SMTP settings |
| `GET` | `/settings` | Admin camera, recording, storage, and login settings |
| `GET` | `/anpr` | Plate search, history, details, and alert rules |
| `GET` | `/api/auth/me` | Current user, CSRF token, and session expiry |
| `PUT` | `/api/profile` | Update profile preferences |
| `POST` | `/api/profile/password` | Change current user's password |
| `GET` | `/api/status` | Camera and detector status |
| `GET` | `/api/status/ai` | Detailed AI status |
| `GET` | `/api/live/snapshot` | Live ONVIF/RTSP JPEG snapshot |
| `POST` | `/api/detect/test-image` | Upload image for active detector inference |
| `GET` | `/api/events` | List/search events |
| `GET` | `/api/alerts` | Alert history |
| `GET` | `/api/recordings` | List recordings |
| `GET` | `/api/recordings/{id}/stream` | Stream recording media when present |
| `POST` | `/api/recordings/purge` | Admin purge using retention settings |
| `GET` | `/api/plates` | Recent vehicle plates |
| `GET` | `/api/plates/{id}` | Plate details and history |
| `GET` | `/api/plates/search` | Search plate events |
| `POST` | `/api/plates/whitelist` | Admin whitelist plate and notes |
| `POST` | `/api/plates/blacklist` | Admin blacklist plate and notes |
| `GET/POST` | `/api/plate-alerts` | View/create plate alert rules |
| `PUT/DELETE` | `/api/plate-alerts/{id}` | Edit/delete plate alert rules |
| `GET/POST` | `/api/users` | Admin list/create users |
| `PATCH` | `/api/users/{id}` | Admin role/status/password updates |
| `GET/PUT` | `/api/settings/ai` | Admin AI settings |
| `GET/PUT` | `/api/settings/alert-email` | Admin SMTP settings |
| `GET/POST` | `/api/settings/alerts` | View/create alert rules |
| `PUT/DELETE` | `/api/settings/alerts/{id}` | Edit/delete alert rules |
| `GET` | `/api/settings/system` | Admin system settings summary |
| `PUT` | `/api/settings/system/camera` | Admin camera settings |
| `GET/PUT` | `/api/settings/anpr` | Admin ANPR settings |
| `PUT` | `/api/settings/system/recording` | Admin recording settings |
| `PUT` | `/api/settings/system/storage` | Admin storage settings |
| `PUT` | `/api/settings/system/auth` | Admin login security settings |

## Database

Tables are created automatically at startup. Runtime settings are stored in `app_settings` as JSON values.

Core tables:

- `users`
- `user_sessions`
- `login_attempts`
- `app_settings`
- `alert_rules`
- `events`
- `detections`
- `alert_history`
- `recordings`
- `vehicle_plates`
- `plate_events`
- `plate_alert_rules`

Useful `app_settings` keys:

- `ai`
- `alert_email`
- `camera`
- `anpr`
- `recording`
- `storage`
- `auth`

## Recording and Playback

The recording layer creates event-linked clips and dashboard playback wiring for live camera detections and uploaded-image tests.

Recording policy is managed at `/settings`:

- `continuous`: record every detection event.
- `motion`: record when any detection is present.
- `human`: record when `person` is detected.
- `objects`: record selected labels such as `cat`, `dog`, `package`, and `parcel`.

The policy also has independent toggles for continuous, motion, human, and selected object recording. The dashboard recordings list shows the trigger type and trigger label for each clip.

Retention is managed on the same page:

- `auto_purge_enabled`: purge after recording creation.
- `retention_days`: delete clips older than this age.
- `max_storage_gb`: delete oldest clips until the recording folder is under the cap.
- **Purge now**: run retention purge manually.

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

## ANPR Limitations

- Plate crop extraction is a modular placeholder in uploaded-image workflows; camera backends can replace it with real image crops.
- PaddleOCR and EasyOCR are optional dependencies and may require additional platform packages.
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
# Debian 13 / generic Debian host
sudo ./scripts/install_debian.sh
sudo systemctl restart daygle-ai-camera

# Armbian / Orange Pi host
sudo ./scripts/install_armbian.sh
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
- **Dashboard shows MODEL MISSING**: open `/settings`, download YOLOv8n ONNX or set a readable model path, then reload the detector.
- **ONNX fails to load**: confirm model and labels paths are readable and ONNX Runtime is installed.
- **Email alerts do not send**: open `/alert-settings`, check SMTP host/port/auth/from address, and confirm the rule has email enabled and recipients. Open `/settings` and confirm Live performance -> Background alerts is enabled so cat/person/object rules continue checking when the Live Cameras page is closed.
- **Service cannot write data or models**: open `/settings` and check storage paths, then verify `/opt/daygle-ai-camera/models` and the configured data directory exist. The Debian and Armbian installers run the systemd service as root and grant write access to config, data, and models paths.
- **Need service logs**: run `sudo journalctl -u daygle-ai-camera -f`.
