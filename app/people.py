"""Which of Entra's people ITAM should actually track.

The user filter is an OData expression sent to Graph, and it can only say
"these". Saying "these, except the service accounts and the four test
identities" is not something it does well, and a filter Graph will not run comes
back as an empty sync rather than an error.

So the exceptions live here instead, in the same shape as the device rules:
matched locally, recomputed from the rules every time, and never destructive.
An ignored person is still synced and still holds whatever they held - they are
kept out of the lists and the totals, and un-ignoring is instant.
"""
import datetime

from . import db

FIELDS = {
    "upn":          ("UPN", "COALESCE(u.upn,'')"),
    "display_name": ("Display name", "COALESCE(u.display_name,'')"),
    "department":   ("Department", "COALESCE(u.department,'')"),
    "job_title":    ("Job title", "COALESCE(u.job_title,'')"),
    "country":      ("Country", "COALESCE(u.country,'')"),
}

OPS = {
    "eq":       ("is exactly", "LOWER({expr}) = LOWER(?)"),
    "contains": ("contains", "LOWER({expr}) LIKE '%' || LOWER(?) || '%'"),
    "starts":   ("starts with", "LOWER({expr}) LIKE LOWER(?) || '%'"),
    "blank":    ("is blank", "TRIM({expr}) = ''"),
}


def rules():
    return db.q("SELECT * FROM user_ignore_rules ORDER BY field, id")


