from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.utils import _normalize_iso_to_utc, _parse_iso_datetime

_bcrypt_spec = importlib.util.find_spec("bcrypt")
bcrypt = importlib.import_module("bcrypt") if _bcrypt_spec else None

SESSION_COOKIE = "daygle_session"
CSRF_COOKIE = "daygle_csrf"
CSRF_HEADER = "X-CSRF-Token"
VALID_ROLES = {"admin", "viewer"}


# ── Username-enumeration timing-equaliser ──────────────────────────────────
# Lazily pre-computed hashes of a sentinel password used solely as a
# timing equaliser. ``AuthService.authenticate`` runs an equivalent-cost
# password verification against one of these dummy hashes when ``row is
# None`` (unknown username) so the wall-clock cost of "no such user"
# matches the cost of "known user / wrong password" (the latter already
# pays a real ``bcrypt.checkpw`` / PBKDF2 round inside ``verify_password``).
# Without this equalisation an attacker can enumerate valid usernames by
# timing login responses -- fast = "no such user", slow = "user exists".
# The sentinel password is never used elsewhere and ``checkpw`` always
# returns False against it, so the call is functionally a no-op while
# still paying the full CPU cost.

_DUMMY_BCRYPT_HASH: str | None = None
_DUMMY_PBKDF2_HASH: str | None = None


def _ensure_dummy_bcrypt_hash() -> str | None:
    """Return a pre-computed bcrypt hash of a sentinel string, lazily built once."""
    global _DUMMY_BCRYPT_HASH
    if _DUMMY_BCRYPT_HASH is not None:
        return _DUMMY_BCRYPT_HASH
    if bcrypt is None:
        return None
    _DUMMY_BCRYPT_HASH = bcrypt.hashpw(
        b"daygle-timing-equaliser-do-not-use-please",
        bcrypt.gensalt(),
    ).decode("utf-8")
    return _DUMMY_BCRYPT_HASH


def _ensure_dummy_pbkdf2_hash() -> str | None:
    """Return a pre-computed PBKDF2-hash-formatted string for the test-environment fallback."""
    global _DUMMY_PBKDF2_HASH
    if _DUMMY_PBKDF2_HASH is not None:
        return _DUMMY_PBKDF2_HASH
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        b"daygle-timing-equaliser-do-not-use-please",
        salt.encode("utf-8"),
        390000,
    ).hex()
    _DUMMY_PBKDF2_HASH = f"pbkdf2_sha256${salt}${digest}"
    return _DUMMY_PBKDF2_HASH


def _equalize_password_timing(password: str) -> None:
    """Perform a no-op password verification whose CPU cost matches the real path.

    Called from ``AuthService.authenticate`` whenever the user row is missing
    or disabled, so the unknown-username wall-clock latency matches the
    known-username / wrong-password latency. Defends against username
    enumeration via response-time analysis. Never raises -- if either backend
    raises (corrupt dummy hash, bcrypt disabled mid-call) we swallow the
    exception so the auth flow continues to its expected error.
    """
    try:
        if bcrypt is not None:
            dummy = _ensure_dummy_bcrypt_hash()
            if dummy:
                bcrypt.checkpw(password.encode("utf-8"), dummy.encode("utf-8"))
                return
        # PBKDF2 fallback path (tests without bcrypt): pay the same shape of work.
        dummy = _ensure_dummy_pbkdf2_hash()
        if dummy:
            _algorithm, salt, digest = dummy.split("$", 2)
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("utf-8"),
                390000,
            ).hex()
            hmac.compare_digest(candidate, digest)
    except Exception:
        # Defensive: dummy equalisation must never propagate; the auth
        # path's real branch decides the user-visible outcome.
        pass


def _is_locked_until_future(raw: Any, now_dt: datetime) -> bool:
    """True iff ``raw`` is a parseable ISO datetime strictly in the future of ``now_dt``.

    Defence-in-depth against legacy ``locked_until`` rows being naive
    ``datetime`` strings (no tzinfo). Comparing a naive datetime against the
    tz-aware ``now_dt`` used to raise ``TypeError`` and bubble out of
    ``change_password`` / ``authenticate`` as an HTTP 500. Naive timestamps
    are interpreted as UTC (matching the storage contract for any ISO
    timestamp the auth layer writes, which is canonical ``+00:00``).
    Unparseable values are treated as "not locked" so a corrupt row cannot
    permanently brick an account.
    """
    if not raw:
        return False
    parsed = _parse_iso_datetime(raw)
    if parsed is None:
        return False
    return parsed > now_dt


