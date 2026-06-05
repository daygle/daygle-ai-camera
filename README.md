# Daygle AI Camera

Daygle AI Camera is an Orange Pi 3B AI camera platform with a FastAPI backend, SQLite event history, alert rules, authentication, and a modern dark-mode dashboard. It runs end-to-end today without camera hardware by using a mock camera backend and synthetic object detector.

## Features

- FastAPI backend with protected status, event, alert, stats, user-management, and runtime-config APIs
- Session-based authentication with bcrypt password hashing, CSRF protection, lockout, and 12-hour session expiry
- First-run setup page for creating the initial administrator account
- Admin/viewer roles and an admin-only user management page
- SQLite database for users, sessions, login attempts, events, detections, and alert history
- Mock camera and mock detector so development does not require a physical camera
- Dark-mode dashboard with searchable object detection history and a top-right user menu
- Configurable alert rule example for cat detections
- Armbian install script and systemd unit for Orange Pi 3B deployment
- Architecture notes for future OV5647 CSI, YOLO ONNX, and RKNN support

## Quick start for local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open <http://127.0.0.1:8080/>. On a new database the app redirects to `/setup`; create the first administrator account, then sign in at `/login`.

Generate a mock event from the dashboard. API writes require an authenticated session and a CSRF token, so browser actions are the recommended manual test path. For scripted API calls, sign in first, read `csrf_token` from `GET /api/auth/me`, and send it as `X-CSRF-Token` on mutating requests.

## Authentication and first-run setup

Authentication is enabled by default:

```yaml
auth:
  enabled: true
  session_timeout_hours: 12
  max_login_attempts: 5
  lockout_minutes: 15
```

First startup flow:

1. Start the service with an empty SQLite database.
2. Browse to `/` and follow the redirect to `/setup`.
3. Create the first admin user with a complex password.
4. After the first user exists, `/setup` is disabled and redirects to `/login`.
5. Sign in and use the top-right user menu for Profile, Users, Settings, and Logout links.

Password policy requires at least 8 characters with uppercase, lowercase, numeric, and symbol characters. Passwords are never stored in plaintext; installed deployments use bcrypt hashes via the `bcrypt` package.

## User management

Admins can browse to `/users` or use the dashboard menu to:

- Create `admin` or `viewer` users
- Disable or re-enable users
- Reset passwords
- Change roles

Role model:

- `admin`: dashboard access, user management, settings, and alert configuration endpoints.
- `viewer`: dashboard access, event search, and alert viewing.

## Security model

- Protected routes: `/`, `/api/*`, `/settings`, `/events`, `/alerts`, and `/search`.
- Public routes: `/login`, `/setup` until first user creation, and `/static/*`.
- Session cookies are HttpOnly, SameSite=Lax, 12-hour expiring cookies. The Secure flag is set automatically when served over HTTPS.
- Mutating API endpoints require the session CSRF token in `X-CSRF-Token`.
- Login attempts are recorded in `login_attempts`.
- Accounts lock for `auth.lockout_minutes` after `auth.max_login_attempts` consecutive failures.

## Database schema

The SQLite database includes the existing event tables plus authentication tables:

```sql
users(id, username, password_hash, role, is_active, failed_attempts, locked_until, created_at, updated_at)
user_sessions(id, session_token, user_id, csrf_token, created_at, expires_at, last_seen_at)
login_attempts(id, username, ip_address, success, created_at)
events(id, created_at, source, snapshot_path, thumbnail_path, alert_triggered, metadata)
detections(id, event_id, label, confidence, x, y, width, height)
alert_history(id, created_at, rule_name, event_id, label, confidence, message)
```

Tables are created automatically at startup by the database/auth initialization code.

## Armbian 26.x startup on Orange Pi 3B

From a checkout on the Orange Pi:

```bash
sudo ./scripts/install_armbian.sh
sudo systemctl status daygle-ai-camera
sudo journalctl -u daygle-ai-camera -f
```

Then browse to:

```text
http://<orange-pi-ip>:8080/
```

The installer:

1. Installs Python, venv, pip, SQLite, certificates, and rsync.
2. Copies the app to `/opt/daygle-ai-camera`.
3. Creates a virtual environment in `/opt/daygle-ai-camera/.venv`.
4. Installs `requirements.txt`, including bcrypt support for password hashing.
5. Creates `/etc/daygle-ai-camera/config.yaml` from `config.example.yaml` if needed.
6. Stores mutable data under `/var/lib/daygle-ai-camera`.
7. Installs and enables `daygle-ai-camera.service`.

Override install paths if needed:

```bash
sudo DAYGLE_APP_DIR=/opt/daygle-ai-camera \
  DAYGLE_CONFIG_DIR=/etc/daygle-ai-camera \
  DAYGLE_DATA_DIR=/var/lib/daygle-ai-camera \
  DAYGLE_USER=daygle \
  ./scripts/install_armbian.sh
```

## API overview

- `GET /` - protected dashboard
- `GET /login` / `POST /login` - public login form/action
- `GET /logout` / `POST /logout` - logout action
- `GET /setup` / `POST /setup` - first-admin setup, disabled after first user
- `GET /api/auth/me` - current user and CSRF token
- `GET /api/status` - runtime status
- `POST /api/mock/detect` - create a mock detection event; requires `X-CSRF-Token`
- `GET /api/events?label=cat&limit=50` - event search
- `GET /api/events/{event_id}` - event detail
- `GET /api/alerts?limit=25` - alert history
- `GET /api/stats` - aggregate stats
- `GET /api/config` - non-secret runtime config summary
- `GET /api/users`, `POST /api/users`, `PATCH /api/users/{id}` - admin user management

## Development documentation

See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture details and future OV5647 CSI / YOLO ONNX / RKNN integration notes.

## Remaining security recommendations

- Serve the application behind HTTPS so browsers receive Secure session cookies.
- Put the service behind a trusted reverse proxy or VPN for internet-exposed deployments.
- Add audit-log retention and export before enabling remote access.
- Consider hardware-backed secrets and WebAuthn/TOTP for higher-risk installations.
