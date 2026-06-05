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

`config.yaml` is a bootstrap/default source. Editable runtime settings live in SQLite:

- `app_settings` stores the AI settings document under key `ai`.
- `alert_rules` stores alert rules created or edited in the dashboard.
- Database values override config defaults at runtime.
- Updating AI settings safely constructs a replacement detector first; if an ONNX reload fails, the previous detector remains active.
- Alert processing reloads DB rules before processing generated events and uploaded-image detections.

## Useful routes during development

| Method | Route | Notes |
| --- | --- | --- |
| `GET/POST` | `/setup` | First admin creation only |
| `GET/POST` | `/login` | Session login |
| `GET/POST` | `/logout` | Session logout; POST requires CSRF |
| `GET` | `/api/auth/me` | Current user and CSRF token |
| `GET/PUT` | `/api/settings/ai` | Admin AI settings |
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

The test suite covers detector selection, setup, login success/failure, lockout, logout, route protection, viewer/admin permissions, user creation, password reset, AI settings overrides, alert rule CRUD, and DB-backed alert processing.

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
- Mock mode remains available and should not be removed; it keeps Orange Pi deployments usable while camera/model dependencies are being validated.
