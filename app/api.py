"""Machine-to-machine API, for webhooks from systems like Frappe.

Authentication is a bearer token, not the session cookie the browser uses:

    Authorization: Bearer itam_xxxxxxxx...

Tokens are high-entropy random strings, so a plain SHA-256 of the token is
enough to store - unlike a user password, there is nothing to brute force.
"""
import datetime
import hashlib
import hmac
import json
import secrets

from . import db, settings

TOKEN_PREFIX = "itam_"
LOG_KEEP = 200


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- keys ----------------------------------------------------------------

def create_key(name: str, can_create_assets: bool = True, can_assign: bool = True) -> str:
    """Returns the plaintext token. It is not recoverable afterwards."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    db.execute(
        """INSERT INTO api_keys (name, token_hash, prefix, can_create_assets, can_assign, created_at)
           VALUES (?,?,?,?,?,?)""",
        (name.strip(), hash_token(token), token[:len(TOKEN_PREFIX) + 6],
         1 if can_create_assets else 0, 1 if can_assign else 0, _now()))
    return token


def list_keys():
    return db.q("SELECT * FROM api_keys ORDER BY created_at DESC")


def delete_key(key_id: int) -> None:
    db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))


def set_key_active(key_id: int, active: bool) -> None:
    db.execute("UPDATE api_keys SET active = ? WHERE id = ?", (1 if active else 0, key_id))


def authenticate(header: str | None):
    """Resolve an Authorization / X-API-Key header to an active key row."""
    if not header:
        return None
    token = header.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return None
    digest = hash_token(token)
    for row in db.q("SELECT * FROM api_keys WHERE active = 1"):
        if hmac.compare_digest(digest, row["token_hash"]):
            db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (_now(), row["id"]))
            return row
    return None


# --- request log ---------------------------------------------------------

def log(key_name, endpoint, status, message, payload=None) -> None:
    db.execute(
        "INSERT INTO api_log (at, key_name, endpoint, status, message, payload) VALUES (?,?,?,?,?,?)",
        (_now(), key_name, endpoint, status, message,
         json.dumps(payload)[:2000] if payload is not None else None))
    db.execute(
        "DELETE FROM api_log WHERE id NOT IN (SELECT id FROM api_log ORDER BY id DESC LIMIT ?)",
        (LOG_KEEP,))


def recent_log(limit: int = 25):
    return db.q("SELECT * FROM api_log ORDER BY id DESC LIMIT ?", (limit,))


# --- field mapping -------------------------------------------------------

def apply_map(payload: dict) -> tuple[dict, list]:
    """Translate an incoming payload into ITAM asset fields.

    Returns (mapped, ignored_source_fields). Unmapped keys are reported rather
    than silently dropped, so a wrong webhook config is visible in the log.
    """
    mapping = db.field_map()
    mapped, ignored = {}, []
    for key, value in payload.items():
        target = mapping.get(key)
        if target and target in db.ASSET_FIELDS:
            if value is not None and str(value).strip() != "":
                mapped[target] = value
        else:
            ignored.append(key)
    return mapped, ignored


def set_mapping(source: str, target: str) -> None:
    db.execute(
        """INSERT INTO api_field_map (source_field, target_field) VALUES (?,?)
           ON CONFLICT(source_field) DO UPDATE SET target_field = excluded.target_field""",
        (source.strip(), target.strip()))


def delete_mapping(source: str) -> None:
    db.execute("DELETE FROM api_field_map WHERE source_field = ?", (source,))


# --- asset creation ------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def create_asset(key, payload: dict) -> dict:
    """Create (or return an already-created) asset from a webhook payload."""
    if not key["can_create_assets"]:
        raise ApiError(403, "This API key is not allowed to create assets.")

    mapped, ignored = apply_map(payload)

    name = str(mapped.get("name", "")).strip()
    if not name:
        raise ApiError(400, "No asset name in the payload. Map an incoming field "
                            "to 'name' under Admin > API.")

    external_id = str(mapped.get("external_id", "")).strip() or None
    if external_id:
        existing = db.q1("SELECT * FROM assets WHERE external_id = ?", (external_id,))
        if existing:
            # A retried webhook must not create a second asset.
            return {"status": "already_exists", "asset_id": existing["id"],
                    "name": existing["name"], "ignored_fields": ignored}

    upn = str(mapped.get("assigned_upn", "")).strip().lower() or None
    if upn:
        if not key["can_assign"]:
            raise ApiError(403, "This API key is not allowed to assign assets to people.")
        if not db.q1("SELECT 1 FROM users WHERE upn = ?", (upn,)):
            raise ApiError(422, f"No user with UPN '{upn}'. Run an Entra ID sync first, "
                                f"or omit the assignment.")

    category = str(mapped.get("category", "")).strip() or "Other"
    cost_cents = db.to_cents(mapped.get("cost", 0))
    today = datetime.date.today().isoformat()

    asset_id = db.execute(
        """INSERT INTO assets (name, category, cost_cents, serial, purchased_on, notes,
                               assigned_upn, assigned_on, external_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (name, category, cost_cents,
         str(mapped.get("serial", "")).strip() or None,
         str(mapped.get("purchased_on", "")).strip() or None,
         str(mapped.get("notes", "")).strip() or None,
         upn, today if upn else None, external_id))

    return {"status": "created", "asset_id": asset_id, "name": name,
            "category": category, "assigned_upn": upn,
            "cost": db.money(cost_cents), "currency": settings.currency(),
            "ignored_fields": ignored}
