"""SQLite access layer. Money is stored as integer cents everywhere."""
import os
import sqlite3
from contextlib import contextmanager

# `or` not a default arg: an empty value in .env must fall back, not win.
DB_PATH = os.environ.get("ITAM_DB") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "itam.db")
CURRENCY = os.environ.get("ITAM_CURRENCY") or "USD"

DEFAULT_CATEGORIES = ["Laptop", "Desktop", "Monitor", "Phone", "Peripheral", "Software", "Other"]

# Fields an incoming webhook payload is allowed to populate.
ASSET_FIELDS = ["name", "category", "cost", "serial", "purchased_on", "notes",
                "assigned_upn", "external_id"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    upn            TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    job_title      TEXT,
    department     TEXT,
    entra_id       TEXT,
    account_enabled INTEGER NOT NULL DEFAULT 1,
    country        TEXT,
    usage_location TEXT,
    source         TEXT NOT NULL DEFAULT 'manual',
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
    assigned_on  TEXT,
    external_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_upn ON assets(assigned_upn);

-- Licences the tenant owns, straight from Entra. Keyed on the SKU id the
-- tenant reports; the string id (skuPartNumber) is what names are matched on,
-- because published GUID lists disagree with each other.
CREATE TABLE IF NOT EXISTS licenses (
    sku_id          TEXT PRIMARY KEY,
    sku_part_number TEXT,
    display_name    TEXT,
    prepaid         INTEGER NOT NULL DEFAULT 0,
    consumed        INTEGER NOT NULL DEFAULT 0,
    synced_at       TEXT
);

CREATE TABLE IF NOT EXISTS user_licenses (
    upn    TEXT NOT NULL REFERENCES users(upn) ON DELETE CASCADE,
    sku_id TEXT NOT NULL REFERENCES licenses(sku_id) ON DELETE CASCADE,
    PRIMARY KEY (upn, sku_id)
);
CREATE INDEX IF NOT EXISTS idx_user_licenses_sku ON user_licenses(sku_id);

-- Entra ID groups and their membership.
CREATE TABLE IF NOT EXISTS groups (
    id           TEXT PRIMARY KEY,        -- Entra group object id
    display_name TEXT NOT NULL,
    description  TEXT,
    member_count INTEGER NOT NULL DEFAULT 0,
    synced_at    TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    upn      TEXT NOT NULL REFERENCES users(upn) ON DELETE CASCADE,
    PRIMARY KEY (group_id, upn)
);
CREATE INDEX IF NOT EXISTS idx_group_members_upn ON group_members(upn);

-- Devices from Intune. Kept separate from assets: a device is what Intune
-- reports, an asset is what you paid for. They are linked by serial number.
CREATE TABLE IF NOT EXISTS devices (
    id               TEXT PRIMARY KEY,   -- Intune managedDevice id
    device_name      TEXT,
    serial_number    TEXT,
    manufacturer     TEXT,
    model            TEXT,
    os               TEXT,
    os_version       TEXT,
    primary_upn      TEXT,
    compliance_state TEXT,
    enrolled_at      TEXT,
    last_contact     TEXT,
    storage_total    INTEGER,
    storage_free     INTEGER,
    synced_at        TEXT,
    asset_id         INTEGER REFERENCES assets(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_serial ON devices(serial_number);
CREATE INDEX IF NOT EXISTS idx_devices_upn ON devices(primary_upn);

-- Intune custom attributes (macOS shell script results), merged onto a device.
CREATE TABLE IF NOT EXISTS device_attributes (
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    value        TEXT,
    collected_at TEXT,
    PRIMARY KEY (device_id, name)
);

-- Entitlement rules: what members of a group should have.
CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    group_id        TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,       -- 'asset' or 'subscription'
    category        TEXT,                -- asset category, when kind='asset'
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE CASCADE,
    quantity        INTEGER NOT NULL DEFAULT 1,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    token_hash        TEXT NOT NULL UNIQUE,
    prefix            TEXT NOT NULL,
    can_create_assets INTEGER NOT NULL DEFAULT 1,
    can_assign        INTEGER NOT NULL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT,
    last_used_at      TEXT
);

-- Maps a field name in the incoming payload to an ITAM asset field.
CREATE TABLE IF NOT EXISTS api_field_map (
    source_field TEXT PRIMARY KEY,
    target_field TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    key_name TEXT,
    endpoint TEXT,
    status   INTEGER,
    message  TEXT,
    payload  TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_log_at ON api_log(at DESC);

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


DEFAULT_FIELD_MAP = {f: f for f in ASSET_FIELDS}

# A reserved pseudo-group so rules can target everyone without waiting for a
# group sync. It satisfies the rules.group_id foreign key like any other row.
ALL_USERS_GROUP = "__all_users__"
ALL_USERS_LABEL = "Everyone (all users)"


def init_db():
    with cursor() as conn:
        conn.executescript(SCHEMA)

        # Migration: databases created before the API existed have no
        # assets.external_id. Add it, then index it - in that order, which is
        # why the index is not part of SCHEMA above.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets)")]
        if "external_id" not in cols:
            conn.execute("ALTER TABLE assets ADD COLUMN external_id TEXT")

        # Migration: country and usage location arrived after the first release.
        ucols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        for col in ("country", "usage_location"):
            if col not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
        # NULLs repeat freely in a SQLite unique index, so only real ids are
        # constrained - which is what makes webhook retries idempotent.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_external "
                     "ON assets(external_id)")

        conn.execute(
            """INSERT INTO groups (id, display_name, description, member_count, synced_at)
               VALUES (?,?,?,0,NULL)
               ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name""",
            (ALL_USERS_GROUP, ALL_USERS_LABEL,
             "Built in: every synced user, no Entra group needed"))

        if not conn.execute("SELECT 1 FROM api_field_map LIMIT 1").fetchone():
            conn.executemany("INSERT INTO api_field_map (source_field, target_field) VALUES (?,?)",
                             DEFAULT_FIELD_MAP.items())


def categories() -> list[str]:
    """Built-in categories plus anything the API or a user has introduced."""
    seen = {r["category"] for r in q("SELECT DISTINCT category FROM assets") if r["category"]}
    return sorted(seen | set(DEFAULT_CATEGORIES))


def field_map() -> dict:
    return {r["source_field"]: r["target_field"] for r in
            q("SELECT * FROM api_field_map ORDER BY source_field")}


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
