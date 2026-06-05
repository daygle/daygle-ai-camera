# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B / Armbian-friendly AI camera platform. It provides a FastAPI backend, SQLite storage, session authentication, a browser dashboard, mock camera mode for development, and mock or ONNX YOLO object detection.

## Features

- FastAPI backend with protected dashboard and APIs.
- SQLite event database for events, detections, alert history, users, sessions, login attempts, runtime AI settings, and editable alert rules.
- First-run setup flow for creating the initial administrator.
- Session-based authentication with HttpOnly SameSite cookies, CSRF protection, configurable expiry, and lockout after repeated failures.
- Admin/viewer roles with admin-only user and settings management.
- Mock camera/detector for development and CI without camera hardware.
- ONNX YOLO test-image detection that preserves the existing upload workflow.
- Dashboard user menu, logout, user management, and admin-only settings page.
- Armbian install script and systemd unit for Orange Pi 3B deployment.

## Requirements

### Local development

- Python 3.10 or newer.
- `python3-venv` and `pip`.
- A modern browser.
- Optional: an ONNX YOLO model file if you want real detector inference instead of the mock detector.

### Orange Pi / Armbian deployment

- Orange Pi 3B or another Linux host running Armbian/Debian-like packages.
- Root or `sudo` access.
- Network access during installation so `apt` and `pip` can install dependencies.
- Optional: HTTPS reverse proxy or VPN for devices exposed beyond a private LAN.

### Debian apt packages

On a fresh Debian-like host, install the system software before installing
Python dependencies:

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

For Debian 13 installs without a virtual environment, see
`INSTALL_DEBIAN13.md`.

## Installation

Choose one of these installation paths:

- **Local install**: best for development, testing, or running the app manually on a workstation.
- **Armbian service install**: best for Orange Pi deployment with systemd auto-start.

### Option 1: Local install

1. Clone the repository and enter it:

   ```bash
   git clone https://github.com/daygle/daygle-ai-camera.git
   cd daygle-ai-camera
   ```

   If you already have the source code, run the remaining commands from the repository root.

2. Create and activate a Python virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:

   ```bash
   pip install --upgrade pip wheel
   pip install -r requirements-dev.txt
   ```

   Use `requirements-dev.txt` for local development and tests. It includes the runtime requirements plus `pytest`. For a runtime-only environment, install `requirements.txt` instead.

4. Create a writable configuration file:

   ```bash
   cp config.example.yaml config.yaml
   ```

5. Start the web server:

   ```bash
   DAYGLE_CONFIG=config.yaml uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
   ```

6. Open the dashboard at <http://127.0.0.1:8080/>.

7. Complete first-run setup:
   - A fresh database redirects to `/setup`.
   - Create the first administrator account.
   - After setup, sign in at `/login`.

### Option 2: Armbian service install

Run the installer from the repository root on the Orange Pi or target Linux host:

```bash
sudo ./scripts/install_armbian.sh
```

The installer will:

- Install required system packages with `apt`.
- Create a system user named `daygle` unless `DAYGLE_USER` overrides it.
- Copy the app to `/opt/daygle-ai-camera` unless `DAYGLE_APP_DIR` overrides it.
- Create `/etc/daygle-ai-camera/config.yaml` unless `DAYGLE_CONFIG_DIR` overrides it.
- Store SQLite data and generated files under `/var/lib/daygle-ai-camera` unless `DAYGLE_DATA_DIR` overrides it.
- Create and start the `daygle-ai-camera.service` systemd service.

Check the service and logs:

```bash
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Open the dashboard at:

```text
http://<orange-pi-ip>:8080/
```

Then complete the same first-run setup flow by creating the first administrator.

### Installer environment overrides

You can override install paths before running the Armbian installer:

```bash
sudo DAYGLE_USER=daygle \
  DAYGLE_APP_DIR=/opt/daygle-ai-camera \
  DAYGLE_CONFIG_DIR=/etc/daygle-ai-camera \
  DAYGLE_DATA_DIR=/var/lib/daygle-ai-camera \
  ./scripts/install_armbian.sh