def add_rule(field: str, op: str, value: str) -> str | None:
    if field not in FIELDS:
        return "Unknown thing to match on"
    if op not in OPS:
        return "Unknown match"
    value = (value or "").strip()
    if op != "blank" and not value:
        return "Give the rule something to match"
    if op == "blank":
        value = ""
    if db.q1("SELECT 1 FROM user_ignore_rules WHERE field=? AND op=? AND value=?",
             (field, op, value)):
        return "That rule is already there"
    db.execute(
        """INSERT INTO user_ignore_rules (field, op, value, created_at)
           VALUES (?,?,?,?)""",
        (field, op, value,
         datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
    return None


def delete_rule(rule_id: int) -> None:
    db.execute("DELETE FROM user_ignore_rules WHERE id = ?", (rule_id,))


def describe(rule) -> str:
    field_label = FIELDS.get(rule["field"], (rule["field"], None))[0]
    op_label = OPS.get(rule["op"], (rule["op"], None))[0]
    if rule["op"] == "blank":
        return f"{field_label} {op_label}"
    return f"{field_label} {op_label} \u201c{rule['value']}\u201d"


def _clause(rule) -> tuple[str, list]:
    expr = FIELDS[rule["field"]][1]
    sql = OPS[rule["op"]][1].format(expr=expr)
    return (sql, [] if rule["op"] == "blank" else [rule["value"]])


def recompute() -> int:
    """Stamp each person with the rule hiding them, or clear it."""
    db.execute("UPDATE users SET ignored_reason = NULL")
    for rule in rules():
        sql, params = _clause(rule)
        db.execute(
            f"""UPDATE users SET ignored_reason = ?
                WHERE ignored_reason IS NULL
                  AND upn IN (SELECT u.upn FROM users u WHERE {sql})""",
            [describe(rule)] + params)
    return db.q1("SELECT COUNT(*) c FROM users WHERE ignored_reason IS NOT NULL")["c"]


def hiding() -> dict:
    """How many people each rule is hiding, keyed by its description.

    Counted here rather than in the template: the template only has the visible
    list, which by definition excludes everyone a rule is hiding, so it always
    came out as nought.
    """
    out = {}
    for row in db.q("""SELECT ignored_reason, COUNT(*) c FROM users
                       WHERE ignored_reason IS NOT NULL GROUP BY ignored_reason"""):
        out[row["ignored_reason"]] = row["c"]
    return out


def counts() -> dict:
    row = db.q1(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN ignored_reason IS NOT NULL THEN 1 ELSE 0 END) AS ignored
           FROM users""")
    return {"total": row["total"], "ignored": row["ignored"] or 0}


def listing(q: str = "", show_ignored: bool = False):
    sql = """SELECT u.*,
                    (SELECT COUNT(*) FROM assets a WHERE a.assigned_upn = u.upn) AS assets,
                    (SELECT COUNT(*) FROM subscription_seats s WHERE s.upn = u.upn) AS seats
             FROM users u"""
    where, params = [], []
    if not show_ignored:
        where.append("u.ignored_reason IS NULL")
    if q:
        where.append("(LOWER(u.upn) LIKE ? OR LOWER(COALESCE(u.display_name,'')) LIKE ?)")
        needle = f"%{q.lower()}%"
        params += [needle, needle]
    if where:
        sql += " WHERE " + " AND ".join(where)
    return db.q(sql + " ORDER BY u.display_name", params)


# --- when somebody's UPN changes -----------------------------------------
#
# UPN is the key everything hangs off, and it is not stable: rename somebody in
# Entra and the next sync sees a person who has never existed, while everything
# they hold stays attached to a name nobody uses any more. Entra's own stable
# identifier is the object id, which is already stored as users.entra_id, so a
# rename is detectable rather than guessed at.

# (table, upn column, the rest of its primary key). Everything that points at a
# person. A merge that misses one silently strands whatever it holds.
UPN_TABLES = [
    ("assets", "assigned_upn", None),
    ("group_members", "upn", "group_id"),
    ("group_members_unlinked", "upn", "group_id"),
    ("rule_fulfilments", "upn", "rule_id"),
    ("subscription_seats", "upn", "subscription_id"),
    ("user_licenses", "upn", "sku_id"),
]


def merge(from_upn: str, into_upn: str) -> dict | str:
    """Move everything from one person onto another, then delete the first.

    Returns a report, or a complaint if it cannot be done. Deliberately
    all-or-nothing per table and never destructive of holdings: where both
    people hold the same thing the counts are added, not dropped.
    """
    from_upn = (from_upn or "").strip().lower()
    into_upn = (into_upn or "").strip().lower()
    if not from_upn or not into_upn:
        return "Both people are needed"
    if from_upn == into_upn:
        return "That is the same person"
    if not db.q1("SELECT 1 FROM users WHERE upn = ?", (from_upn,)):
        return f"No such person: {from_upn}"
    if not db.q1("SELECT 1 FROM users WHERE upn = ?", (into_upn,)):
        return f"No such person: {into_upn}"

    moved = {}

    # Counted units: both may hold the same item, and the quantities have to
    # add up. Anything else would quietly lose kit.
    for row in db.q("SELECT item_id, quantity, assigned_on FROM pooled_allocations "
                    "WHERE upn = ?", (from_upn,)):
        db.execute(
            """INSERT INTO pooled_allocations (item_id, upn, quantity, assigned_on)
               VALUES (?,?,?,?)
               ON CONFLICT(item_id, upn) DO UPDATE SET
                   quantity = pooled_allocations.quantity + excluded.quantity""",
            (row["item_id"], into_upn, row["quantity"], row["assigned_on"]))
    units = db.q1("SELECT COALESCE(SUM(quantity),0) c FROM pooled_allocations "
                  "WHERE upn = ?", (from_upn,))["c"]
    db.execute("DELETE FROM pooled_allocations WHERE upn = ?", (from_upn,))
    if units:
        moved["counted units"] = units

    for table, column, other_key in UPN_TABLES:
        before = db.q1(f"SELECT COUNT(*) c FROM {table} WHERE {column} = ?",
                       (from_upn,))["c"]
        if not before:
            continue
        if other_key:
            # A composite key: the row may already exist for the other person,
            # in which case there is nothing to move, only to drop.
            db.execute(
                f"UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?",
                (into_upn, from_upn))
            db.execute(f"DELETE FROM {table} WHERE {column} = ?", (from_upn,))
        else:
            db.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                       (into_upn, from_upn))
        moved[table.replace("_", " ")] = before

    # Intune owns primary_upn and will correct it on the next device sync, but
    # leaving a stale name there means the holder check reports a disagreement
    # that is really just this rename.
    stale = db.q1("SELECT COUNT(*) c FROM devices WHERE primary_upn = ?",
                  (from_upn,))["c"]
    if stale:
        db.execute("UPDATE devices SET primary_upn = ? WHERE primary_upn = ?",
                   (into_upn, from_upn))
        moved["devices"] = stale

    db.execute("DELETE FROM users WHERE upn = ?", (from_upn,))
    return {"from": from_upn, "into": into_upn, "moved": moved}


def renamed() -> list[dict]:
    """People whose Entra object id already belongs to somebody else here.

    That is what a UPN change looks like from this side: two rows, one object.
    """
    return [dict(r) for r in db.q(
        """SELECT a.upn AS old_upn, a.display_name AS old_name,
                  b.upn AS new_upn, b.display_name AS new_name, a.entra_id
           FROM users a JOIN users b
             ON a.entra_id = b.entra_id AND a.upn < b.upn
           WHERE TRIM(COALESCE(a.entra_id,'')) != ''""")]