class AuthError(Exception):
    pass


class AuthService:
    def __init__(self, database_path: str, config: dict[str, Any]) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.session_timeout = timedelta(hours=float(config.get("session_timeout_hours", 12)))
        self.max_login_attempts = int(config.get("max_login_attempts", 5))
        self.lockout = timedelta(minutes=float(config.get("lockout_minutes", 15)))
        # H2 absolute session-expiry cap. Independent of the sliding
        # ``expires_at``: a stolen cookie + active user still loses after
        # ``absolute_session_lifetime`` from sign-in. Configurable via
        # ``auth.absolute_session_lifetime_seconds`` (default 14 days).
        self.absolute_session_lifetime = timedelta(
            seconds=int(config.get('absolute_session_lifetime_seconds', 14 * 86400))
        )
        self.init()
        self.apply_config(self.config)

    def apply_config(self, config: dict[str, Any]) -> None:
        self.config.update(config)
        self.session_timeout = timedelta(hours=float(self.config.get("session_timeout_hours", 12)))
        self.max_login_attempts = int(self.config.get("max_login_attempts", 5))
        self.lockout = timedelta(minutes=float(self.config.get("lockout_minutes", 15)))
        # H2 absolute session-expiry cap (paired with __init__).
        self.absolute_session_lifetime = timedelta(
            seconds=int(self.config.get("absolute_session_lifetime_seconds", 14 * 86400))
        )
        from app.rate_limiter import login_limiter
        login_limiter.apply_config(self.config)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL;')
            db.commit()  # flush PRAGMA before executescript issues its own implicit COMMIT
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'viewer')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT 'Australia/Sydney',
                    date_format TEXT NOT NULL DEFAULT 'locale',
                    time_format TEXT NOT NULL DEFAULT '24h',
                    theme TEXT NOT NULL DEFAULT 'system',
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS user_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_token TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    csrf_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    absolute_expires_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions(session_token);
                CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);
                      CREATE INDEX IF NOT EXISTS idx_login_attempts_username_created ON login_attempts(username, created_at);
                """
            )
            # Migration: add ``absolute_expires_at`` column to ``user_sessions``
            # on databases created before the H2 fix. The ALTER alone is
            # idempotent: re-running init() on an already-migrated DB
            # produces a "duplicate column name" OperationalError which we
            # swallow. Any other OperationalError is real schema corruption
            # and SHOULD propagate up at startup rather than silently passing.
            #
            # The CREATE INDEX and backfill UPDATE must run on EVERY
            # startup, not just the first -- previously they lived inside
            # the same try/except as the ALTER, so the duplicate-column
            # short-circuit on the ALTER would skip both. Fresh
            # installations would have an index; warm restarts would not.
            # The index is required for the H2 absolute-expiry guard's
            # SELECT performance on production-sized ``user_sessions``
            # tables, and the backfill guarantees legacy sessions get a
            # ``created_at + 14 days`` cap. Both are idempotent and safe
            # to re-run.
            try:
                db.execute("ALTER TABLE user_sessions ADD COLUMN absolute_expires_at TEXT")
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc).lower():
                    raise
            # Migration: add the theme column to existing databases.
            # The CREATE TABLE above already includes it for fresh installs.
            try:
                db.execute("ALTER TABLE users ADD COLUMN theme TEXT NOT NULL DEFAULT 'system'")
            except sqlite3.OperationalError as exc:
                if 'duplicate column name' not in str(exc).lower():
                    raise
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_sessions_absolute_expires "
                "ON user_sessions(absolute_expires_at)"
            )
            # Backfill legacy rows with a STRICT retroactive cap at
            # ``created_at + 14 days`` (matching the default lifetime).
            # Per the design-review verdict, grandfathering with NOW+14
            # is REJECTED -- enforcing the cap strictly on existing
            # long-lived sessions is the security-correct choice
            # (otherwise stolen cookies for sessions older than 14d
            # would quietly extend without bound).
            db.execute(
                "UPDATE user_sessions "
                "SET absolute_expires_at = datetime(created_at, '+14 days') "
                "WHERE absolute_expires_at IS NULL"
            )
            db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (utc_now(),))

    def users_exist(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()
            return bool(row["count"])

    def validate_password_complexity(self, password: str) -> list[str]:
        errors: list[str] = []
        if len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        if not any(c.isupper() for c in password):
            errors.append("Password must include an uppercase letter.")
        if not any(c.islower() for c in password):
            errors.append("Password must include a lowercase letter.")
        if not any(c.isdigit() for c in password):
            errors.append("Password must include a number.")
        if not any(not c.isalnum() for c in password):
            errors.append("Password must include a symbol.")
        return errors

    def hash_password(self, password: str) -> str:
        if bcrypt is not None:
            return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        # Test-environment fallback only. Runtime deployments install bcrypt from requirements.txt.
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 390000).hex()
        return f"pbkdf2_sha256${salt}${digest}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        if password_hash.startswith("pbkdf2_sha256$"):
            try:
                _algorithm, salt, digest = password_hash.split("$", 2)
            except ValueError:
                return False
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 390000).hex()
            return hmac.compare_digest(candidate, digest)
        if bcrypt is None:
            return False
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))

    def create_user(self, username: str, password: str, role: str = "viewer", *, first_name: str = "", last_name: str = "", email: str = "") -> dict[str, Any]:
        username = username.strip()
        if not username:
            raise AuthError("Username is required.")
        if role not in VALID_ROLES:
            raise AuthError("Role must be admin or viewer.")
        errors = self.validate_password_complexity(password)
        if errors:
            raise AuthError(" ".join(errors))
        now = utc_now()
        try:
            with self.connect() as db:
                cursor = db.execute(
                    """
                    INSERT INTO users (username, password_hash, role, is_active, first_name, last_name, email, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (username, self.hash_password(password), role, (first_name or '').strip(), (last_name or '').strip(), (email or '').strip(), now, now),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise AuthError("Username already exists.") from exc
        return self.get_user(user_id)  # type: ignore[return-value]

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, username, first_name, last_name, email, role, is_active, timezone, date_format, time_format, failed_attempts, locked_until, created_at, updated_at, last_login_at FROM users ORDER BY username"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id, username, first_name, last_name, email, role, is_active, timezone, date_format, time_format, failed_attempts, locked_until, created_at, updated_at, last_login_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_user(self, user_id: int, *, role: str | None = None, is_active: bool | None = None, password: str | None = None) -> dict[str, Any]:
        # Fetch the existing row FIRST so we can (a) fail early on a bad user_id,
        # (b) diff incoming role against the stored role to detect a REAL change,
        # and (c) avoid unexpectedly invalidating sessions when a no-op role=SAME
        # submission is replayed by the frontend or a scripted test.
        existing = self.get_user(user_id)
        if not existing:
            raise AuthError("User not found.")
        role_will_change = role is not None and role != existing.get("role")
        updates: list[str] = []
        params: list[Any] = []
        if role is not None:
            if role not in VALID_ROLES:
                raise AuthError("Role must be admin or viewer.")
            updates.append("role = ?")
            params.append(role)
        if is_active is not None:
            updates.append("is_active = ?")
            params.append(int(is_active))
        if password:
            errors = self.validate_password_complexity(password)
            if errors:
                raise AuthError(" ".join(errors))
            updates.append("password_hash = ?")
            params.append(self.hash_password(password))
            updates.append("failed_attempts = 0")
            updates.append("locked_until = NULL")
        if not updates:
            return existing
        updates.append("updated_at = ?")
        params.append(utc_now())
        params.append(user_id)
        with self.connect() as db:
            cursor = db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                raise AuthError("User not found.")
            # Privilege-escalation guard: any ACTUAL privilege change must force
            # re-authentication. Without this, a stolen viewer cookie silently
            # elevates to admin on the next request when an admin promotes the
            # user. ``is_active is False`` already invalidates sessions and is
            # combined here so the single DELETE statement covers both cases.
            if is_active is False or role_will_change:
                db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
            return self.get_user(user_id)  # type: ignore[return-value]

    def update_profile(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        timezone_name: str | None = None,
        date_format: str | None = None,
        time_format: str | None = None,
        theme: str | None = None,
        current_password: str | None = None,
    ) -> dict[str, Any]:
        # H4 fix: require ``current_password`` when the request would
        # change the user's email or username. Non-sensitive fields
        # (first/last name, timezone, formats) still update without
        # proof of possession. The "sensitive" check compares the
        # incoming value against the currently stored row so a
        # round-trip that re-sends the same email does NOT re-prompt
        # for a password (preserves frontend back-compat for
        # "save my profile without changing anything" flows).
        existing = self.get_user(user_id)
        if not existing:
            raise AuthError('User not found.')
        username_changed = (
            username is not None and username.strip() != str(existing.get('username') or '')
        )
        email_changed = (
            email is not None and email.strip() != str(existing.get('email') or '')
        )
        if username_changed or email_changed:
            if not current_password:
                raise AuthError(
                    'Current password is required to change username or email.'
                )
            now_dt = datetime.now(timezone.utc)
            with self.connect() as db:
                row = db.execute(
                    'SELECT * FROM users WHERE id = ?', (user_id,)
                ).fetchone()
                if row is None or not row['is_active']:
                    raise AuthError('Current password is incorrect.')
                if _is_locked_until_future(row['locked_until'], now_dt):
                    raise AuthError(
                        'Account is temporarily locked. Try again later.'
                    )
                if not self.verify_password(current_password, row['password_hash']):
                    raise AuthError('Current password is incorrect.')
        updates: list[str] = []
        params: list[Any] = []
        if username is not None:
            username = username.strip()
            if not username:
                raise AuthError("Username is required.")
            updates.append("username = ?")
            params.append(username)
        if first_name is not None:
            updates.append("first_name = ?")
            params.append(first_name.strip())
        if last_name is not None:
            updates.append("last_name = ?")
            params.append(last_name.strip())
        if email is not None:
            updates.append("email = ?")
            params.append(email.strip())
        if timezone_name is not None:
            timezone_name = timezone_name.strip()
            if not timezone_name:
                raise AuthError("Timezone is required.")
            updates.append("timezone = ?")
            params.append(timezone_name)
        if date_format is not None:
            if date_format not in {"locale", "iso", "us", "au"}:
                raise AuthError("Date format must be locale, iso, us, or au.")
            updates.append("date_format = ?")
            params.append(date_format)
        if time_format is not None:
            if time_format not in {"12h", "24h"}:
                raise AuthError("Time format must be 12h or 24h.")
            updates.append("time_format = ?")
            params.append(time_format)
        if theme is not None:
            if theme not in {"system", "light", "dark"}:
                raise AuthError("Theme must be system, light, or dark.")
            updates.append("theme = ?")
            params.append(theme)
        if not updates:
            # ``existing`` was already loaded at the top of the method
            # for the H4 current-password gate; reuse it here so we
            # don't open a second connection.
            return existing
        updates.append("updated_at = ?")
        params.extend([utc_now(), user_id])
        try:
            with self.connect() as db:
                cursor = db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
                if cursor.rowcount == 0:
                    raise AuthError("User not found.")
        except sqlite3.IntegrityError as exc:
            raise AuthError("Username already exists.") from exc
        return self.get_user(user_id)  # type: ignore[return-value]

    def change_password(self, user_id: int, current_password: str, new_password: str) -> None:
        errors = self.validate_password_complexity(new_password)
        if errors:
            raise AuthError(" ".join(errors))
        now_dt = datetime.now(timezone.utc)
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or not row["is_active"]:
                raise AuthError("Current password is incorrect.")
            if _is_locked_until_future(row["locked_until"], now_dt):
                raise AuthError("Account is temporarily locked. Try again later.")
            if not self.verify_password(current_password, row["password_hash"]):
                raise AuthError("Current password is incorrect.")
            db.execute(
                "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
                (self.hash_password(new_password), utc_now(), user_id),
            )

    def too_many_recent_failures(self, db: sqlite3.Connection, username: str, ip_address: str, now: datetime) -> bool:
        _window_start_raw = (now - self.lockout).isoformat()
        # Defence-in-depth -- cosmetically identical coverage to the
        # recordings / events / camera_diagnostics lifecycle. ``(now -
        # self.lockout).isoformat()`` against a tz-aware UTC ``now`` is
        # already canonical ``+00:00`` so the helper is a no-op on the
        # present call site. The wrap keeps the bound value on the
        # same lexical form as the row's ``created_at`` even if a
        # future caller hands ``too_many_recent_failures`` a tz-aware
        # non-UTC datetime. Idempotent on already-canonical input.
        window_start = _normalize_iso_to_utc(_window_start_raw) or _window_start_raw
        row = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM login_attempts
            WHERE success = 0
              AND created_at >= ?
              AND (username = ? OR ip_address = ?)
            """,
            (window_start, username, ip_address),
        ).fetchone()
        return int(row["count"]) >= self.max_login_attempts

    def authenticate(self, username: str, password: str, ip_address: str) -> tuple[dict[str, Any], str, str, str]:
        username = username.strip()
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            now_dt = datetime.now(timezone.utc)
            _now_raw = now_dt.isoformat()
            # Defence-in-depth -- cosmetically identical coverage to the
            # recordings / events / camera_diagnostics lifecycle.
            # ``datetime.now(timezone.utc).isoformat()`` is canonical
            # ``+00:00`` by construction so the helper is a no-op on the
            # present source. The wrap ensures the ``login_attempts``
            # ``INSERT`` below binds a canonical value even if any
            # future patch sources ``now`` from a different shape
            # (tz-aware non-UTC datetime, naive datetime assembled via
            # ``.isoformat()``, etc.). Idempotent on already-canonical
            # input.
            now = _normalize_iso_to_utc(_now_raw) or _now_raw
            success = False
            try:
                if row is None or not row["is_active"]:
                    # Defend against username enumeration: run an equivalent-
                    # cost password verification so the unknown-user latency
                    # matches the known-user / wrong-password latency. See
                    # ``_equalize_password_timing`` for the equaliser details.
                    _equalize_password_timing(password)
                    if self.too_many_recent_failures(db, username, ip_address, now_dt):
                        raise AuthError("Too many failed login attempts. Try again later.")
                    raise AuthError("Invalid username or password.")
                if _is_locked_until_future(row["locked_until"], now_dt):
                    raise AuthError("Account is temporarily locked. Try again later.")
                if self.too_many_recent_failures(db, username, ip_address, now_dt):
                    raise AuthError("Too many failed login attempts. Try again later.")
                if not self.verify_password(password, row["password_hash"]):
                    failures = int(row["failed_attempts"]) + 1
                    locked_until = None
                    if failures >= self.max_login_attempts:
                        # Defence-in-depth -- cosmetically identical coverage to the
                        # rest of the users / login_attempts lifecycle.
                        # ``(now_dt + self.lockout).isoformat()`` against a tz-aware
                        # UTC ``now_dt`` is already canonical ``+00:00`` so the helper
                        # is a no-op on the present source. The wrap routes any
                        # FUTURE change (different tz derivation, naive ``datetime``,
                        # f-string of local time) through the same canonicaliser so
                        # the storage form on the ``UPDATE`` below stays uniform.
                        # Idempotent on already-canonical input.
                        _locked_raw = (now_dt + self.lockout).isoformat()
                        locked_until = _normalize_iso_to_utc(_locked_raw) or _locked_raw
                    db.execute(
                        "UPDATE users SET failed_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
                        (failures, locked_until, now, row["id"]),
                    )
                    raise AuthError("Invalid username or password.")
                token = secrets.token_urlsafe(48)
                csrf_token = secrets.token_urlsafe(32)
                expires_at = (now_dt + self.session_timeout).isoformat()
                db.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL, updated_at = ?, last_login_at = ? WHERE id = ?", (now, now, row["id"]))
                db.execute(
                    "INSERT INTO user_sessions (session_token, user_id, csrf_token, created_at, expires_at, last_seen_at, absolute_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (token, row["id"], csrf_token, now, expires_at, now, (now_dt + self.absolute_session_lifetime).isoformat()),
                )
                success = True
                return self.public_user(row), token, csrf_token, expires_at
            finally:
                try:
                    db.execute(
                        "INSERT INTO login_attempts (username, ip_address, success, created_at) VALUES (?, ?, ?, ?)",
                        (username, ip_address, int(success), now),
                    )
                    db.commit()
                except Exception as audit_write_exc:
                    # Best-effort audit-write. A missing row here is a
                    # compliance-traceability gap, not an auth failure --
                    # the surrounding flow still committed the user's
                    # outcome -- but the operator deserves a breadcrumb if
                    # the audit log ever falls over (disk full, SQLite
                    # locked, permission denied). We intentionally do NOT
                    # raised-and-propagate: that would convert a 5-minute
                    # disk hiccup into a full login outage.
                    _logging.getLogger('daygle.ai').warning(
                        'login_attempts audit-write failed for %s/%s: %s',
                        username, ip_address, audit_write_exc,
                    )

    # Lazy-renewal window: any session that hasn't been touched in this much
    # wall clock gets its ``expires_at`` extended by ``session_timeout`` on the
    # next authenticated read. 5 minutes balances write amplification (cheap
    # SELECT-then-UPDATE per call) against staleness (a refresh metadata in
    # the browser typically fires every few minutes when the tab is active;
    # 5 minutes leaves a wide margin to the default 12 h timeout). Tighter /
    # wider values are both safe; this default matches common CRUD app
    # practice for sliding windows.
    _SESSION_RENEWAL_INTERVAL = timedelta(minutes=5)

    def _renew_session_if_stale(self, db: sqlite3.Connection, session_token: str, current_expires_at: str, now_dt: datetime) -> str:
        """Extend ``expires_at`` by ``session_timeout`` if the session hasn't
        been touched in the last ``_SESSION_RENEWAL_INTERVAL`` minutes.
        Returns the (possibly renewed) ``expires_at`` ISO string so the
        caller can pass it back to the client. This is the server-side side
        of the timeout UX fix in ``web/utils.js`` - every authenticated read
        silently keeps the session alive while the user is actively using
        the app, so an idle tab returning to the foreground is no longer
        greeted by "Session expired" because the very GET that woke it
        already extended the row.
        """
        try:
            current_exp_dt = datetime.fromisoformat(current_expires_at)
        except (TypeError, ValueError):
            return current_expires_at
        # If the row was last renewed < ``_SESSION_RENEWAL_INTERVAL`` ago we
        # leave it alone. ``-now_dt`` is the inverse delta - last renew time
        # isn't on the row, so use expires_at as the proxy: a session that
        # still has plenty of runway was almost certainly just refreshed.
        remaining = current_exp_dt - now_dt
        if remaining >= self.session_timeout - self._SESSION_RENEWAL_INTERVAL:
            return current_expires_at
        new_expires_at = (now_dt + self.session_timeout).isoformat()
        db.execute(
            "UPDATE user_sessions SET expires_at = ?, last_seen_at = ? WHERE session_token = ?",
            (new_expires_at, now_dt.isoformat(), session_token),
        )
        return new_expires_at

    def get_session(self, session_token: str | None) -> dict[str, Any] | None:
        if not session_token:
            return None
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT s.session_token, s.csrf_token, s.expires_at, s.absolute_expires_at, u.id, u.username, u.role, u.is_active,
                       u.first_name, u.last_name, u.email, u.timezone, u.date_format, u.time_format, u.theme
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.session_token = ?
                """,
                (session_token,),
            ).fetchone()
            if row is None:
                return None
            if not row["is_active"] or datetime.fromisoformat(row["expires_at"]) <= now_dt:
                db.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))
                return None
            # H2 absolute-expiry guard: refuse any session whose hard cap has
            # elapsed, EVEN if the sliding ``expires_at`` was kept fresh by
            # ``_renew_session_if_stale``. The cap is set at session creation
            # and never extended, so a stolen cookie + active user still
            # loses after ``absolute_session_lifetime`` from sign-in.
            absolute_expires_at_raw = row["absolute_expires_at"] if "absolute_expires_at" in row.keys() else None
            if absolute_expires_at_raw:
                try:
                    if datetime.fromisoformat(absolute_expires_at_raw) <= now_dt:
                        db.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))
                        return None
                except (TypeError, ValueError):
                    # Garbled legacy row: treat as expired -- refuse.
                    db.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))
                    return None
            expires_at = self._renew_session_if_stale(db, row["session_token"], row["expires_at"], now_dt)
            # Keep last_seen_at fresh regardless of whether we renewed. The
            # ``_renew_session_if_stale`` already sets it on the renew path so
            # only issue the no-op UPDATE when the session was just-up-to-date.
            if expires_at == row["expires_at"]:
                db.execute("UPDATE user_sessions SET last_seen_at = ? WHERE session_token = ?", (now, session_token))
            return {"session_token": row["session_token"], "csrf_token": row["csrf_token"], "expires_at": expires_at, "user": self.public_user(row)}

    def delete_session(self, session_token: str | None) -> None:
        if not session_token:
            return
        with self.connect() as db:
            db.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))

    def cleanup_expired_sessions(self) -> None:
        # M2 (round-7): the whole method is best-effort. Any per-phase failure
        # (expired-session sweep, login_attempts purge, camera_diagnostics
        # purge) is logged at warning level and swallowed so a transient
        # backend hiccup never breaks the caller's maintenance tick. This is
        # the contract ``test_purge_is_best_effort_on_exception`` exercises.
        try:
            with self.connect() as db:
                now_str = utc_now()
                # H2: also drop any session whose absolute_expires_at has elapsed,
                # since the sliding ``expires_at`` may still be in the future.
                db.execute(
                    "DELETE FROM user_sessions "
                    "WHERE expires_at <= ? "
                    "OR (absolute_expires_at IS NOT NULL AND absolute_expires_at <= ?)",
                    (now_str, now_str),
                )
        except Exception as exc:
            import logging as _logging
            _logging.getLogger('daygle.ai').warning(
                'cleanup_expired_sessions: session sweep failed: %s', exc
            )
        # M2 (round-7): purge ``login_attempts`` rows older than a fixed
        # 90-day window. Cheap DELETE (low-volume table) without VACUUM /
        # WAL-checkpoint per the M2 design verdict. Idempotent.
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            cutoff_iso = (_dt.now(_tz.utc) - _td(days=90)).isoformat()
            with self.connect() as db:
                db.execute(
                    'DELETE FROM login_attempts WHERE created_at < ?',
                    (cutoff_iso,),
                )
        except Exception as login_purge_exc:
            # Best-effort; never let audit-log-adjacent cleanup crash
            # session cleanup. Admin can re-run manually if needed.
            # Logged at warning level so a long-running retention bug
            # (disk full, schema mismatch, corrupted row) is visible
            # rather than silently failing forever.
            import logging as _logging
            _logging.getLogger('daygle.ai').warning(
                'cleanup_expired_sessions: login_attempts purge failed: %s',
                login_purge_exc,
            )
        # M3 wiring (round-7): the camera_diagnostics purger already lives
        # at ``app.backup.purge_camera_diagnostics_by_policy``; we just
        # invoke it from this same regularly-scheduled maintenance point
        # so it actually fires without operator intervention. Audit-log
        # purger is intentionally NOT wired here -- the immutability
        # trigger in ``app.db.audit`` MUST NOT be bypassed (round-7 M3 NO-GO).
        try:
            from app.backup import purge_camera_diagnostics_by_policy
            purge_camera_diagnostics_by_policy()
        except Exception as diag_purge_exc:
            # Same best-effort policy as the login_attempts sweep above:
            # a failed diagnostics purge should NOT collapse the
            # surrounding session-sweep cleanup, but a recurrence MUST
            # show up in the operator log so disk-/schema-related
            # retention bugs are diagnosable rather than silent.
            import logging as _logging
            _logging.getLogger('daygle.ai').warning(
                'cleanup_expired_sessions: camera_diagnostics purge failed: %s',
                diag_purge_exc,
            )

    def public_user(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "email": row["email"],
            "role": row["role"],
            "is_active": bool(row["is_active"]),
            "timezone": row["timezone"],
            "date_format": row["date_format"],
            "time_format": row["time_format"],
            "theme": row["theme"],
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
