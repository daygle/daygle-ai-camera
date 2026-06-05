# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B / Armbian-friendly AI camera platform with a FastAPI backend, SQLite storage, session authentication, a browser dashboard, mock camera mode for development, and mock or ONNX YOLO object detection.

The app is now designed to be configured from the web UI. `config.yaml` is only a small bootstrap file for settings the app must know before the database and dashboard can load.

## Features

- Protected browser dashboard with event search, alert history, recordings, playback, and object stats.
- First-run setup flow for creating the initial administrator.
- Admin/viewer roles with admin-only settings and user management.
- Profile page for timezone, date/time format, and password changes.
- Web-managed AI settings for mock or ONNX detection.
- Modular ANPR/ALPR pipeline for vehicle detections, plate OCR, plate search, and plate alerts.
- Web-managed alert rules with optional SMTP email delivery.
- Web-managed system settings for camera, recording policy, retention, storage directories, and login security.
- SQLite persistence for events, detections, alerts, users, sessions, runtime settings, and alert rules.
- Mock camera/detector for development and CI without camera hardware.
- Armbian install script and systemd unit for Orange Pi deployment.

## Requirements

### Local Development

- Python 3.10 or newer.
- `pip` and optionally `python3-venv`.
- A modern browser.
- Optional: an ONNX YOLO model file if you want real inference instead of mock detection.
- Optional: PaddleOCR or EasyOCR if you want OCR beyond the built-in mock ANPR backend.

### Orange Pi / Armbian Deployment

- Orange Pi 3B or another Linux host running Armbian/Debian-like packages.
- Root or `sudo` access.
- Network access during installation for `apt` and `pip`.
- Optional: HTTPS reverse proxy or VPN for devices exposed beyond a private LAN.

On a fresh Debian-like host:

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  git \
  python3 \
  python3-pip \
  python3-dev \
  python3-venv \
  sqlite3 \
  ca-certificates \
  rsync \
  ffmpeg \
  v4l-utils \
  libgl1 \
  libglib2.0-0
```

## Installation

### Local Install

1. Clone the repository and enter it:

   ```bash
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
   pip install --upgrade pip wheel
   pip install -r requirements.txt pytest
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

### Armbian Service Install

Run the installer from the repository root:

```bash
sudo ./scripts/install_armbian.sh
```

The installer will:

- Install required system packages.
- Create a `daygle` service user unless `DAYGLE_USER` overrides it.
- Copy the app to `/opt/daygle-ai-camera` unless `DAYGLE_APP_DIR` overrides it.
- Create `/etc/daygle-ai-camera/config.yaml` unless `DAYGLE_CONFIG_DIR` overrides it.
- Store SQLite data and generated files under `/var/lib/daygle-ai-camera` unless `DAYGLE_DATA_DIR` overrides it.
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

Route: `/settings`

- AI enabled state.
- Backend: `mock` or `onnx`.
- Confidence threshold.
- IOU threshold.
- Input size.
- Model path.
- Labels path.
- Check model.
- Download YOLOv8n ONNX.
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

Route: `/system-settings`

- Camera: backend, device, width, height, FPS, flip.
- ANPR: enable/disable, OCR backend, confidence threshold, and vehicle labels.
- Recording policy: continuous recording, record on motion, record on human, and selected object labels such as `cat`, `dog`, `package`, and `parcel`.
- Clip settings: pre-event seconds, post-event seconds, max clip seconds, and file format.
- Retention: auto purge, retention days, max storage GB, and manual purge.
- Storage: data, snapshots, events, and recordings directories.
- Login security: session timeout, max login attempts, lockout minutes.

Camera, ANPR, recording, storage, and login security settings are stored in SQLite and applied at runtime where possible. Database path, auth enablement, cookie name, and server bind settings remain bootstrap YAML.

### ANPR

Route: `/anpr`

ANPR runs as a separate pipeline after object detection:

```text
Camera/Image -> YOLO Object Detection -> Vehicle Detection -> Plate Detection -> Plate OCR -> Plate Event -> Search / Alerts
```

Vehicle labels are configurable and default to `car`, `truck`, `bus`, and `motorcycle`. When one is detected, the ANPR pipeline writes a plate crop artifact, runs OCR, stores a plate event, and links it back to the original event and any recording.

Supported OCR backends:

- `mock`: built-in deterministic OCR for development and tests.
- `paddleocr`: optional PaddleOCR backend. Falls back to mock if the package is unavailable.
- `easyocr`: optional EasyOCR backend. Falls back to mock if the package is unavailable.

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

The default setup uses mock detection so installation can be verified without a camera or model file.

To enable ONNX inference:

1. Sign in as an admin.
2. Open `/settings`.
3. Click **Download YOLOv8n ONNX**, or enter your own model path.
4. Set backend to `onnx`.
5. Save AI settings.
6. Use **Check model**, **Reload detector**, and **Test detector**.

If `onnx` is selected and the model is missing, the dashboard reports `MODEL MISSING` and upload inference returns a clear API error. The app does not silently fall back to mock detections.

Manual model download remains available:

```bash
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
```

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
| `GET` | `/system-settings` | Admin camera, recording, storage, and login settings |
| `GET` | `/anpr` | Plate search, history, details, and alert rules |
| `GET` | `/api/auth/me` | Current user, CSRF token, and session expiry |
| `PUT` | `/api/profile` | Update profile preferences |
| `POST` | `/api/profile/password` | Change current user's password |
| `GET` | `/api/status` | Camera and detector status |
| `GET` | `/api/status/ai` | Detailed AI status |
| `POST` | `/api/mock/detect` | Generate mock detection event |
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

The recording layer creates event-linked clips and dashboard playback wiring. In the current mock/test-image stage, clips are generated as short playable footage with event and detection overlays. Future camera and RTSP work can replace the generated frames with real camera frames while keeping the same recording policy and retention controls.

Recording policy is managed at `/system-settings`:

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

- Mock and uploaded-image events create generated footage, not real camera footage.
- Real clip writing for camera, RTSP, and uploaded videos is future work.
- Retention runs when clips are created or when an admin clicks **Purge now**; there is no background scheduler yet.

## ANPR Limitations

- Plate crop extraction is a modular placeholder in mock/upload workflows; future camera backends can replace it with real image crops.
- The built-in `mock` OCR backend is deterministic and useful for workflow testing, not real OCR.
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

### Armbian Service

```bash
sudo ./scripts/install_armbian.sh
sudo systemctl restart daygle-ai-camera
```

The installer preserves existing config unless you remove or replace it manually.

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
- **Email alerts do not send**: open `/alert-settings`, check SMTP host/port/auth/from address, and confirm the rule has email enabled and recipients.
- **Service cannot write data**: open `/system-settings` and check storage paths, then verify OS permissions for the service user.
- **Need logs on Armbian**: run `sudo journalctl -u daygle-ai-camera -f`.
