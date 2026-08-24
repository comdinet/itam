"""Local username + password sign-in.

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes (stdlib only).
Sessions live in the database so they can be revoked and expire server-side;
the cookie carries nothing but an opaque random token.
"""
import datetime
import hashlib
import hmac
import os
import secrets

from . import db

COOKIE = "itam_session"
SESSION_HOURS = int(os.environ.get("ITAM_SESSION_HOURS") or 12)
COOKIE_SECURE = (os.environ.get("ITAM_COOKIE_SECURE") or "").lower() in ("1", "true", "yes")
ITERATIONS = 400_000
MAX_FAILURES = 8            # per username, per process
LOCKOUT_MINUTES = 15

_failures: dict[str, list] = {}   # username -> [count, locked_until]


# --- password hashing ----------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def password_problem(password: str) -> str | None:
    """Returns a complaint string, or None if the password is acceptable."""
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if password.lower() in ("password1234", "changeme1234", "itamitamitam"):
        return "Please choose a less predictable password."
    return None


# --- time helpers --------------------------------------------------------

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


# --- accounts ------------------------------------------------------------

def create_user(username: str, password: str, is_admin: bool = False,
                must_change: bool = False) -> None:
    db.execute(
        """INSERT INTO auth_users (username, password_hash, is_admin, must_change, created_at)
           VALUES (?,?,?,?,?)""",
        (username.strip().lower(), hash_password(password), 1 if is_admin else 0,
         1 if must_change else 0, _iso(_now())))


def set_password(username: str, password: str) -> None:
    db.execute(
        "UPDATE auth_users SET password_hash = ?, must_change = 0 WHERE username = ?",
        (hash_password(password), username))


def get_user(username: str):
    return db.q1("SELECT * FROM auth_users WHERE username = ?", (username.strip().lower(),))


def list_users():
    return db.q("SELECT * FROM auth_users ORDER BY username")


def delete_user(username: str) -> None:
    db.execute("DELETE FROM auth_users WHERE username = ?", (username,))


def bootstrap() -> str | None:
    """Create the first admin account if none exists.

    Uses ITAM_ADMIN_PASSWORD when set; otherwise generates one, returns it for
    the caller to print, and forces a change at first sign-in.
    """
    if db.q1("SELECT 1 FROM auth_users LIMIT 1"):
        return None
    username = (os.environ.get("ITAM_ADMIN_USER") or "admin").strip().lower()
    password = os.environ.get("ITAM_ADMIN_PASSWORD")
    if password:
        create_user(username, password, is_admin=True, must_change=False)
        return None
    password = secrets.token_urlsafe(12)
    create_user(username, password, is_admin=True, must_change=True)
    return f"{username} / {password}"


# --- brute-force throttling ---------------------------------------------

def is_locked(username: str) -> int:
    """Remaining lockout in minutes, or 0."""
    rec = _failures.get(username)
    if not rec or not rec[1]:
        return 0
    remaining = (rec[1] - _now()).total_seconds()
    if remaining <= 0:
        _failures.pop(username, None)
        return 0
    return max(1, int(remaining // 60) + 1)


def record_failure(username: str) -> None:
    rec = _failures.setdefault(username, [0, None])
    rec[0] += 1
    if rec[0] >= MAX_FAILURES:
        rec[1] = _now() + datetime.timedelta(minutes=LOCKOUT_MINUTES)
        rec[0] = 0


def clear_failures(username: str) -> None:
    _failures.pop(username, None)


# --- sessions ------------------------------------------------------------

def login(username: str, password: str):
    """Returns (token, user) on success, or (None, None)."""
    username = (username or "").strip().lower()
    user = get_user(username)
    if not user or not verify_password(password or "", user["password_hash"]):
        # Same cost either way, so a missing user is indistinguishable from a bad password.
        if not user:
            hash_password(password or "")
        record_failure(username)
        return None, None
    clear_failures(username)
    token = secrets.token_urlsafe(32)
    now = _now()
    db.execute(
        "INSERT INTO auth_sessions (token, username, created_at, expires_at) VALUES (?,?,?,?)",
        (token, username, _iso(now), _iso(now + datetime.timedelta(hours=SESSION_HOURS))))
    db.execute("UPDATE auth_users SET last_login = ? WHERE username = ?", (_iso(now), username))
    return token, user


def current_user(token: str | None):
    if not token:
        return None
    row = db.q1(
        """SELECT u.* FROM auth_sessions s JOIN auth_users u ON u.username = s.username
           WHERE s.token = ? AND s.expires_at > ?""", (token, _iso(_now())))
    return row


def logout(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


def revoke_all(username: str) -> None:
    db.execute("DELETE FROM auth_sessions WHERE username = ?", (username,))


def purge_expired() -> None:
    db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (_iso(_now()),))
