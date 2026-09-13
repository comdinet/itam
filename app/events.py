"""What has happened to an asset, and who did it.

Written by comparing the whole row before a change against the whole row
after. A hand-written list of "fields worth logging" is a list somebody
forgets to update the next time a column is added, and the first you hear of
it is when the history is missing the one change you needed.

Nothing here refuses or validates. It records. A history that can reject a
write is a history that changes what happened.
"""
import datetime
import uuid

from . import db

# Columns worth a line in a timeline. id and rate_micro are plumbing, and
# external_id is the webhook's own key rather than anything about the kit.
TRACKED = {
    "name": "Name",
    "category": "Category",
    "cost_cents": "Cost",
    "currency": "Currency",
    "serial": "Serial",
    "purchased_on": "Purchased",
    "notes": "Notes",
    "assigned_upn": "Assigned to",
    "status": "Status",
}

SOURCES = ("ui", "api", "sync", "import", "rule")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _shown(field: str, value) -> str | None:
    """The value as a person reads it, not as SQLite stores it."""
    if value is None or value == "":
        return None
    if field == "cost_cents":
        return db.money(value)
    return str(value)


def _write(batch, asset_id, asset_name, action, source, actor,
           field=None, old=None, new=None) -> None:
    db.execute(
        """INSERT INTO asset_events (asset_id, asset_name, at, actor, source,
                                     action, batch, field, old_value, new_value)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (asset_id, asset_name, _now(), actor, source, action, batch,
         field, old, new))


def created(asset_id: int, source: str, actor: str | None = None) -> None:
    row = db.q1("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not row:
        return
    batch = uuid.uuid4().hex
    _write(batch, asset_id, row["name"], "created", source, actor)
    # The state it arrived in, so the timeline starts somewhere rather than
    # with a bare "created" and no idea what was created.
    for field in TRACKED:
        value = _shown(field, row[field])
        if value is not None and field != "name":
            _write(batch, asset_id, row["name"], "created", source, actor,
                   field, None, value)


def changed(asset_id: int, before, source: str, actor: str | None = None) -> int:
    """Record every tracked difference between `before` and the row now."""
    after = db.q1("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not after or before is None:
        return 0
    batch, written = uuid.uuid4().hex, 0
    for field in TRACKED:
        old, new = _shown(field, before[field]), _shown(field, after[field])
        if old != new:
            _write(batch, asset_id, after["name"], "changed", source, actor,
                   field, old, new)
            written += 1
    return written


def deleted(asset_id: int, source: str, actor: str | None = None) -> None:
    row = db.q1("SELECT name FROM assets WHERE id = ?", (asset_id,))
    if row:
        _write(uuid.uuid4().hex, asset_id, row["name"], "deleted", source, actor)


def for_asset(asset_id: int, limit: int = 200) -> list[dict]:
    """The timeline, newest first, with same-moment changes grouped.

    Editing four fields in one form submission is one thing that happened, not
    four, so every row written by one call carries the same batch. Grouping on
    the timestamp instead would fold a creation and an edit together whenever
    they landed in the same second - which is exactly what a test does, and
    occasionally what a person does.
    """
    rows = db.q(
        """SELECT * FROM asset_events WHERE asset_id = ?
           ORDER BY id DESC LIMIT ?""", (asset_id, limit))
    return group(rows)


def group(rows) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        key = row["batch"]
        if out and out[-1]["key"] == key:
            entry = out[-1]
        else:
            entry = {"key": key, "at": row["at"], "actor": row["actor"],
                     "source": row["source"], "action": row["action"],
                     "asset_id": row["asset_id"], "asset_name": row["asset_name"],
                     "changes": []}
            out.append(entry)
        if row["field"]:
            entry["changes"].append({
                "field": row["field"], "label": TRACKED.get(row["field"], row["field"]),
                "old": row["old_value"], "new": row["new_value"]})
    return out


def recent(limit: int = 100, upn: str = "") -> list[dict]:
    """Everything, newest first - the whole estate's activity in one place."""
    if upn:
        rows = db.q(
            """SELECT * FROM asset_events
               WHERE old_value = ? OR new_value = ?
               ORDER BY id DESC LIMIT ?""", (upn, upn, limit))
    else:
        rows = db.q("SELECT * FROM asset_events ORDER BY id DESC LIMIT ?", (limit,))
    return group(rows)
