# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B / Armbian-friendly AI camera platform with a FastAPI backend, SQLite storage, session authentication, a modern dashboard, mock camera mode, and mock/ONNX YOLO detection.

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

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp config.example.yaml config.yaml
DAYGLE_CONFIG=config.yaml uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>. With an empty database, the app redirects to `/setup`; create the first administrator, then sign in at `/login`.

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

`config.yaml` remains the bootstrap/default configuration. Runtime edits made on `/settings` are stored in SQLite and override config values without requiring direct config-file edits.

Admins can manage:

- AI enabled state.
- Backend: `mock` or `onnx`.
- Confidence threshold.
- IOU threshold.
- Input size.
- Model path.
- Labels path.
- Alert rules: name, object label, min confidence, cooldown, enabled state, and optional active time window.

If an ONNX detector reload fails, the API returns a clear error and keeps the previous working detector.

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
| `POST` | `/api/mock/detect` | Generate a mock detection event |
| `POST` | `/api/detect/test-image` | Upload an image for active detector inference |
| `GET` | `/api/events` | Search/list events |
| `GET` | `/api/events/{event_id}` | Fetch one event with detections |
| `GET` | `/api/alerts` | Alert history |
| `GET` | `/api/stats` | Event and object stats |
| `GET` | `/api/config` | Non-secret runtime summary |
| `GET/POST` | `/api/users` | Admin list/create users |
| `PATCH` | `/api/users/{user_id}` | Admin role/status/password updates |
| `GET/PUT` | `/api/settings/ai` | Admin view/update AI settings |
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

## ONNX YOLO setup

```bash
python scripts/download_yolov8n_onnx.py --output models/yolov8n.onnx
```

Then either edit `config.yaml` before first start or use `/settings` as an admin:

```yaml
ai:
  enabled: true
  backend: onnx
  confidence: 0.45
  iou_threshold: 0.45
  input_size: 640
  model_path: models/yolov8n.onnx
  labels_path: models/coco.names
```

Test-image detection stays available at `POST /api/detect/test-image`. The mock camera mode and `POST /api/mock/detect` are intentionally preserved.

## Armbian deployment notes

```bash
sudo scripts/install_armbian.sh
sudo systemctl status daygle-ai-camera
```

The installer uses `/etc/daygle-ai-camera/config.yaml` via `DAYGLE_CONFIG`. Keep `auth.enabled: true` on devices exposed to a network. Use a reverse proxy or VPN with HTTPS so cookies receive the Secure flag and credentials are protected in transit.

## Tests

```bash
python -m compileall app tests
pytest -q
```
