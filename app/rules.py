"""Entitlement rules: what members of an Entra group should have.

A rule states the intended state ("everyone in Design gets 2 monitors") and
compliance is computed against it. Applying one closes the gap outright: there
is no stock to run out of, so a rule never stalls waiting for supply.

Rules grant counted assets and licence seats only. A serial-tracked machine is
a specific physical object with a specific serial - it is assigned by hand or
comes from Intune, and no rule can conjure one.
"""
import datetime

from . import db, pooled


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


def _held(upn: str, category: str, name: str | None) -> int:
    """What a person already holds towards an asset rule.

    Counted units and serial-tracked assets both count. A rule only ever hands
    out the counted kind, but somebody who was already given a serial-tracked
    monitor by hand has a monitor, and reporting them as short would mean
    issuing a second one.
    """
    sql = "SELECT COUNT(*) c FROM assets WHERE assigned_upn = ? AND category = ?"
    params = [upn, category]
    if name:
        sql += " AND name = ?"
        params.append(name)
    return db.q1(sql, params)["c"] + pooled.held_by(upn, category, name)


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
            have = _held(m["upn"], rule["category"], rule["asset_name"])
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
    return {"members": len(rows), "compliant": len(rows) - len(short) - len(over),
            "short": len(short), "over": len(over), "needed": needed,
            "rows": rows,
            "fulfilled": sum(1 for r in rows if r["fulfilled"]),
            "outstanding": len(outstanding),
            "conditions": group_conditions(rule["id"])}


def apply(rule) -> dict:
    """Close the gaps. Everything a rule grants is a record, so it can.

    Counted units and subscription seats are both records of who has what, not
    draws against a shelf, so applying a rule finishes the job in one go. The
    only thing that can be reported back is a rule pointing at an item that no
    longer exists.
    """
    today = datetime.date.today().isoformat()
    granted_total = 0
    shortfall = []

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

        item = pooled.find(rule["category"], rule["asset_name"])
        if not item:
            # The item was renamed or deleted out from under the rule. Say so
            # rather than marking anybody served with nothing.
            shortfall.append({"upn": row["upn"], "display_name": row["display_name"],
                              "still_short": gap})
            continue

        pooled.assign(item["id"], row["upn"], gap)
        granted_total += gap
        _mark(rule["id"], row["upn"], gap)

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
    """What a rule may grant, grouped by category.

    Feeds the second step of the rule form: pick a category, then the actual
    item. Only counted assets appear - a rule hands out "a Dell U2723QE", and
    there is no sensible way for it to hand out a particular serial number.
    """
    return pooled.names_by_category()


def grants_label(rule) -> str:
    if rule["kind"] != "asset":
        return rule["subscription_name"] or "a licence"
    return rule["asset_name"] or f"{rule['category']} (any)"
