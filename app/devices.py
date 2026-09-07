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


def filter_groups(rows, q: str = "", state: str = ""):
    """Narrow the group catalogue.

    The search runs over the name, the description AND the membership rule, so
    "device." finds every dynamic device group without anybody knowing what its
    groups happen to be called. A * is a wildcard; without one the search is a
    plain substring, because that is what people expect from a search box.
    """
    import fnmatch

    out = []
    needle = (q or "").strip().lower()
    for r in rows:
        haystack = " ".join(str(r[k] or "") for k in
                            ("display_name", "description", "membership_rule")).lower()
        if needle:
            hit = (fnmatch.fnmatch(haystack, f"*{needle}*") if "*" in needle
                   else needle in haystack)
            if not hit:
                continue
        out.append(r)

    if state == "ticked":
        out = [r for r in out if r["ticked"]]
    elif state == "unticked":
        out = [r for r in out if not r["ticked"]]
    elif state == "members":
        out = [r for r in out if r["synced"]]
    elif state == "empty":
        out = [r for r in out if not r["synced"]]
    elif state in ("device", "user", "assigned"):
        out = [r for r in out if r["looks_like"] == state]
    return out


GROUP_STATES = [
    ("", "Any"),
    ("ticked", "Ticked"),
    ("unticked", "Not ticked"),
    ("members", "Has members here"),
    ("empty", "No members here"),
    ("device", "Dynamic \u00b7 devices"),
    ("user", "Dynamic \u00b7 users"),
    ("assigned", "Assigned membership"),
]


# --- what a device reports about itself ----------------------------------
#
# Whatever Intune has collected for a device is shown against its asset, under
# the names it was collected under. On a Mac that is the custom attributes you
# wrote the scripts for; on Windows it is whatever Device inventory yielded.
#
# An earlier version tried to sort them into CPU/RAM/disk slots by matching the
# attribute NAME. That silently dropped every attribute whose name did not
# happen to contain "cpu" or "memory" - which is most of them, because you named
# them, not Microsoft. Guessing at somebody else's naming is the same mistake as
# guessing at their strings.

import re as _re


def _disk_gb(raw) -> int | None:
    """Decimal GB, which is the number on a disk: 512110190592 -> 512."""
    try:
        total = int(raw or 0)
    except (TypeError, ValueError):
        return None
    return int(round(total / 1_000_000_000)) if total else None


def _ram_gb(raw) -> int | None:
    """Binary GB, which is the number on a memory module.

    RAM is sold and fitted in powers of two, so 34359738368 bytes is a 32GB
    machine. Dividing by a billion gives 34, which is not a size anybody has
    ever bought, and it makes the app look like it cannot count.
    """
    try:
        total = int(raw or 0)
    except (TypeError, ValueError):
        return None
    return int(round(total / (1024 ** 3))) if total else None


def spec_of(attrs, cpu_model=None, memory_total=None,
            storage_total=None) -> list[tuple[str, str]]:
    """What to show about one machine, as (label, value) pairs.

    Two sources, and they are never mixed. A custom attribute is a script on
    the machine reporting its own spec: on a Mac, one tag already reads
    MBA-13.6"-M5/24/512G-10CPU-10GPU, so it is shown exactly as collected and
    nothing is appended - a second opinion on the disk size beside it is noise,
    and when the two disagree it reads as a bug.

    Only when no script reported anything does this fall back to the three
    fields Graph carries: the processor name from Endpoint Analytics, and the
    memory and storage from managedDevices. That is the Windows case, where
    there is no custom attribute mechanism.
    """
    if attrs:
        return list(attrs)
    out = []
    cpu = (cpu_model or "").strip()
    if cpu:
        out.append(("CPU", cpu))
    disk = _disk_gb(storage_total)
    if disk:
        out.append(("SSD", f"{disk}GB"))
    ram = _ram_gb(memory_total)
    if ram:
        out.append(("RAM", f"{ram}GB"))
    return out


def _collect(rows, key: str) -> dict:
    """Group attribute-joined device rows by `key`, then reduce to a spec."""
    seen: dict = {}
    for row in rows:
        entry = seen.setdefault(row[key], {
            "attrs": [], "cpu_model": row["cpu_model"],
            "memory_total": row["memory_total"],
            "storage_total": row["storage_total"]})
        if row["name"]:
            entry["attrs"].append((row["name"], row["value"]))
    return {k: spec_of(v["attrs"], v["cpu_model"], v["memory_total"],
                       v["storage_total"]) for k, v in seen.items()}


SPEC_SELECT = """SELECT d.id AS device_id, d.asset_id, d.storage_total,
                        d.memory_total, d.cpu_model, da.name, da.value
                 FROM devices d
                 LEFT JOIN device_attributes da ON da.device_id = d.id"""


def specs_for(upn: str) -> dict:
    """asset id -> [(label, value)...] for the machine behind that asset."""
    return _collect(db.q(
        SPEC_SELECT + " WHERE d.asset_id IN (SELECT id FROM assets "
                      "WHERE assigned_upn = ?)", (upn,)), "asset_id")


def all_specs() -> dict:
    """device id -> [(label, value)...], for the devices list."""
    return _collect(db.q(SPEC_SELECT), "device_id")


# --- the Assets overview --------------------------------------------------

def _os_family(raw: str | None) -> str | None:
    """Group Intune's operatingSystem into the two families anyone asks about.

    Intune reports "Windows", "macOS", "iOS", "Android" and occasionally
    nothing. Only the first two are machines somebody is issued here, and a
    widget per value was a wall of cards that grew whenever somebody enrolled
    a phone.
    """
    text = (raw or "").strip().lower()
    if text.startswith("windows"):
        return "Windows"
    if text in ("macos", "mac os", "macos x", "osx", "mac") or text.startswith("mac"):
        return "macOS"
    return None


def overview() -> dict:
    """Counts for the Assets landing page.

    Serial-tracked kit is counted per machine; counted kit is counted in units
    handed to people, because "3 monitors" means three monitors and not three
    rows or three people.
    """
    from . import pooled

    def serial_count(category: str) -> dict:
        row = db.q1(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN TRIM(COALESCE(assigned_upn,'')) != ''
                               THEN 1 ELSE 0 END) AS assigned
               FROM assets WHERE category = ?""", (category,))
        return {"total": row["total"] or 0, "assigned": row["assigned"] or 0}

    families = {"Windows": 0, "macOS": 0}
    for row in db.q("""SELECT d.os, COUNT(*) AS n FROM devices d
                       JOIN assets a ON a.id = d.asset_id
                       WHERE d.ignored_reason IS NULL GROUP BY d.os"""):
        family = _os_family(row["os"])
        if family:
            families[family] += row["n"]

    def units(category: str) -> dict:
        """Units of counted kit in a category, per item name and in total."""
        rows = db.q(
            "SELECT s.name, " + pooled.assigned_expr() + " AS assigned, s.spare "
            "FROM pooled_items s WHERE s.category = ? ORDER BY s.name", (category,))
        models = [{"name": r["name"], "assigned": r["assigned"]}
                  for r in rows if r["assigned"]]
        serial = serial_count(category)
        return {
            "handed_out": sum(r["assigned"] for r in rows) + serial["assigned"],
            "total": sum(r["assigned"] + r["spare"] for r in rows) + serial["total"],
            "models": models,
            "serial": serial["total"],
        }

    return {"laptops": serial_count("Laptop"), "families": families,
            "monitors": units("Monitor"), "peripherals": units("Peripheral")}
