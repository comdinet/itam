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


# --- reading and updating ------------------------------------------------

# What an update may set. Deliberately not every column: id, external_id and
# the frozen rate are identity and history, and a webhook that can rewrite
# those can rewrite what a purchase cost after the fact.
UPDATABLE = ["name", "category", "cost", "serial", "purchased_on", "notes",
             "assigned_upn", "status"]


def _asset_json(row) -> dict:
    return {"asset_id": row["id"], "name": row["name"], "category": row["category"],
            "serial": row["serial"], "assigned_upn": row["assigned_upn"],
            "assigned_on": row["assigned_on"], "status": row["status"],
            "cost": db.money(row["cost_cents"]),
            "currency": row["currency"] or settings.currency(),
            "purchased_on": row["purchased_on"], "notes": row["notes"]}


def select_assets(where: dict) -> list:
    """The assets a request is talking about.

    Two ways to say which, because those are the two things anybody knows
    offhand: the serial printed on the machine, or the person holding it. A
    selector that matches nothing is not an error - it is an answer - but it
    is reported, so a typo in a serial does not read as a successful no-op.
    """
    serials = where.get("serial")
    upns = where.get("assigned_upn") or where.get("upn")
    if serials is None and upns is None:
        raise ApiError(400, 'Say which assets: "serial" or "assigned_upn", '
                            "either one value or a list of them.")
    if serials is not None and upns is not None:
        raise ApiError(400, 'Give "serial" or "assigned_upn", not both - two '
                            "selectors cannot both be the one that matched.")
    if serials is not None:
        values = [str(v).strip() for v in _as_list(serials) if str(v).strip()]
        if not values:
            raise ApiError(400, "No serial given.")
        # Case-insensitive: Intune, a label printer and a human all disagree.
        placeholders = ",".join("?" * len(values))
        return db.q(f"""SELECT * FROM assets
                        WHERE UPPER(TRIM(COALESCE(serial,''))) IN ({placeholders})
                        ORDER BY id""", [v.upper() for v in values])
    values = [str(v).strip().lower() for v in _as_list(upns) if str(v).strip()]
    if not values:
        raise ApiError(400, "No UPN given.")
    placeholders = ",".join("?" * len(values))
    return db.q(f"""SELECT * FROM assets
                    WHERE LOWER(TRIM(COALESCE(assigned_upn,''))) IN ({placeholders})
                    ORDER BY id""", values)


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _validated(key, changes: dict) -> dict:
    """Check every field before writing any of them."""
    unknown = [k for k in changes if k not in UPDATABLE]
    if unknown:
        raise ApiError(400, "Cannot set " + ", ".join(sorted(unknown))
                       + ". Settable fields: " + ", ".join(UPDATABLE) + ".")
    if not changes:
        raise ApiError(400, 'Nothing to set. Give a "set" object with at least '
                            "one field.")
    out = dict(changes)
    if "status" in out:
        status = str(out["status"] or "").strip()
        if status and status not in db.ASSET_STATUSES:
            raise ApiError(422, f"'{status}' is not a status. Known statuses: "
                           + ", ".join(db.ASSET_STATUSES) + ", or \"\" for none.")
        out["status"] = status or None
    if "category" in out:
        category = str(out["category"] or "").strip()
        if category not in db.categories():
            raise ApiError(422, f"'{category}' is not a category. Known: "
                           + ", ".join(db.categories()) + ".")
        out["category"] = category
    if "assigned_upn" in out:
        if not key["can_assign"]:
            raise ApiError(403, "This API key is not allowed to assign assets.")
        upn = str(out["assigned_upn"] or "").strip().lower()
        if upn and not db.q1("SELECT 1 FROM users WHERE upn = ?", (upn,)):
            raise ApiError(422, f"No user with UPN '{upn}'.")
        out["assigned_upn"] = upn or None
    if "cost" in out:
        out["cost_cents"] = db.to_cents(out.pop("cost"))
    for field in ("name", "serial", "notes", "purchased_on"):
        if field in out:
            out[field] = str(out[field] or "").strip() or None
    if out.get("name") is None and "name" in out:
        raise ApiError(422, "An asset cannot have an empty name.")
    return out


def update_assets(key, payload: dict) -> dict:
    """Change fields on every asset a selector matches.

    dry_run is the default answer to "how do I know what this will hit" - it
    reports the same thing a real run would, having written nothing.
    """
    if not key["can_create_assets"]:
        raise ApiError(403, "This API key is not allowed to change assets.")
    changes = payload.get("set")
    if not isinstance(changes, dict):
        raise ApiError(400, 'Give a "set" object: {"set": {"status": "Sold to employee"}}.')
    where = payload.get("where")
    if not isinstance(where, dict):
        where = {k: payload[k] for k in ("serial", "assigned_upn", "upn")
                 if k in payload}
    fields = _validated(key, changes)
    rows = select_assets(where)
    dry_run = bool(payload.get("dry_run"))

    assigning = "assigned_upn" in fields
    today = datetime.date.today().isoformat()
    updated = []
    for row in rows:
        if not dry_run:
            sets = ", ".join(f"{k} = ?" for k in fields)
            params = list(fields.values())
            if assigning and (row["assigned_upn"] or "") != (fields["assigned_upn"] or ""):
                sets += ", assigned_on = ?"
                params.append(today if fields["assigned_upn"] else None)
            params.append(row["id"])
            db.execute(f"UPDATE assets SET {sets} WHERE id = ?", params)
        after = db.q1("SELECT * FROM assets WHERE id = ?", (row["id"],))
        updated.append(_asset_json(after))
    return {"status": "would_update" if dry_run else "updated",
            "matched": len(rows), "set": {k: fields[k] for k in sorted(fields)},
            "assets": updated,
            "note": None if rows else "Nothing matched that selector."}