```

## Configuration

The app reads configuration from the file pointed to by `DAYGLE_CONFIG`. For local installs, this is usually `config.yaml`. For service installs, the default is `/etc/daygle-ai-camera/config.yaml`.

Start from the example file:

```bash
cp config.example.yaml config.yaml
```

Important sections:

- `server`: host and port for Uvicorn.
- `camera`: mock camera settings and future camera backend settings.
- `ai`: mock or ONNX detector settings.
- `alerts`: initial alert-rule defaults.
- `auth`: authentication, session, and lockout settings.
- `storage`: database, snapshot, and event file locations.

`config.yaml` is the bootstrap/default configuration. Runtime edits made on `/settings` are stored in SQLite and override config values without requiring direct config-file edits.

## Web-based setup and ONNX YOLO setup

Normal users should not need to edit `config.yaml` after installation. Start the app, create the first administrator, then open **Setup / AI Settings** from the dashboard menu (`/settings`). The page shows the active backend, model and labels paths, whether the model file exists, whether ONNX Runtime is installed, whether the detector is loaded, the last detector error, and whether AI settings are coming from SQLite, `config.yaml`, or defaults.

The default configuration uses the mock detector, which is enough to verify installation. To enable ONNX inference from the web UI:

1. Sign in as an admin.
2. Open `/settings`.
3. Click **Download YOLOv8n ONNX** to install `models/yolov8n.onnx`, or enter your own model path.
4. Set **Backend** to `onnx`.
5. Adjust confidence, IOU threshold, input size, model path, and labels path.
6. Click **Save AI settings**. The settings are saved to SQLite and override `config.yaml`.
7. Use **Check model**, **Reload detector**, and **Test detector** to validate the runtime detector without restarting the app.

The old script remains available for offline/manual installs:

```bash
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
```

`mock` mode is explicit: mock detections are generated only when `ai.backend` is `mock` or when an admin uses the dedicated mock-camera endpoint. If `ai.backend` is `onnx` and the model is missing, the dashboard reports `MODEL MISSING`, upload inference returns a clear API error, and the app does not silently fall back to fake mock detections. If a runtime reload fails, the previous working detector is kept while the saved SQLite settings and status panel show the error.

Test-image detection stays available at `POST /api/detect/test-image` and returns the backend used in the response. The mock camera mode and `POST /api/mock/detect` are intentionally preserved.

## First setup and users

1. Start with an empty SQLite database.
2. Browse to `/setup` or `/`.
3. Create the first `admin` user with a complex password.
4. After any user exists, `/setup` is disabled and redirects to `/login`.
5. Admins can open `/users` from the dashboard menu to create users, disable users, change roles, and reset passwords.

Roles:

- `admin`: view dashboard, search events, view alerts, manage users, update AI settings, and create/edit/delete alert rules.
- `viewer`: view dashboard, search events, view alert history, and view current alert rules. Viewers cannot manage users, modify AI settings, change config, or modify alert rules.

Password policy requires at least 8 characters with uppercase, lowercase, numeric, and symbol characters. Passwords are hashed with bcrypt when installed; tests have a PBKDF2 fallback for constrained environments.

## Authentication and sessions

Default auth configuration:

```yaml
auth:
  enabled: true
  session_timeout_hours: 12
  max_login_attempts: 5
  lockout_minutes: 15
  cookie_name: daygle_session
