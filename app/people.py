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
