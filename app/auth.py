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

_bcrypt_spec = importlib.util.find_spec("bcrypt")
bcrypt = importlib.import_module("bcrypt") if _bcrypt_spec else None

SESSION_COOKIE = "daygle_session"
CSRF_COOKIE = "daygle_csrf"
CSRF_HEADER = "X-CSRF-Token"
VALID_ROLES = {"admin", "viewer"}


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
        self.init()

    def apply_config(self, config: dict[str, Any]) -> None:
        self.config.update(config)
        self.session_timeout = timedelta(hours=float(self.config.get("session_timeout_hours", 12)))
        self.max_login_attempts = int(self.config.get("max_login_attempts", 5))
        self.lockout = timedelta(minutes=float(self.config.get("lockout_minutes", 15)))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL;')
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
            _algorithm, salt, digest = password_hash.split("$", 2)
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
            existing = self.get_user(user_id)
            if not existing:
                raise AuthError("User not found.")
            return existing
        updates.append("updated_at = ?")
        params.append(utc_now())
        params.append(user_id)
        with self.connect() as db:
            cursor = db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            if cursor.rowcount == 0:
                raise AuthError("User not found.")
            if is_active is False:
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
    ) -> dict[str, Any]:
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
        if not updates:
            existing = self.get_user(user_id)
            if not existing:
                raise AuthError("User not found.")
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
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or not self.verify_password(current_password, row["password_hash"]):
                raise AuthError("Current password is incorrect.")
        self.update_user(user_id, password=new_password)

    def too_many_recent_failures(self, db: sqlite3.Connection, username: str, ip_address: str, now: datetime) -> bool:
        window_start = (now - self.lockout).isoformat()
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
            now = now_dt.isoformat()
            success = False
            try:
                if row is None or not row["is_active"]:
                    if self.too_many_recent_failures(db, username, ip_address, now_dt):
                        raise AuthError("Too many failed login attempts. Try again later.")
                    raise AuthError("Invalid username or password.")
                if row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > now_dt:
                    raise AuthError("Account is temporarily locked. Try again later.")
                if self.too_many_recent_failures(db, username, ip_address, now_dt):
                    raise AuthError("Too many failed login attempts. Try again later.")
                if not self.verify_password(password, row["password_hash"]):
                    failures = int(row["failed_attempts"]) + 1
                    locked_until = None
                    if failures >= self.max_login_attempts:
                        locked_until = (now_dt + self.lockout).isoformat()
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
                    "INSERT INTO user_sessions (session_token, user_id, csrf_token, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (token, row["id"], csrf_token, now, expires_at, now),
                )
                success = True
                return self.public_user(row), token, csrf_token, expires_at
            finally:
                db.execute(
                    "INSERT INTO login_attempts (username, ip_address, success, created_at) VALUES (?, ?, ?, ?)",
                    (username, ip_address, int(success), now),
                )
                db.commit()

    def get_session(self, session_token: str | None) -> dict[str, Any] | None:
        if not session_token:
            return None
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT s.session_token, s.csrf_token, s.expires_at, u.id, u.username, u.role, u.is_active,
                       u.first_name, u.last_name, u.email, u.timezone, u.date_format, u.time_format
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
            db.execute("UPDATE user_sessions SET last_seen_at = ? WHERE session_token = ?", (now, session_token))
            return {"session_token": row["session_token"], "csrf_token": row["csrf_token"], "expires_at": row["expires_at"], "user": self.public_user(row)}

    def delete_session(self, session_token: str | None) -> None:
        if not session_token:
            return
        with self.connect() as db:
            db.execute("DELETE FROM user_sessions WHERE session_token = ?", (session_token,))

    def cleanup_expired_sessions(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (utc_now(),))

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
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
