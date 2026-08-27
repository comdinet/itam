"""Device groups defined by specification, each carrying a price.

Pricing a fleet one machine at a time does not scale, and the model alone is
often not enough - two MacBook Airs of the same model differ by memory and
disk, which on macOS arrive as Intune custom attributes. So a group is a set of
criteria (all must match) over the asset, its linked Intune device, and that
device's custom attributes, and the group carries the price.

Applying a group writes its price onto every matching asset. It never deletes
or reassigns anything.
"""
import datetime

from . import db

# field -> (label, SQL expression). Whitelisted: the field name reaches SQL.
FIELDS = {
    "model":        ("Model", "COALESCE(NULLIF(TRIM(d.model),''), a.name)"),
    "manufacturer": ("Manufacturer", "COALESCE(d.manufacturer,'')"),
    "os":           ("Operating system", "COALESCE(d.os,'')"),
    "category":     ("Category", "COALESCE(a.category,'')"),
    "attribute":    ("Custom attribute", None),      # handled separately
}

OPS = {
    "eq":       ("is exactly", "LOWER({expr}) = LOWER(?)"),
    "contains": ("contains", "LOWER({expr}) LIKE '%' || LOWER(?) || '%'"),
    "starts":   ("starts with", "LOWER({expr}) LIKE LOWER(?) || '%'"),
}


def create(name: str, price_cents: int, notes: str | None = None,
           currency: str | None = None, rate_micro: int | None = None) -> int:
    return db.execute(
        """INSERT INTO price_groups (name, price_cents, notes, created_at,
                                     currency, rate_micro)
           VALUES (?,?,?,?,?,?)""",
        (name.strip(), max(0, price_cents), (notes or "").strip() or None,
         datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
         currency, rate_micro))


def listing():
    return db.q("SELECT * FROM price_groups ORDER BY name")


def get(group_id: int):
    return db.q1("SELECT * FROM price_groups WHERE id = ?", (group_id,))


def delete(group_id: int) -> None:
    db.execute("DELETE FROM price_groups WHERE id = ?", (group_id,))


def update(group_id: int, name: str, price_cents: int, notes: str | None,
           currency: str | None = None, rate_micro: int | None = None) -> None:
    db.execute(
        """UPDATE price_groups SET name = ?, price_cents = ?, notes = ?,
                                   currency = ?, rate_micro = ? WHERE id = ?""",
        (name.strip(), max(0, price_cents), (notes or "").strip() or None,
         currency, rate_micro, group_id))


def criteria(group_id: int):
    return db.q("SELECT * FROM price_group_criteria WHERE group_id = ? ORDER BY id",
                (group_id,))


def add_criterion(group_id: int, field: str, op: str, value: str,
                  attr_name: str | None = None) -> None:
    if field not in FIELDS or op not in OPS:
        raise ValueError("unknown field or operator")
    if field == "attribute" and not (attr_name or "").strip():
        raise ValueError("an attribute criterion needs the attribute name")
    db.execute(
        """INSERT INTO price_group_criteria (group_id, field, attr_name, op, value)
           VALUES (?,?,?,?,?)""",
        (group_id, field, (attr_name or "").strip() or None, op, value.strip()))


def delete_criterion(criterion_id: int) -> None:
    db.execute("DELETE FROM price_group_criteria WHERE id = ?", (criterion_id,))


def describe(criterion) -> str:
    field_label = FIELDS.get(criterion["field"], (criterion["field"], None))[0]
    if criterion["field"] == "attribute":
        field_label = f"Attribute '{criterion['attr_name']}'"
    op_label = OPS.get(criterion["op"], (criterion["op"], None))[0]
    return f"{field_label} {op_label} “{criterion['value']}”"


def _where(rows) -> tuple[str, list]:
    """Build the WHERE fragment for a group's criteria. Field and operator are
    looked up in whitelists, so only the value ever reaches SQL as data."""
    clauses, params = [], []
    for c in rows:
        op_sql = OPS[c["op"]][1]
        if c["field"] == "attribute":
            inner = op_sql.format(expr="da.value")
            clauses.append(
                "EXISTS (SELECT 1 FROM device_attributes da "
                "WHERE da.device_id = d.id AND LOWER(da.name) = LOWER(?) "
                f"AND {inner})")
            params.extend([c["attr_name"] or "", c["value"]])
        else:
            expr = FIELDS[c["field"]][1]
            clauses.append(op_sql.format(expr=expr))
            params.append(c["value"])
    return (" AND ".join(clauses) if clauses else "1=0"), params


def matching_assets(group_id: int):
    """Assets a group covers. No criteria matches nothing, deliberately - an
    empty group silently repricing the whole estate would be worse."""
    rows = criteria(group_id)
    where, params = _where(rows)
    return db.q(
        f"""SELECT a.*, d.model AS device_model, d.os AS device_os,
                   d.manufacturer AS device_manufacturer, d.device_name
            FROM assets a
            LEFT JOIN devices d ON d.asset_id = a.id
            WHERE {where}
            ORDER BY a.name, a.serial""", params)


def summary(group) -> dict:
    assets = matching_assets(group["id"])
    at_price = [a for a in assets
                if a["cost_cents"] == group["price_cents"]
                and (a["currency"] or "") == (group["currency"] or "")]
    from . import fx
    return {"criteria": criteria(group["id"]), "matched": len(assets),
            "at_price": len(at_price), "to_change": len(assets) - len(at_price),
            "assets": assets,
            "current_total": sum(fx.to_reporting(a["cost_cents"], a["rate_micro"])
                                 for a in assets),
            "priced_total": fx.to_reporting(
                len(assets) * group["price_cents"], group["rate_micro"])}


def apply(group) -> dict:
    """Write the group's price onto every matching asset."""
    changed = 0
    for asset in matching_assets(group["id"]):
        if (asset["cost_cents"] != group["price_cents"]
                or (asset["currency"] or "") != (group["currency"] or "")):
            # The price carries its currency and frozen rate with it, or the
            # asset would inherit a number with no idea what it is in.
            db.execute(
                """UPDATE assets SET cost_cents = ?, currency = ?, rate_micro = ?
                   WHERE id = ?""",
                (group["price_cents"], group["currency"], group["rate_micro"],
                 asset["id"]))
            changed += 1
    return {"changed": changed}


def price_for_asset(asset_id: int) -> dict | None:
    """The price of the first group covering this asset, if any.

    Used when an asset is created from an Intune device, so a new machine of a
    known specification is priced without anyone touching it.
    """
    for group in listing():
        if not criteria(group["id"]):
            continue
        where, params = _where(criteria(group["id"]))
        hit = db.q1(
            f"""SELECT a.id FROM assets a
                LEFT JOIN devices d ON d.asset_id = a.id
                WHERE a.id = ? AND {where}""", [asset_id] + params)
        if hit:
            return {"price_cents": group["price_cents"],
                    "currency": group["currency"],
                    "rate_micro": group["rate_micro"]}
    return None


def attribute_names() -> list[str]:
    return [r["name"] for r in
            db.q("SELECT DISTINCT name FROM device_attributes ORDER BY name")]


def known_models() -> list[str]:
    return [r["m"] for r in db.q(
        """SELECT DISTINCT COALESCE(NULLIF(TRIM(d.model),''), a.name) AS m
           FROM assets a LEFT JOIN devices d ON d.asset_id = a.id
           WHERE m IS NOT NULL AND TRIM(m) != '' ORDER BY m""")]
