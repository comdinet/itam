"""SQLite access layer. Money is stored as integer cents everywhere."""
import os
import sqlite3
from contextlib import contextmanager

# `or` not a default arg: an empty value in .env must fall back, not win.
DB_PATH = os.environ.get("ITAM_DB") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "itam.db")
CURRENCY = os.environ.get("ITAM_CURRENCY") or "USD"

CATEGORIES = ["Laptop", "Desktop", "Monitor", "Phone", "Peripheral", "Software", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    upn            TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    job_title      TEXT,
    department     TEXT,
    entra_id       TEXT,
    account_enabled INTEGER NOT NULL DEFAULT 1,
    source         TEXT NOT NULL DEFAULT 'seed',
    synced_at      TEXT
);

CREATE TABLE IF NOT EXISTS auth_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    must_change   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL REFERENCES auth_users(username) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(username);

CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'Other',
    cost_cents   INTEGER NOT NULL DEFAULT 0,
    serial       TEXT,
    purchased_on TEXT,
    notes        TEXT,
    assigned_upn TEXT REFERENCES users(upn) ON DELETE SET NULL,
    assigned_on  TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_upn ON assets(assigned_upn);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    vendor                TEXT,
    monthly_cost_cents    INTEGER NOT NULL DEFAULT 0,  -- per seat, per month
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS subscription_seats (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    upn             TEXT NOT NULL REFERENCES users(upn) ON DELETE CASCADE,
    assigned_on     TEXT,
    PRIMARY KEY (subscription_id, upn)
);
CREATE INDEX IF NOT EXISTS idx_seats_upn ON subscription_seats(upn);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # readers don't block the writer
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with cursor() as conn:
        conn.executescript(SCHEMA)


def q(sql, params=()):
    with cursor() as conn:
        return conn.execute(sql, params).fetchall()


def q1(sql, params=()):
    with cursor() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql, params=()):
    with cursor() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


# --- money helpers -------------------------------------------------------

def to_cents(value) -> int:
    """Parse user-typed money into integer cents.

    Handles both conventions ('1,299.99' and '1.299,99') plus space-grouped
    input. A single separator followed by exactly three digits is read as a
    thousands separator ('1,250' -> 1250.00), since three-decimal money is
    not a thing; one or two trailing digits mean it is a decimal separator.
    """
    if value is None:
        return 0
    s = str(value).strip()
    for space in (" ", "\u00a0", "\u202f", "_"):
        s = s.replace(space, "")
    if not s:
        return 0

    neg = s.startswith("-")
    s = s.lstrip("+-")

    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        # The rightmost separator is the decimal one; the other groups digits.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        head, _, tail = s.rpartition(sep)
        # Repeated separator, or a 3-digit tail after real digits, groups thousands.
        if s.count(sep) > 1 or (len(tail) == 3 and head.isdigit() and head):
            s = s.replace(sep, "")
        else:
            s = s.replace(sep, ".")

    try:
        cents = int(round(float(s) * 100))
    except ValueError:
        return 0
    return -cents if neg else cents


def money(cents) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100:,}.{cents % 100:02d}"