# --- mass assignment -----------------------------------------------------

def resolve_people(to: dict) -> tuple[list, list]:
    """Who a bulk assignment is for: a group by name, or UPNs written out.

    Returns (upns, problems). A UPN nobody has is reported rather than skipped
    quietly, because a bulk call is exactly where a typo disappears.
    """
    if not isinstance(to, dict):
        raise ApiError(400, 'Give "to" as {"group": "Israel Staff"} or '
                            '{"upns": ["someone@example.com"]}.')
    group, upns = to.get("group"), to.get("upns") or to.get("upn")
    if bool(group) == bool(upns):
        raise ApiError(400, 'Give "to" exactly one of "group" or "upns".')

    if group:
        name = str(group).strip()
        rows = db.q("SELECT id, display_name FROM groups WHERE LOWER(display_name) = ?",
                    (name.lower(),))
        if not rows:
            known = [r["display_name"] for r in
                     db.q("SELECT display_name FROM groups ORDER BY display_name LIMIT 25")]
            raise ApiError(422, f"No group called '{name}'."
                           + (" Groups ITAM knows: " + ", ".join(known) if known else
                              " No groups have been synced yet."))
        if len(rows) > 1:
            raise ApiError(422, f"More than one group is called '{name}'. "
                                "Rename one, or give the people by UPN.")
        members = db.q(
            """SELECT u.upn FROM group_members m JOIN users u ON u.upn = m.upn
               WHERE m.group_id = ? AND u.ignored_reason IS NULL
               ORDER BY u.upn""", (rows[0]["id"],))
        if not members:
            raise ApiError(422, f"'{rows[0]['display_name']}' has no members in ITAM. "
                                "Sync groups, or check the group is not empty.")
        return [r["upn"] for r in members], []

    wanted = [str(v).strip().lower() for v in _as_list(upns) if str(v).strip()]
    found, problems = [], []
    for upn in wanted:
        if db.q1("SELECT 1 FROM users WHERE upn = ?", (upn,)):
            found.append(upn)
        else:
            problems.append({"upn": upn, "why": "nobody in ITAM has that UPN"})
    if not found:
        raise ApiError(422, "None of those UPNs is a person in ITAM.")
    return found, problems


def assign_to_people(key, payload: dict) -> dict:
    """Hand out a counted item to everybody in a group, or to a named list.

    Serial-tracked kit is not here on purpose: one machine goes to one person,
    and "assign this laptop to forty people" has no meaning. Change who holds
    a specific machine with a PATCH on its serial instead.
    """
    if not key["can_assign"]:
        raise ApiError(403, "This API key is not allowed to assign.")
    from . import pooled

    name = str(payload.get("item") or payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, 'Which item? Give "item": "Dell U2725QE".')
    category = str(payload.get("category") or "").strip()
    sql = "SELECT * FROM pooled_items WHERE LOWER(TRIM(name)) = ?"
    params = [name.lower()]
    if category:
        sql += " AND category = ?"
        params.append(category)
    matches = db.q(sql + " ORDER BY id", params)
    if not matches:
        raise ApiError(422, f"No counted item called '{name}'"
                       + (f" in {category}" if category else "")
                       + ". Serial-tracked kit is assigned one machine at a "
                         "time - PATCH /api/v1/assets with its serial.")
    if len(matches) > 1:
        raise ApiError(422, f"'{name}' exists in more than one category: "
                       + ", ".join(sorted({m["category"] for m in matches}))
                       + '. Add "category" to say which.')
    item = matches[0]

    try:
        quantity = int(payload.get("quantity", 1))
    except (TypeError, ValueError):
        raise ApiError(400, "quantity must be a whole number.")
    if quantity < 1:
        raise ApiError(400, "quantity must be at least 1.")

    upns, problems = resolve_people(payload.get("to") or {})
    dry_run = bool(payload.get("dry_run"))

    assigned = []
    for upn in upns:
        held = pooled.held_by(upn, item["category"], item["name"])
        if not dry_run:
            complaint = pooled.assign(item["id"], upn, quantity)
            if complaint:
                problems.append({"upn": upn, "why": complaint})
                continue
        assigned.append({"upn": upn, "quantity": quantity, "held_before": held})

    return {"status": "would_assign" if dry_run else "assigned",
            "item": item["name"], "category": item["category"],
            "quantity_each": quantity, "people": len(assigned),
            "units": len(assigned) * quantity,
            "assigned": assigned, "problems": problems}
