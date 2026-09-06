"""SQLite access layer. Money is stored as integer cents everywhere."""
import os
import sqlite3
from contextlib import contextmanager

# `or` not a default arg: an empty value in .env must fall back, not win.
DB_PATH = os.environ.get("ITAM_DB") or os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "itam.db")


DEFAULT_CATEGORIES = ["Laptop", "Desktop", "Monitor", "Peripheral", "Software", "Other"]

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
    ignored_reason TEXT,          -- which rule keeps them out of ITAM's lists
    synced_at      TEXT
);

-- Settings changed in the UI. A row here overrides the environment; delete it
-- and the .env value (or the built-in default) applies again.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS auth_users (
    username       TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    must_change    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT,
    last_login     TEXT,
    totp_secret    TEXT,
    totp_enabled   INTEGER NOT NULL DEFAULT 0,
    totp_last_step INTEGER,
    sso            INTEGER NOT NULL DEFAULT 0
);

-- Single-use codes for when the authenticator app is gone.
CREATE TABLE IF NOT EXISTS auth_recovery_codes (
    username  TEXT NOT NULL REFERENCES auth_users(username) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at   TEXT,
    PRIMARY KEY (username, code_hash)
);

-- An AuthnRequest we sent to the IdP. The reply must quote one of these back
-- in InResponseTo, which is what stops a stray or injected assertion being
-- accepted as a sign-in.
CREATE TABLE IF NOT EXISTS saml_requests (
    request_id TEXT PRIMARY KEY,
    next_url   TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Assertion ids already consumed, so a captured response cannot be replayed.
CREATE TABLE IF NOT EXISTS saml_seen (
    assertion_id TEXT PRIMARY KEY,
    seen_at      TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

-- Holds a sign-in that passed the password step and still owes a second
-- factor. Short lived, so an abandoned half-login cannot be resumed later.
CREATE TABLE IF NOT EXISTS auth_2fa_pending (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL REFERENCES auth_users(username) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
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
    external_id  TEXT,
    currency     TEXT,
    rate_micro   INTEGER          -- rate at entry: what was paid stays what was paid
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

-- Members Entra reports for a group that are not users here, usually because
-- ENTRA_USER_FILTER excludes them. Kept so the group page can say who is
-- missing instead of just how many.
CREATE TABLE IF NOT EXISTS group_members_unlinked (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    upn      TEXT NOT NULL,
    PRIMARY KEY (group_id, upn)
);

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
    azure_device_id  TEXT,               -- Entra device object id (azureADDeviceId)
    ignored_reason   TEXT,               -- which rule hid it, recomputed on sync
    synced_at        TEXT,
    asset_id         INTEGER REFERENCES assets(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_serial ON devices(serial_number);
CREATE INDEX IF NOT EXISTS idx_devices_upn ON devices(primary_upn);

-- Devices you do not want to see. Virtual machines live in an Entra group and
-- are not kit anybody holds; neither are test rigs or loan pool spares. They
-- are still synced, so the list of what is being hidden - and why - is always
-- answerable, and un-ignoring is instant rather than a re-sync.
CREATE TABLE IF NOT EXISTS device_ignore_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    field      TEXT NOT NULL,   -- group | device | device_name | model | manufacturer | os
    op         TEXT NOT NULL DEFAULT 'contains',   -- eq | contains | starts
    value      TEXT NOT NULL,   -- the Entra group id, Intune device id, or text
    label      TEXT,            -- what to show for an id-shaped value
    created_at TEXT
);

-- One row per sync job run. "Did the nightly sync happen" is not answerable
-- from the data alone: a sync that fetched nothing looks exactly like a sync
-- that never ran, and a cron entry nobody installed looks like both.
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    source      TEXT NOT NULL DEFAULT 'cron'   -- 'cron' or 'ui'
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_job ON sync_runs(job, id DESC);

-- People Entra returns that ITAM should not track: service accounts, shared
-- mailboxes, test identities. Same shape as the device rules, and for the same
-- reason - an OData filter cannot express "except these", and getting it wrong
-- means a sync that silently brings in nothing.
CREATE TABLE IF NOT EXISTS user_ignore_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    field      TEXT NOT NULL,   -- upn | display_name | department | job_title | country
    op         TEXT NOT NULL DEFAULT 'contains',
    value      TEXT NOT NULL,
    created_at TEXT
);

-- Every group Entra knows about, with what you have asked ITAM to do with it.
-- Discovery is one cheap call; syncing members is a call per group, so which
-- groups are worth that is a decision, not a guess. Ticking a box is that
-- decision, made once and visible.
CREATE TABLE IF NOT EXISTS entra_groups (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT,
    group_types     TEXT,        -- comma separated, as Entra reports them
    membership_rule TEXT,        -- present when membership is dynamic
    looks_like      TEXT,        -- 'device' | 'user' | 'assigned'
    sync_users      INTEGER NOT NULL DEFAULT 0,
    sync_devices    INTEGER NOT NULL DEFAULT 0,
    scope_devices   INTEGER NOT NULL DEFAULT 0,   -- limit the device sync to this
    discovered_at   TEXT
);

-- Entra groups whose members are DEVICES. The user-group sync casts members to
-- microsoft.graph.user, so a group full of virtual machines syncs as empty and
-- looks like nothing - these are kept apart so each list means one thing.
CREATE TABLE IF NOT EXISTS device_groups (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT,
    device_count    INTEGER NOT NULL DEFAULT 0,
    dynamic         INTEGER NOT NULL DEFAULT 0,   -- membership type Dynamic Device
    membership_rule TEXT,                         -- the rule, when it is dynamic
    synced_at       TEXT
);

-- Which Entra devices are in such a group. Refreshed whenever devices or device
-- groups are synced, so adding a VM to the group in Entra takes effect on the
-- next sync like everything else.
CREATE TABLE IF NOT EXISTS device_group_members (
    group_id        TEXT NOT NULL,
    azure_device_id TEXT NOT NULL,
    device_name     TEXT,
    PRIMARY KEY (group_id, azure_device_id)
);
CREATE INDEX IF NOT EXISTS idx_device_group_azure
    ON device_group_members(azure_device_id);

-- Intune custom attributes (macOS shell script results), merged onto a device.
CREATE TABLE IF NOT EXISTS device_attributes (
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    value        TEXT,
    collected_at TEXT,
    PRIMARY KEY (device_id, name)
);

-- Extra groups a rule includes, and groups it excludes. The rule's own
-- group_id is the primary include; these narrow or widen it.
CREATE TABLE IF NOT EXISTS rule_groups (
    rule_id  INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    mode     TEXT NOT NULL,             -- include | exclude
    PRIMARY KEY (rule_id, group_id, mode)
);

-- Who a rule has already been applied to. A rule fulfils a person once; it
-- does not maintain a level forever, so returning a monitor does not silently
-- earn another.
CREATE TABLE IF NOT EXISTS rule_fulfilments (
    rule_id      INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    upn          TEXT NOT NULL REFERENCES users(upn) ON DELETE CASCADE,
    granted      INTEGER NOT NULL DEFAULT 0,
    fulfilled_at TEXT,
    PRIMARY KEY (rule_id, upn)
);

-- Device groups defined by specification, each with a price. Lets a fleet be
-- priced by spec ("MacBook Air 13 M4/16/512 = 1299") instead of per machine.
CREATE TABLE IF NOT EXISTS price_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0,
    notes       TEXT,
    created_at  TEXT,
    currency    TEXT,
    rate_micro  INTEGER
);

-- All criteria of a group must match (AND), so a group narrows as you add to it.
CREATE TABLE IF NOT EXISTS price_group_criteria (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES price_groups(id) ON DELETE CASCADE,
    field     TEXT NOT NULL,   -- model | manufacturer | os | category | attribute
    attr_name TEXT,            -- which custom attribute, when field = attribute
    op        TEXT NOT NULL,   -- eq | contains | starts
    value     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_criteria_group
    ON price_group_criteria(group_id);

-- Who holds the device, as a condition on the price. A shekel price belongs to
-- the fleet bought in Israel, and the machine on a UK desk should keep its
-- pounds - so a group can be narrowed to, or held back from, the people in an
-- Entra group.
CREATE TABLE IF NOT EXISTS price_group_groups (
    group_id  INTEGER NOT NULL REFERENCES price_groups(id) ON DELETE CASCADE,
    entra_id  TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    mode      TEXT NOT NULL,   -- 'include' or 'exclude'
    PRIMARY KEY (group_id, entra_id, mode)
);

-- Entitlement rules: what members of a group should have.
CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    group_id        TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,       -- 'asset' or 'subscription'
    category        TEXT,                -- asset category, when kind='asset'
    asset_name      TEXT,                -- a specific item, or NULL for any in the category
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

-- Currencies money can be recorded in. rate_micro is USD per one unit of the
-- currency, times 1,000,000, so conversion is integer arithmetic throughout.
CREATE TABLE IF NOT EXISTS currencies (
    code        TEXT PRIMARY KEY,          -- ISO 4217
    symbol      TEXT,
    name        TEXT,
    rate_micro  INTEGER NOT NULL DEFAULT 1000000,
    rate_set_on TEXT,
    rate_source TEXT,                      -- base | manual | boi
    active      INTEGER NOT NULL DEFAULT 1
);

-- Every rate ever approved, so a converted total can be explained later.
CREATE TABLE IF NOT EXISTS rate_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    rate_micro  INTEGER NOT NULL,
    set_on      TEXT,
    source      TEXT,
    approved_by TEXT,
    recorded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rate_history_code ON rate_history(code, id DESC);

-- Counted assets: one row for many identical units. Mice, keyboards, headsets,
-- monitors and bulk-bought licences have no serial and are interchangeable, so
-- a row per unit would be noise. The row carries a unit price; handing one out
-- raises the count against it. Same categories as the serial-tracked assets:
-- these are assets, counted rather than listed.
--
-- There is deliberately no "how many did we buy" field. We are not a warehouse:
-- units come into existence by being handed to somebody. `spare` is what came
-- BACK - kit returned when someone left or swapped machines - waiting to go out
-- again without costing anything new.
CREATE TABLE IF NOT EXISTS pooled_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Peripheral',
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    spare           INTEGER NOT NULL DEFAULT 0,   -- returned, not yet re-issued
    vendor          TEXT,
    notes           TEXT,
    created_at      TEXT,
    currency        TEXT,
    rate_micro      INTEGER
);

-- One row per person per item; handing out a second unit raises the quantity
-- rather than adding a row.
CREATE TABLE IF NOT EXISTS pooled_allocations (
    item_id     INTEGER NOT NULL REFERENCES pooled_items(id) ON DELETE CASCADE,
    upn         TEXT NOT NULL REFERENCES users(upn) ON DELETE CASCADE,
    quantity    INTEGER NOT NULL DEFAULT 1,
    assigned_on TEXT,
    PRIMARY KEY (item_id, upn)
);
CREATE INDEX IF NOT EXISTS idx_pooled_alloc_upn ON pooled_allocations(upn);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    vendor                TEXT,
    monthly_cost_cents    INTEGER NOT NULL DEFAULT 0,  -- per seat, per month
    notes                 TEXT,
    sku_id                TEXT,       -- set when created from an Entra licence
    currency              TEXT,
    rate_micro            INTEGER
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


def _rename_legacy_tables(conn) -> None:
    """Stock became part of Assets, and the tables follow the vocabulary.

    This has to run before the schema script: CREATE TABLE IF NOT EXISTS would
    otherwise make an empty pooled_items and leave every real row stranded in
    stock_items. Renaming carries the foreign key in the allocations table with
    it, so long as legacy_alter_table is off - which it is by default.
    """
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for old, new in (("stock_items", "pooled_items"),
                     ("stock_allocations", "pooled_allocations")):
        if old in names and new not in names:
            conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    conn.execute("DROP INDEX IF EXISTS idx_stock_alloc_upn")


def init_db():
    with cursor() as conn:
        _rename_legacy_tables(conn)
        conn.executescript(SCHEMA)

        # Migration: databases created before the API existed have no
        # assets.external_id. Add it, then index it - in that order, which is
        # why the index is not part of SCHEMA above.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets)")]
        if "external_id" not in cols:
            conn.execute("ALTER TABLE assets ADD COLUMN external_id TEXT")

        # Migration: a rule can name a specific item, not just a category.
        rcols = [r["name"] for r in conn.execute("PRAGMA table_info(rules)")]
        if "asset_name" not in rcols:
            conn.execute("ALTER TABLE rules ADD COLUMN asset_name TEXT")

        # Migration: the Entra device object id, so a device can be matched
        # against the groups it is in.
        dcols = [r["name"] for r in conn.execute("PRAGMA table_info(devices)")]
        for col in ("azure_device_id", "ignored_reason"):
            if col not in dcols:
                conn.execute(f"ALTER TABLE devices ADD COLUMN {col} TEXT")
        gcols = [r["name"] for r in conn.execute("PRAGMA table_info(device_group_members)")]
        if "device_name" not in gcols:
            conn.execute("ALTER TABLE device_group_members ADD COLUMN device_name TEXT")
        egcols = [r["name"] for r in conn.execute("PRAGMA table_info(entra_groups)")]
        if "scope_devices" not in egcols:
            conn.execute("ALTER TABLE entra_groups ADD COLUMN scope_devices "
                         "INTEGER NOT NULL DEFAULT 0")

        dgcols = [r["name"] for r in conn.execute("PRAGMA table_info(device_groups)")]
        if "membership_rule" not in dgcols:
            conn.execute("ALTER TABLE device_groups ADD COLUMN membership_rule TEXT")
        if "dynamic" not in dgcols:
            conn.execute("ALTER TABLE device_groups ADD COLUMN dynamic "
                         "INTEGER NOT NULL DEFAULT 0")

        # Migration: counted assets no longer declare how many were bought.
        # What you owned but had not handed out is exactly what is on the shelf,
        # so that becomes `spare` and the typed quantity goes away.
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(pooled_items)")]
        if "quantity" in pcols and "spare" not in pcols:
            conn.execute("ALTER TABLE pooled_items ADD COLUMN spare INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """UPDATE pooled_items SET spare = MAX(0, quantity - COALESCE(
                       (SELECT SUM(quantity) FROM pooled_allocations a
                        WHERE a.item_id = pooled_items.id), 0))""")
            try:
                conn.execute("ALTER TABLE pooled_items DROP COLUMN quantity")
            except Exception:
                # Pre-3.35 SQLite cannot drop a column. Leaving it is harmless -
                # nothing reads it - but it must not keep rejecting inserts.
                pass

        # Migration: money-bearing rows gain the currency they were paid in and
        # the rate that applied then. Existing rows inherit the reporting
        # currency at a rate of 1, so no stored figure changes meaning.
        for table in ("assets", "pooled_items", "subscriptions", "price_groups"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "currency" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN currency TEXT")
            if "rate_micro" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN rate_micro INTEGER")

        # Migration: subscriptions can be linked to an Entra licence SKU.
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")]
        if "sku_id" not in scols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN sku_id TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_sku "
                     "ON subscriptions(sku_id)")

        # Migration: two-factor columns arrived after the first release.
        acols = [r["name"] for r in conn.execute("PRAGMA table_info(auth_users)")]
        for col, decl in (("totp_secret", "TEXT"),
                          ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
                          ("totp_last_step", "INTEGER"),
                          ("sso", "INTEGER NOT NULL DEFAULT 0")):
            if col not in acols:
                conn.execute(f"ALTER TABLE auth_users ADD COLUMN {col} {decl}")

        # Migration: country and usage location arrived after the first release.
        ucols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        for col in ("country", "usage_location", "ignored_reason"):
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
    """Built-in categories plus anything the API or a user has introduced.

    Pooled items are assets too, so a category that only exists in the pool
    still belongs in the list - otherwise it would have no page to live on.
    """
    seen = {r["category"] for r in q("SELECT DISTINCT category FROM assets") if r["category"]}
    seen |= {r["category"] for r in
             q("SELECT DISTINCT category FROM pooled_items") if r["category"]}
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

def conv(amount_col: str, rate_col: str) -> str:
    """SQL that converts a stored amount to the reporting currency.

    Uses the rate frozen on the row, with explicit half-up rounding. Integers
    throughout: SQLite would otherwise hand back floats that do not add up.
    """
    return f"(({amount_col} * COALESCE({rate_col}, 1000000) + 500000) / 1000000)"


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
