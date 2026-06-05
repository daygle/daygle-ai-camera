# Daygle AI Camera Development

## Local environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp config.example.yaml config.yaml
DAYGLE_CONFIG=config.yaml uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>. A fresh database redirects to `/setup`; create the first administrator and then log in at `/login`.

## Authentication workflow

- `AuthService` creates and manages `users`, `user_sessions`, and `login_attempts` in the configured SQLite database.
- The first-run setup page can create exactly the first admin user; once a user exists, `/setup` redirects to `/login`.
- Login creates a database session, returns an HttpOnly SameSite session cookie, and exposes the CSRF token through `GET /api/auth/me`.
- Dashboard JavaScript sends `X-CSRF-Token` on state-changing requests.
- Failed login attempts are tracked by username and IP. Defaults are 5 failures and 15 minutes lockout.
- Roles are `admin` and `viewer`. Admin-only endpoints are enforced in `app.main` middleware.

## Runtime settings workflow

`config.yaml` is a bootstrap/default source. Normal AI setup happens in the admin-only Setup / AI Settings page at `/settings`; editable runtime settings live in SQLite:

- `app_settings` stores the AI settings document under key `ai`.
- `alert_rules` stores alert rules created or edited in the dashboard.
- Database AI values override config defaults at runtime.
- `GET /api/status/ai` reports the active config source as `database`, `config.yaml`, or `default`.
- Updating AI settings saves to SQLite, attempts a runtime detector reload, and keeps the previous working detector if the reload fails.
- If `ai.backend` is `onnx` and the model is missing, status reports `MODEL MISSING`, uploads return a clear error, and mock image detections are not generated.
- Alert processing reloads DB rules before processing generated events and uploaded-image detections.

## Useful routes during development

| Method | Route | Notes |
| --- | --- | --- |
| `GET/POST` | `/setup` | First admin creation only |
| `GET/POST` | `/login` | Session login |
| `GET/POST` | `/logout` | Session logout; POST requires CSRF |
| `GET` | `/api/auth/me` | Current user and CSRF token |
| `GET/PUT` | `/api/settings/ai` | Admin SQLite-backed AI settings |
| `POST` | `/api/settings/ai/check-model` | Admin model/runtime status action |
| `POST` | `/api/settings/ai/download-yolov8n` | Admin model download action |
| `POST` | `/api/settings/ai/reload` | Admin runtime detector reload |
| `POST` | `/api/settings/ai/test-detector` | Admin detector readiness test |
| `GET/POST` | `/api/settings/alerts` | Alert rules; viewer can GET, admin can POST |
| `PUT/DELETE` | `/api/settings/alerts/{rule_id}` | Admin alert rule changes |
| `GET/POST/PATCH` | `/api/users` and `/api/users/{id}` | Admin user management |

For scripted mutating calls, log in first, call `GET /api/auth/me`, and send the returned `csrf_token` as `X-CSRF-Token`.

## Database tables

Core tables:

```sql
users
user_sessions
login_attempts
app_settings
alert_rules
events
detections
alert_history
```

The app initializes tables on import/startup. Tests use temporary config files and temporary SQLite databases through `DAYGLE_CONFIG`.

## Test commands

```bash
python -m compileall app tests
pytest -q
```

The test suite covers detector selection, missing ONNX errors, model status, setup, login success/failure, lockout, logout, route protection, viewer/admin permissions, user creation, password reset, AI settings persistence/overrides, alert rule CRUD, upload backend reporting, and DB-backed alert processing.

## Security notes

- Keep `auth.enabled: true` for deployed systems.
- Serve through HTTPS (or a VPN) on real networks; cookie `Secure` is set when the request scheme is HTTPS.
- Never log passwords, session tokens, or CSRF tokens.
- Do not store model files or databases in a web-served directory.
- The PBKDF2 fallback exists for constrained test environments; production installs should include `bcrypt` from `requirements.txt`.

## Armbian notes

- Use `scripts/install_armbian.sh` for systemd deployment.
- The service reads `/etc/daygle-ai-camera/config.yaml` via `DAYGLE_CONFIG`.
- SQLite, snapshots, and mutable state should live under writable storage paths from config.
- Mock mode remains available and should not be removed; it keeps Orange Pi deployments usable while camera/model dependencies are being validated. ONNX mode must fail clearly rather than silently falling back to mock detections when its model/runtime is unavailable.

## Recording architecture

Recording support is intentionally lightweight. `RecordingService` owns recording configuration, the recordings storage directory, and mock event metadata generation. Event creation calls the service after the event row is committed, then inserts a linked `recordings` row when `recording.mode: event` is enabled.

Future camera backends should pass encoded media paths (or frame writer outputs) through the same service/database boundary so OV5647 frames, USB camera frames, RTSP streams, and uploaded videos can create event-linked clips without changing the dashboard API.

### Recording database table

```sql
recordings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NULL,
  camera_id TEXT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  duration_seconds REAL NOT NULL,
  file_path TEXT NOT NULL,
  thumbnail_path TEXT NULL,
  source TEXT NOT NULL CHECK(source IN ('mock', 'camera', 'upload', 'rtsp')),
  created_at TEXT NOT NULL
)
```

`events` responses include `recordings` and `recording_status` so the dashboard can link detections to playback. Recording list/detail responses include linked event data and detections for filtering/display.

### Playback API during development

| Method | Route | Expected behavior |
| --- | --- | --- |
| `GET` | `/api/recordings` | Lists newest recordings; supports `?label=cat` |
| `GET` | `/api/recordings/{id}` | Returns one recording or `404` |
| `GET` | `/api/recordings/{id}/stream` | Streams the media file with byte-range support or returns clean `404` if placeholder media is missing |
| `DELETE` | `/api/recordings/{id}` | Admin-only; removes DB row and media file if present |

To test playback locally, create a detection from the dashboard or with `POST /api/mock/detect`, open the **Recordings / Playback** panel, and click **Play clip**. In mock mode, a clean missing-media message is expected unless you manually place an MP4 at the recording `file_path`.