```

Security behavior:

- `/login`, `/setup`, and `/static/*` are public.
- `/`, dashboard pages, and `/api/*` are protected once a user exists.
- Sessions are stored in SQLite and referenced by a Secure-on-HTTPS, HttpOnly, SameSite=Lax cookie.
- Session expiry defaults to 12 hours.
- Mutating routes require `X-CSRF-Token`; dashboard JavaScript retrieves it from `GET /api/auth/me`.
- Failed logins are stored in `login_attempts`; accounts or IPs lock for 15 minutes after 5 recent failures by default.

## AI and alert settings

Admins can manage these settings from `/settings`:

- AI enabled state.
- Backend: `mock` or `onnx`.
- Confidence threshold.
- IOU threshold.
- Input size.
- Model path.
- Labels path.
- Model actions: check model, download YOLOv8n ONNX, reload detector, and test detector.
- Alert rules: create, edit, delete, enable/disable, object label, minimum confidence, cooldown, and optional active time window.

AI settings are stored as a JSON document in SQLite table `app_settings` under key `ai`. Alert rules are stored in `alert_rules`. SQLite settings override `config.yaml`; `config.yaml` remains the bootstrap/default source for new installs.

## Important routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET/POST` | `/setup` | First admin setup, disabled after first user exists |
| `GET/POST` | `/login` | Login page and login action |
| `GET/POST` | `/logout` | End the current session |
| `GET` | `/` | Main dashboard |
| `GET` | `/users` | Admin user-management page |
| `GET` | `/settings` | Admin AI and alert settings page |
| `GET` | `/api/auth/me` | Current user, role, CSRF token, and expiry |
| `GET` | `/api/status` | Camera and detector status |
| `GET` | `/api/status/ai` | Detailed AI status panel data |
| `POST` | `/api/mock/detect` | Generate a mock detection event |
| `POST` | `/api/detect/test-image` | Upload an image for active detector inference |
| `GET` | `/api/events` | Search/list events |
| `GET` | `/api/events/{event_id}` | Fetch one event with detections |
| `GET` | `/api/alerts` | Alert history |
| `GET` | `/api/stats` | Event and object stats |
| `GET` | `/api/config` | Non-secret runtime summary |
| `GET/POST` | `/api/users` | Admin list/create users |
| `PATCH` | `/api/users/{user_id}` | Admin role/status/password updates |
| `GET/PUT` | `/api/settings/ai` | Admin view/update SQLite-backed AI settings |
| `POST` | `/api/settings/ai/check-model` | Admin checks model/runtime/detector status |
| `POST` | `/api/settings/ai/download-yolov8n` | Admin downloads YOLOv8n ONNX to `models/yolov8n.onnx` |
| `POST` | `/api/settings/ai/reload` | Admin reloads detector without app restart when possible |
| `POST` | `/api/settings/ai/test-detector` | Admin validates the configured detector is ready |
| `GET/POST` | `/api/settings/alerts` | View alert rules; admin creates rules |
| `PUT/DELETE` | `/api/settings/alerts/{rule_id}` | Admin edit/delete alert rules |

## Database schema

Tables are created automatically at startup:

```sql
users(id, username, password_hash, role, is_active, failed_attempts, locked_until, created_at, updated_at, last_login_at)
user_sessions(id, session_token, user_id, csrf_token, created_at, expires_at, last_seen_at)
login_attempts(id, username, ip_address, success, created_at)
app_settings(key, value, updated_at)
alert_rules(id, name, object, min_confidence, cooldown_seconds, enabled, active_start, active_end, created_at, updated_at)
events(id, created_at, source, snapshot_path, thumbnail_path, alert_triggered, metadata)
detections(id, event_id, label, confidence, x, y, width, height)
alert_history(id, created_at, rule_name, event_id, label, confidence, message)
```

## Updating an existing install

### Local install

```bash
git pull
source .venv/bin/activate
pip install -r requirements-dev.txt
DAYGLE_CONFIG=config.yaml uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

### Armbian service install

From the updated repository root:

```bash
sudo ./scripts/install_armbian.sh
sudo systemctl restart daygle-ai-camera
```

The installer preserves an existing config file at `/etc/daygle-ai-camera/config.yaml` unless you remove or replace it manually.

## Troubleshooting

- **Cannot log in after first start**: open `/setup` and create the initial admin user if the database is empty.
- **Setup page redirects to login**: a user already exists; sign in with an existing admin account or reset the database intentionally.
- **Dashboard shows MODEL MISSING**: open `/settings`, click **Download YOLOv8n ONNX** or set a readable model path, then click **Reload detector**.
- **ONNX detector fails to load**: confirm the model path and labels path in `/settings` exist and are readable by the running user, and verify ONNX Runtime is installed in the status panel.
- **Service cannot write data**: check ownership of the configured `storage` paths. The Armbian installer assigns them to the service user.
- **Need logs on Armbian**: run `sudo journalctl -u daygle-ai-camera -f`.

## Tests

```bash
python -m compileall app tests
pytest -q
```
