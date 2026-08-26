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
# Defaults to on: the shipped deployment terminates TLS in front of the app.
COOKIE_SECURE = (os.environ.get("ITAM_COOKIE_SECURE") or "1").lower() in ("1", "true", "yes")
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

def check_credentials(username: str, password: str):
    """Verify the password step only. Returns the user row, or None."""
    username = (username or "").strip().lower()
    user = get_user(username)
    if not user or not verify_password(password or "", user["password_hash"]):
        # Same cost either way, so a missing user is indistinguishable from a
        # bad password.
        if not user:
            hash_password(password or "")
        record_failure(username)
        return None
    clear_failures(username)
    return user


def login(username: str, password: str):
    """Password-only sign-in. Returns (token, user) or (None, None).

    Refuses an account with two-factor enabled: that path must go through the
    second step, so no caller can accidentally bypass it.
    """
    user = check_credentials(username, password)
    if not user or user["totp_enabled"]:
        return None, None
    return issue_session(user["username"]), user


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


# --- two-factor (TOTP, RFC 6238) -----------------------------------------
#
# Standard 6-digit / 30-second / SHA-1 TOTP, so Microsoft Authenticator,
# Google Authenticator, 1Password and the rest all work. Implemented on the
# standard library; no crypto dependency.

import base64
import struct

TOTP_DIGITS = 6
TOTP_STEP = 30
TOTP_SKEW = 1           # accept the neighbouring windows for clock drift
RECOVERY_CODES = 8
PENDING_MINUTES = 5
ISSUER = os.environ.get("ITAM_TOTP_ISSUER") or "ITAM"


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_uri(username: str, secret: str) -> str:
    from urllib.parse import quote
    label = quote(f"{ISSUER}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(ISSUER)}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def _totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def current_step() -> int:
    return int(_now().timestamp()) // TOTP_STEP


def totp_already_used(secret: str, code: str, last_step: int | None) -> bool:
    """True when the code is genuinely this account's but its window is spent.

    Worth telling apart from a wrong code: it is what happens when someone
    signs in twice inside the same 30 seconds, and "wait for the next code" is
    actionable where "invalid" is not.
    """
    code = (code or "").strip().replace(" ", "")
    if not secret or last_step is None or not code.isdigit():
        return False
    now = current_step()
    for step in range(now - TOTP_SKEW, now + TOTP_SKEW + 1):
        if step <= last_step and hmac.compare_digest(_totp_at(secret, step), code):
            return True
    return False


def verify_totp(secret: str, code: str, last_step: int | None) -> int | None:
    """Returns the matched step, or None. Rejects a step already used, so a
    code cannot be replayed inside its own window."""
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    now = current_step()
    for step in range(now - TOTP_SKEW, now + TOTP_SKEW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None


def begin_totp_setup(username: str) -> str:
    """Store a secret but leave 2FA off until a code proves the app works."""
    secret = new_totp_secret()
    db.execute("UPDATE auth_users SET totp_secret = ?, totp_enabled = 0 WHERE username = ?",
               (secret, username))
    return secret


def confirm_totp(username: str, code: str) -> bool:
    user = get_user(username)
    if not user or not user["totp_secret"]:
        return False
    step = verify_totp(user["totp_secret"], code, user["totp_last_step"])
    if step is None:
        return False
    db.execute(
        "UPDATE auth_users SET totp_enabled = 1, totp_last_step = ? WHERE username = ?",
        (step, username))
    return True


def disable_totp(username: str) -> None:
    db.execute(
        """UPDATE auth_users SET totp_enabled = 0, totp_secret = NULL, totp_last_step = NULL
           WHERE username = ?""", (username,))
    db.execute("DELETE FROM auth_recovery_codes WHERE username = ?", (username,))


# --- recovery codes ------------------------------------------------------

def _recovery_hash(code: str) -> str:
    return hashlib.sha256(code.strip().replace("-", "").upper().encode()).hexdigest()


def issue_recovery_codes(username: str) -> list[str]:
    """Replaces any existing codes. Returned once, stored only as hashes."""
    db.execute("DELETE FROM auth_recovery_codes WHERE username = ?", (username,))
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = secrets.token_hex(5).upper()          # 10 hex chars
        codes.append(f"{raw[:5]}-{raw[5:]}")
        db.execute(
            "INSERT OR IGNORE INTO auth_recovery_codes (username, code_hash) VALUES (?,?)",
            (username, _recovery_hash(raw)))
    return codes


def recovery_codes_left(username: str) -> int:
    return db.q1(
        "SELECT COUNT(*) c FROM auth_recovery_codes WHERE username = ? AND used_at IS NULL",
        (username,))["c"]


def use_recovery_code(username: str, code: str) -> bool:
    row = db.q1(
        """SELECT code_hash FROM auth_recovery_codes
           WHERE username = ? AND code_hash = ? AND used_at IS NULL""",
        (username, _recovery_hash(code or "")))
    if not row:
        return False
    db.execute(
        "UPDATE auth_recovery_codes SET used_at = ? WHERE username = ? AND code_hash = ?",
        (_iso(_now()), username, row["code_hash"]))
    return True


# --- half-finished sign-ins ---------------------------------------------

def start_pending(username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    db.execute(
        """INSERT INTO auth_2fa_pending (token, username, created_at, expires_at)
           VALUES (?,?,?,?)""",
        (token, username, _iso(now),
         _iso(now + datetime.timedelta(minutes=PENDING_MINUTES))))
    return token


def pending_user(token: str | None):
    if not token:
        return None
    row = db.q1(
        """SELECT u.* FROM auth_2fa_pending p JOIN auth_users u ON u.username = p.username
           WHERE p.token = ? AND p.expires_at > ?""", (token, _iso(_now())))
    return row


def clear_pending(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM auth_2fa_pending WHERE token = ?", (token,))


def purge_pending() -> None:
    db.execute("DELETE FROM auth_2fa_pending WHERE expires_at <= ?", (_iso(_now()),))


def require_2fa() -> bool:
    return (os.environ.get("ITAM_REQUIRE_2FA") or "").lower() in ("1", "true", "yes")


def issue_session(username: str) -> str:
    """Create a signed-in session. Used once both factors are satisfied."""
    token = secrets.token_urlsafe(32)
    now = _now()
    db.execute(
        "INSERT INTO auth_sessions (token, username, created_at, expires_at) VALUES (?,?,?,?)",
        (token, username, _iso(now), _iso(now + datetime.timedelta(hours=SESSION_HOURS))))
    db.execute("UPDATE auth_users SET last_login = ? WHERE username = ?", (_iso(now), username))
    return token
