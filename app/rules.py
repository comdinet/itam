"""Entitlement rules: what members of an Entra group should have.

A rule states the intended state ("everyone in Design gets 2 monitors"), and
compliance is computed against it. Applying a rule assigns what is actually
available - it never invents hardware that does not exist, because inventing
assets would mean ITAM reporting kit nobody owns.
"""
import datetime

from . import db


def create(name: str, group_id: str, kind: str, quantity: int,
           category: str | None = None, subscription_id: int | None = None) -> int:
    # A person either holds a licence seat or does not, so quantity only means
    # something for assets. Clamping here keeps such a rule from reading as
    # permanently short.
    if kind == "subscription":
        quantity = 1
    return db.execute(
        """INSERT INTO rules (name, group_id, kind, category, subscription_id,
                              quantity, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (name.strip(), group_id, kind, category, subscription_id, max(1, quantity),
         datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))


def listing():
    return db.q(
        """SELECT r.*, g.display_name AS group_name, g.member_count,
                  s.name AS subscription_name, s.monthly_cost_cents
           FROM rules r
           JOIN groups g ON g.id = r.group_id
           LEFT JOIN subscriptions s ON s.id = r.subscription_id
           ORDER BY r.active DESC, g.display_name, r.name""")


def get(rule_id: int):
    return db.q1(
        """SELECT r.*, g.display_name AS group_name,
                  s.name AS subscription_name
           FROM rules r
           JOIN groups g ON g.id = r.group_id
           LEFT JOIN subscriptions s ON s.id = r.subscription_id
           WHERE r.id = ?""", (rule_id,))


def delete(rule_id: int) -> None:
    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def set_active(rule_id: int, active: bool) -> None:
    db.execute("UPDATE rules SET active = ? WHERE id = ?", (1 if active else 0, rule_id))


def evaluate(rule) -> list[dict]:
    """Per-member holdings versus the rule. Positive gap means short."""
    members = db.q(
        """SELECT u.upn, u.display_name, u.account_enabled
           FROM group_members gm JOIN users u ON u.upn = gm.upn
           WHERE gm.group_id = ? ORDER BY u.display_name""", (rule["group_id"],))
    out = []
    for m in members:
        if rule["kind"] == "asset":
            have = db.q1(
                "SELECT COUNT(*) c FROM assets WHERE assigned_upn = ? AND category = ?",
                (m["upn"], rule["category"]))["c"]
        else:
            have = db.q1(
                "SELECT COUNT(*) c FROM subscription_seats WHERE upn = ? AND subscription_id = ?",
                (m["upn"], rule["subscription_id"]))["c"]
        want = 1 if rule["kind"] == "subscription" else rule["quantity"]
        out.append({"upn": m["upn"], "display_name": m["display_name"],
                    "account_enabled": m["account_enabled"],
                    "have": have, "want": want, "gap": want - have})
    return out


def summarise(rule) -> dict:
    rows = evaluate(rule)
    short = [r for r in rows if r["gap"] > 0]
    over = [r for r in rows if r["gap"] < 0]
    needed = sum(r["gap"] for r in short)
    available = 0
    if rule["kind"] == "asset":
        available = db.q1(
            "SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NULL AND category = ?",
            (rule["category"],))["c"]
    return {"members": len(rows), "compliant": len(rows) - len(short) - len(over),
            "short": len(short), "over": len(over), "needed": needed,
            "available": available, "rows": rows}


def apply(rule) -> dict:
    """Close the gaps this rule can close, and report what it could not.

    Subscription seats are just records, so they are granted outright. Assets
    are only ever taken from existing spares.
    """
    today = datetime.date.today().isoformat()
    granted_total = 0
    shortfall = []

    # Members are served in the order evaluate() returns them (by name). With
    # stock too short to satisfy everyone, earlier names are filled first and
    # the rest are reported rather than silently skipped.
    for row in evaluate(rule):
        gap = row["gap"]
        if gap <= 0:
            continue

        if rule["kind"] == "subscription":
            db.execute(
                """INSERT OR IGNORE INTO subscription_seats (subscription_id, upn, assigned_on)
                   VALUES (?,?,?)""", (rule["subscription_id"], row["upn"], today))
            granted_total += 1
            continue

        received = 0
        for _ in range(gap):
            spare = db.q1(
                """SELECT id FROM assets
                   WHERE assigned_upn IS NULL AND category = ?
                   ORDER BY id LIMIT 1""", (rule["category"],))
            if not spare:
                break
            db.execute("UPDATE assets SET assigned_upn = ?, assigned_on = ? WHERE id = ?",
                       (row["upn"], today, spare["id"]))
            received += 1

        granted_total += received
        if received < gap:
            shortfall.append({"upn": row["upn"], "display_name": row["display_name"],
                              "still_short": gap - received})

    return {"granted": granted_total, "shortfall": shortfall}


def compliance_overview() -> list[dict]:
    """One row per active rule, for the Rules page."""
    out = []
    for rule in listing():
        if not rule["active"]:
            out.append({"rule": rule, "summary": None})
            continue
        out.append({"rule": rule, "summary": summarise(rule)})
    return out
