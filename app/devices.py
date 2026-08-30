"""Local concerns of the Intune device list: what to hide, and who holds it.

Not everything Intune manages is kit somebody has. Virtual machines are the
obvious case - they live in an Entra group, nobody carries one, and every one of
them turning into an asset makes the estate look bigger than it is. Test rigs
and loan-pool spares are the same shape of problem.

Ignored devices are still synced. Hiding them by not fetching them would mean
never being able to answer "what is being hidden, and why", and un-ignoring
would need a round trip to Graph. So they come down, get marked, and the marking
is recomputed from the rules every time.
"""
import datetime

from . import db

# field -> (label, SQL expression over `devices d`). Whitelisted: the field name
# reaches SQL, the value never does except as a bound parameter.
FIELDS = {
    "group":        ("In Entra group", None),          # handled separately
    "device":       ("This one device", "d.id"),
    "device_name":  ("Device name", "COALESCE(d.device_name,'')"),
    "model":        ("Model", "COALESCE(d.model,'')"),
    "manufacturer": ("Manufacturer", "COALESCE(d.manufacturer,'')"),
    "os":           ("Operating system", "COALESCE(d.os,'')"),
}

OPS = {
    "eq":       ("is exactly", "LOWER({expr}) = LOWER(?)"),
    "contains": ("contains", "LOWER({expr}) LIKE '%' || LOWER(?) || '%'"),
    "starts":   ("starts with", "LOWER({expr}) LIKE LOWER(?) || '%'"),
}


def rules():
    return db.q("SELECT * FROM device_ignore_rules ORDER BY field, id")


def group_rules() -> list[str]:
    return [r["value"] for r in
            db.q("SELECT value FROM device_ignore_rules WHERE field = 'group'")]


