"""Entitlement rules: what members of an Entra group should have.

A rule states the intended state ("everyone in Design gets 2 monitors"), and
compliance is computed against it. Applying a rule assigns what is actually
available - it never invents hardware that does not exist, because inventing
assets would mean ITAM reporting kit nobody owns.
"""
import datetime

from . import db


def create(name: str, group_id: str, kind: str, quantity: int,
           category: str | None = None, subscription_id: int | None = None,
           asset_name: str | None = None) -> int:
    # A person either holds a licence seat or does not, so quantity only means
    # something for assets. Clamping here keeps such a rule from reading as
    # permanently short.
    if kind == "subscription":
        quantity = 1
    return db.execute(
        """INSERT INTO rules (name, group_id, kind, category, subscription_id,
                              quantity, created_at, asset_name)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name.strip(), group_id, kind, category, subscription_id, max(1, quantity),
         datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
         (asset_name or "").strip() or None))


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


def group_conditions(rule_id: int):
    return db.q(
        """SELECT rg.*, g.display_name FROM rule_groups rg
           JOIN groups g ON g.id = rg.group_id
           WHERE rg.rule_id = ? ORDER BY rg.mode, g.display_name""", (rule_id,))


def add_group(rule_id: int, group_id: str, mode: str) -> str | None:
    if mode not in ("include", "exclude"):
        return "Unknown condition"
    if not db.q1("SELECT 1 FROM groups WHERE id = ?", (group_id,)):
        return "No such group"
    rule = get(rule_id)
    if rule and group_id == rule["group_id"] and mode == "include":
        return "That group is already the one the rule applies to"
    db.execute(
        "INSERT OR IGNORE INTO rule_groups (rule_id, group_id, mode) VALUES (?,?,?)",
        (rule_id, group_id, mode))
    return None


def remove_group(rule_id: int, group_id: str, mode: str) -> None:
    db.execute(
        "DELETE FROM rule_groups WHERE rule_id = ? AND group_id = ? AND mode = ?",
        (rule_id, group_id, mode))


def _members_of(group_id: str) -> set:
    if group_id == db.ALL_USERS_GROUP:
        return {r["upn"] for r in db.q("SELECT upn FROM users")}
    return {r["upn"] for r in
            db.q("SELECT upn FROM group_members WHERE group_id = ?", (group_id,))}


def covered_upns(rule) -> set:
    """Who the rule applies to: the primary group plus any extra includes,
    minus everyone in an excluded group."""
    covered = _members_of(rule["group_id"])
    for cond in group_conditions(rule["id"]):
        if cond["mode"] == "include":
            covered |= _members_of(cond["group_id"])
    for cond in group_conditions(rule["id"]):
        if cond["mode"] == "exclude":
            covered -= _members_of(cond["group_id"])
    return covered


def evaluate(rule) -> list[dict]:
    """Per-member holdings versus the rule. Positive gap means short."""
    upns = covered_upns(rule)
    if not upns:
        return []
    marks = {r["upn"]: r for r in db.q(
        "SELECT * FROM rule_fulfilments WHERE rule_id = ?", (rule["id"],))}
    placeholders = ",".join("?" for _ in upns)
    members = db.q(
        f"""SELECT upn, display_name, account_enabled FROM users
            WHERE upn IN ({placeholders}) ORDER BY display_name""", list(upns))
    out = []
    for m in members:
        if rule["kind"] == "asset":
            if rule["asset_name"]:
                have = db.q1(
                    """SELECT COUNT(*) c FROM assets
                       WHERE assigned_upn = ? AND category = ? AND name = ?""",
                    (m["upn"], rule["category"], rule["asset_name"]))["c"]
            else:
                have = db.q1(
                    "SELECT COUNT(*) c FROM assets WHERE assigned_upn = ? AND category = ?",
                    (m["upn"], rule["category"]))["c"]
        else:
            have = db.q1(
                "SELECT COUNT(*) c FROM subscription_seats WHERE upn = ? AND subscription_id = ?",
                (m["upn"], rule["subscription_id"]))["c"]
        want = 1 if rule["kind"] == "subscription" else rule["quantity"]
        mark = marks.get(m["upn"])
        out.append({"upn": m["upn"], "display_name": m["display_name"],
                    "account_enabled": m["account_enabled"],
                    "have": have, "want": want, "gap": want - have,
                    "fulfilled": bool(mark),
                    "fulfilled_at": mark["fulfilled_at"] if mark else None,
                    "granted": mark["granted"] if mark else 0})
    return out


def summarise(rule) -> dict:
    rows = evaluate(rule)
    # A person the rule has already served is finished with, whatever they hold
    # now: the rule fulfils once, it does not top anyone back up.
    outstanding = [r for r in rows if not r["fulfilled"]]
    short = [r for r in outstanding if r["gap"] > 0]
    over = [r for r in rows if r["gap"] < 0]
    needed = sum(r["gap"] for r in short)
    available = 0
    if rule["kind"] == "asset":
        available = db.q1(
            "SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NULL AND category = ?"
            + (" AND name = ?" if rule["asset_name"] else ""),
            (rule["category"], rule["asset_name"]) if rule["asset_name"]
            else (rule["category"],))["c"]
    return {"members": len(rows), "compliant": len(rows) - len(short) - len(over),
            "short": len(short), "over": len(over), "needed": needed,
            "available": available, "rows": rows,
            "fulfilled": sum(1 for r in rows if r["fulfilled"]),
            "outstanding": len(outstanding),
            "conditions": group_conditions(rule["id"])}


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
        # Already served by this rule: leave them alone. This is what makes a
        # rule a one-time entitlement rather than a level it keeps restoring.
        if row["fulfilled"]:
            continue

        gap = row["gap"]
        if gap <= 0:
            # Already has enough by other means. Mark it done so the rule does
            # not come back to them later if they hand something in.
            _mark(rule["id"], row["upn"], 0)
            continue

        if rule["kind"] == "subscription":
            db.execute(
                """INSERT OR IGNORE INTO subscription_seats (subscription_id, upn, assigned_on)
                   VALUES (?,?,?)""", (rule["subscription_id"], row["upn"], today))
            _mark(rule["id"], row["upn"], 1)
            granted_total += 1
            continue

        received = 0
        for _ in range(gap):
            if rule["asset_name"]:
                spare = db.q1(
                    """SELECT id FROM assets
                       WHERE assigned_upn IS NULL AND category = ? AND name = ?
                       ORDER BY id LIMIT 1""", (rule["category"], rule["asset_name"]))
            else:
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
        if received >= gap:
            # Recorded as served only when the entitlement was met in full.
            # Someone who got one of two monitors is not finished with: a later
            # apply completes them once there is stock, which is different from
            # topping somebody up after they hand an item back.
            _mark(rule["id"], row["upn"], received)
        else:
            shortfall.append({"upn": row["upn"], "display_name": row["display_name"],
                              "still_short": gap - received})

    return {"granted": granted_total, "shortfall": shortfall}


def _mark(rule_id: int, upn: str, granted: int) -> None:
    db.execute(
        """INSERT INTO rule_fulfilments (rule_id, upn, granted, fulfilled_at)
           VALUES (?,?,?,?)
           ON CONFLICT(rule_id, upn) DO UPDATE SET
               granted = rule_fulfilments.granted + excluded.granted""",
        (rule_id, upn, granted, datetime.date.today().isoformat()))


def clear_fulfilment(rule_id: int, upn: str) -> None:
    """Let a rule serve someone again - for when it was applied by mistake."""
    db.execute("DELETE FROM rule_fulfilments WHERE rule_id = ? AND upn = ?",
               (rule_id, upn))


def clear_all_fulfilments(rule_id: int) -> int:
    n = db.q1("SELECT COUNT(*) c FROM rule_fulfilments WHERE rule_id = ?",
              (rule_id,))["c"]
    db.execute("DELETE FROM rule_fulfilments WHERE rule_id = ?", (rule_id,))
    return n


def compliance_overview() -> list[dict]:
    """One row per active rule, for the Rules page."""
    out = []
    for rule in listing():
        if not rule["active"]:
            out.append({"rule": rule, "summary": None})
            continue
        out.append({"rule": rule, "summary": summarise(rule)})
    return out


def assets_by_category() -> dict:
    """Distinct item names you own, grouped by category.

    Feeds the second step of the rule form: pick a category, then the actual
    item, so a rule can grant "a Dell U2723QE" rather than "any monitor".
    """
    out: dict[str, list[str]] = {}
    for row in db.q(
            """SELECT DISTINCT category, name FROM assets
               WHERE TRIM(COALESCE(name,'')) != '' ORDER BY category, name"""):
        out.setdefault(row["category"], []).append(row["name"])
    return out


def grants_label(rule) -> str:
    if rule["kind"] != "asset":
        return rule["subscription_name"] or "a licence"
    if rule["asset_name"]:
        return rule["asset_name"]
    return f"{rule['category']} (any)"