def add_rule(field: str, op: str, value: str, label: str | None = None) -> str | None:
    if field not in FIELDS:
        return "Unknown thing to match on"
    if field in ("group", "device"):
        op = "eq"                    # an id either matches or it does not
    if op not in OPS:
        return "Unknown match"
    value = (value or "").strip()
    if not value:
        return "Give the rule something to match"
    if db.q1("SELECT 1 FROM device_ignore_rules WHERE field = ? AND op = ? AND value = ?",
             (field, op, value)):
        return "That rule is already there"
    db.execute(
        """INSERT INTO device_ignore_rules (field, op, value, label, created_at)
           VALUES (?,?,?,?,?)""",
        (field, op, value, (label or "").strip() or None,
         datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
    return None


def delete_rule(rule_id: int) -> None:
    db.execute("DELETE FROM device_ignore_rules WHERE id = ?", (rule_id,))


def describe(rule) -> str:
    if rule["field"] == "group":
        return f"In Entra group “{rule['label'] or rule['value']}”"
    if rule["field"] == "device":
        return f"The device “{rule['label'] or rule['value']}”"
    field_label = FIELDS.get(rule["field"], (rule["field"], None))[0]
    op_label = OPS.get(rule["op"], (rule["op"], None))[0]
    return f"{field_label} {op_label} “{rule['value']}”"


def _clauses(rows) -> tuple[list[str], list]:
    """One SQL clause per rule. A device matching ANY of them is ignored."""
    clauses, params = [], []
    for r in rows:
        if r["field"] == "group":
            clauses.append(
                "EXISTS (SELECT 1 FROM device_group_members gm "
                "WHERE gm.group_id = ? AND gm.azure_device_id = d.azure_device_id "
                "AND COALESCE(d.azure_device_id,'') != '')")
            params.append(r["value"])
        else:
            expr = FIELDS[r["field"]][1]
            clauses.append(OPS[r["op"]][1].format(expr=expr))
            params.append(r["value"])
    return clauses, params


def where_ignored(negate: bool = False) -> tuple[str, list]:
    """SQL for `devices d` being ignored, or not. No rules means nothing is.

    Returned as a fragment rather than a list of ids so a big estate does not
    have to be pulled into Python to filter a query.
    """
    clauses, params = _clauses(rules())
    if not clauses:
        return ("0=1" if not negate else "1=1"), []
    joined = "(" + " OR ".join(clauses) + ")"
    return (f"NOT {joined}" if negate else joined), params


def ignored_ids() -> set:
    where, params = where_ignored()
    return {r["id"] for r in
            db.q(f"SELECT d.id FROM devices d WHERE {where}", params)}


def recompute() -> int:
    """Stamp each device with the rule hiding it, or clear it. Returns the count.

    Stored rather than computed per query so the devices list can say *why* a
    device is hidden without re-running every rule for every row.
    """
    db.execute("UPDATE devices SET ignored_reason = NULL")
    hidden = 0
    for rule in rules():
        clauses, params = _clauses([rule])
        db.execute(
            f"""UPDATE devices SET ignored_reason = ?
                WHERE ignored_reason IS NULL
                  AND id IN (SELECT d.id FROM devices d WHERE {clauses[0]})""",
            [describe(rule)] + params)
    hidden = db.q1(
        "SELECT COUNT(*) c FROM devices WHERE ignored_reason IS NOT NULL")["c"]
    return hidden


def counts() -> dict:
    row = db.q1(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN ignored_reason IS NOT NULL THEN 1 ELSE 0 END) AS ignored,
                  SUM(CASE WHEN ignored_reason IS NOT NULL AND asset_id IS NOT NULL
                           THEN 1 ELSE 0 END) AS ignored_with_asset
           FROM devices""")
    return {"total": row["total"], "ignored": row["ignored"] or 0,
            "ignored_with_asset": row["ignored_with_asset"] or 0}


def ignored_listing():
    return db.q(
        """SELECT d.*, a.name AS asset_name FROM devices d
           LEFT JOIN assets a ON a.id = d.asset_id
           WHERE d.ignored_reason IS NOT NULL
           ORDER BY d.ignored_reason, d.device_name""")


def unlink_ignored() -> int:
    """Break the asset link on ignored devices, leaving both records alone.

    Ignoring a device says "this is not kit somebody has". An asset created
    from it before the rule existed is still a record you may want; deleting it
    is not this feature's call to make. So the link goes and the asset stays,
    for you to delete on the Assets page if that is what you meant.
    """
    rows = db.q("SELECT id FROM devices WHERE ignored_reason IS NOT NULL "
                "AND asset_id IS NOT NULL")
    for row in rows:
        db.execute("UPDATE devices SET asset_id = NULL WHERE id = ?", (row["id"],))
    return len(rows)


# --- who is holding it ---------------------------------------------------

def holder_gap() -> dict:
    """Where ITAM and Intune disagree about who is holding a machine.

    An asset takes its holder from the Intune device once, when the asset is
    created, and only if that person is already in ITAM. Sync devices before
    people - or take on somebody who joined afterwards - and the asset stays
    unassigned for good, with nothing on screen to say why. That is the usual
    reason for a pile of "unassigned" kit that is plainly on somebody's desk.

    Reports, without changing anything:
      fillable  - no holder in ITAM, and Intune names one we know
      unknown   - Intune names somebody ITAM has never synced
      nobody    - Intune has no primary user either (shared or never signed in)
      mismatch  - both name a holder, and they differ
    """
    rows = db.q(
        """SELECT d.id, d.device_name, d.primary_upn, a.id AS asset_id, a.name,
                  a.assigned_upn,
                  (SELECT display_name FROM users WHERE upn = d.primary_upn) AS intune_name,
                  (SELECT display_name FROM users WHERE upn = a.assigned_upn) AS itam_name
           FROM devices d JOIN assets a ON a.id = d.asset_id
           WHERE d.ignored_reason IS NULL
           ORDER BY d.device_name""")
    out = {"fillable": [], "unknown": [], "nobody": [], "mismatch": []}
    for r in rows:
        if not r["assigned_upn"]:
            if not r["primary_upn"]:
                out["nobody"].append(r)
            elif r["intune_name"] is None:
                out["unknown"].append(r)
            else:
                out["fillable"].append(r)
        elif r["primary_upn"] and r["primary_upn"] != r["assigned_upn"]:
            out["mismatch"].append(r)
    return out


def fill_holders_from_intune() -> int:
    """Give unassigned assets the holder Intune already knows about.

    Only assets with no holder at all are touched. Where the two disagree the
    difference is reported and left alone: somebody assigned that one by hand,
    and a sync has no business overruling them.
    """
    gap = holder_gap()
    today = datetime.date.today().isoformat()
    for row in gap["fillable"]:
        db.execute(
            "UPDATE assets SET assigned_upn = ?, assigned_on = ? WHERE id = ?",
            (row["primary_upn"], today, row["asset_id"]))
    return len(gap["fillable"])


# --- Entra device groups -------------------------------------------------

def groups_listing():
    """Device groups, with how many of their members ITAM actually knows."""
    return db.q(
        """SELECT dg.*,
                  (SELECT COUNT(*) FROM device_group_members m
                    WHERE m.group_id = dg.id) AS members,
                  (SELECT COUNT(*) FROM device_group_members m
                     JOIN devices d ON d.azure_device_id = m.azure_device_id
                    WHERE m.group_id = dg.id) AS matched,
                  (SELECT COUNT(*) FROM device_ignore_rules r
                    WHERE r.field = 'group' AND r.value = dg.id) AS ignoring
           FROM device_groups dg ORDER BY dg.display_name""")


def group(group_id: str):
    return db.q1("SELECT * FROM device_groups WHERE id = ?", (group_id,))


def group_members(group_id: str):
    """The group's devices, paired with the Intune record where there is one.

    A member with no Intune record is worth showing rather than dropping: it
    usually means the device sync has not run, or a filter is keeping it out.
    """
    return db.q(
        """SELECT m.azure_device_id, m.device_name AS entra_name,
                  d.id AS device_id, d.device_name, d.model, d.os,
                  d.serial_number, d.ignored_reason, d.asset_id,
                  a.name AS asset_name
           FROM device_group_members m
           LEFT JOIN devices d ON d.azure_device_id = m.azure_device_id
           LEFT JOIN assets a ON a.id = d.asset_id
           WHERE m.group_id = ?
           ORDER BY COALESCE(d.device_name, m.device_name)""", (group_id,))
