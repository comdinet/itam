"""Pooled assets: one record for many identical units.

A laptop has a serial and belongs to one person, so an asset row per machine is
right. A mouse does not: fifty identical mice as fifty rows is noise, and the
question worth answering is "how many do we own, how many are out, what did
they cost". The same shape fits licences bought in bulk - four JetBrains seats
are one purchase with a unit price, not four assets.

So a pooled item carries a unit price and how many units are owned, and
allocations count against that. Cost follows the units: a person holding two
units of a 25.00 item carries 50.00. It lives under the same categories as the
individually tracked assets, on the same category page.
"""
import datetime

from . import db


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def create(name: str, category: str, unit_cost_cents: int, quantity: int,
           vendor: str | None = None, notes: str | None = None,
           currency: str | None = None, rate_micro: int | None = None) -> int:
    return db.execute(
        """INSERT INTO pooled_items (name, category, unit_cost_cents, quantity,
                                    vendor, notes, created_at, currency, rate_micro)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (name.strip(), category, max(0, unit_cost_cents), max(0, quantity),
         (vendor or "").strip() or None, (notes or "").strip() or None, _now(),
         currency, rate_micro))


def update(item_id: int, name: str, category: str, unit_cost_cents: int,
           quantity: int, vendor: str | None, notes: str | None,
           currency: str | None = None, rate_micro: int | None = None) -> str | None:
    """Returns a complaint if the change is impossible, else None."""
    allocated = allocated_units(item_id)
    if quantity < allocated:
        return (f"{allocated} unit(s) are already handed out, so the quantity "
                f"owned cannot drop below that. Take some back first.")
    db.execute(
        """UPDATE pooled_items SET name=?, category=?, unit_cost_cents=?, quantity=?,
                                  vendor=?, notes=?, currency=?, rate_micro=?
           WHERE id=?""",
        (name.strip(), category, max(0, unit_cost_cents), max(0, quantity),
         (vendor or "").strip() or None, (notes or "").strip() or None,
         currency, rate_micro, item_id))
    return None


def delete(item_id: int) -> None:
    db.execute("DELETE FROM pooled_items WHERE id = ?", (item_id,))


def get(item_id: int):
    return db.q1("SELECT * FROM pooled_items WHERE id = ?", (item_id,))


def allocated_units(item_id: int) -> int:
    return db.q1("SELECT COALESCE(SUM(quantity),0) q FROM pooled_allocations "
                 "WHERE item_id = ?", (item_id,))["q"]


def listing(category: str | None = None):
    sql = ("""SELECT s.*,
                  """ + db.conv("s.quantity * s.unit_cost_cents", "s.rate_micro") + """ AS value_rep,
                  COALESCE((SELECT SUM(quantity) FROM pooled_allocations a
                            WHERE a.item_id = s.id), 0) AS allocated,
                  COALESCE((SELECT COUNT(*) FROM pooled_allocations a
                            WHERE a.item_id = s.id), 0) AS holders
           FROM pooled_items s""")
    params = ()
    if category:
        sql += " WHERE s.category = ?"
        params = (category,)
    return db.q(sql + " ORDER BY s.category, s.name", params)


def summary(item) -> dict:
    allocated = allocated_units(item["id"])
    from . import fx
    rate = item["rate_micro"]
    return {
        "allocated": allocated,
        "available": item["quantity"] - allocated,
        "total_value": item["quantity"] * item["unit_cost_cents"],
        "allocated_value": allocated * item["unit_cost_cents"],
        "total_value_rep": fx.to_reporting(item["quantity"] * item["unit_cost_cents"], rate),
        "allocated_value_rep": fx.to_reporting(allocated * item["unit_cost_cents"], rate),
        "holders": db.q(
            """SELECT a.*, u.display_name, u.account_enabled
               FROM pooled_allocations a JOIN users u ON u.upn = a.upn
               WHERE a.item_id = ? ORDER BY u.display_name""", (item["id"],)),
    }


def assign(item_id: int, upn: str, quantity: int = 1) -> str | None:
    """Hand out units. Returns a complaint, or None on success."""
    item = get(item_id)
    if not item:
        return "No such item"
    if quantity < 1:
        return "Quantity must be at least 1"
    if not db.q1("SELECT 1 FROM users WHERE upn = ?", (upn,)):
        return "No such person"
    available = item["quantity"] - allocated_units(item_id)
    if quantity > available:
        return (f"Only {available} unit(s) available. Raise the quantity owned "
                f"first, or take some back.")
    db.execute(
        """INSERT INTO pooled_allocations (item_id, upn, quantity, assigned_on)
           VALUES (?,?,?,?)
           ON CONFLICT(item_id, upn) DO UPDATE SET
               quantity = quantity + excluded.quantity,
               assigned_on = excluded.assigned_on""",
        (item_id, upn, quantity, datetime.date.today().isoformat()))
    return None


def take_back(item_id: int, upn: str, quantity: int | None = None) -> str | None:
    """Return units to the pool. Without a quantity, takes back all of them."""
    row = db.q1("SELECT quantity FROM pooled_allocations WHERE item_id = ? AND upn = ?",
                (item_id, upn))
    if not row:
        return "That person holds none of this item"
    if quantity is None or quantity >= row["quantity"]:
        db.execute("DELETE FROM pooled_allocations WHERE item_id = ? AND upn = ?",
                   (item_id, upn))
    else:
        db.execute(
            "UPDATE pooled_allocations SET quantity = quantity - ? "
            "WHERE item_id = ? AND upn = ?", (max(1, quantity), item_id, upn))
    return None


def totals(category: str | None = None) -> dict:
    # Values are in the reporting currency: pooled items may be priced in
    # several, and raw sums across them would be meaningless.
    where = " WHERE category = ?" if category else ""
    params = (category,) if category else ()
    row = db.q1(
        """SELECT COALESCE(SUM(quantity),0) AS units,
                  COALESCE(SUM(""" + db.conv("quantity * unit_cost_cents", "rate_micro") + """),0) AS value,
                  COUNT(*) AS items FROM pooled_items""" + where, params)
    alloc = db.q1(
        """SELECT COALESCE(SUM(a.quantity),0) AS units,
                  COALESCE(SUM(""" + db.conv("a.quantity * s.unit_cost_cents", "s.rate_micro") + """),0) AS value
           FROM pooled_allocations a JOIN pooled_items s ON s.id = a.item_id"""
        + (" WHERE s.category = ?" if category else ""), params)
    # Not named "items": in a template, dict.items is the method, not the key.
    return {"item_count": row["items"], "units": row["units"], "value": row["value"],
            "allocated_units": alloc["units"], "allocated_value": alloc["value"],
            "spare_units": row["units"] - alloc["units"],
            "spare_value": row["value"] - alloc["value"]}


def for_user(upn: str):
    return db.q(
        """SELECT s.id, s.name, s.category, s.unit_cost_cents, s.currency,
                  s.rate_micro, a.quantity, a.assigned_on,
                  (a.quantity * s.unit_cost_cents) AS cost,
                  """ + db.conv("a.quantity * s.unit_cost_cents", "s.rate_micro") + """ AS cost_rep
           FROM pooled_allocations a JOIN pooled_items s ON s.id = a.item_id
           WHERE a.upn = ? ORDER BY s.category, s.name""", (upn,))


def names_by_category() -> dict:
    """Distinct pooled item names, grouped by category.

    Rules pick an item by name, and a pooled item is as grantable as an
    individually tracked one, so both feed the same list.
    """
    out: dict[str, list[str]] = {}
    for row in db.q("""SELECT DISTINCT category, name FROM pooled_items
                       WHERE TRIM(COALESCE(name,'')) != '' ORDER BY category, name"""):
        out.setdefault(row["category"], []).append(row["name"])
    return out


def held_by(upn: str, category: str, name: str | None = None) -> int:
    """How many units of this category (or named item) a person holds."""
    sql = """SELECT COALESCE(SUM(a.quantity),0) c
             FROM pooled_allocations a JOIN pooled_items s ON s.id = a.item_id
             WHERE a.upn = ? AND s.category = ?"""
    params = [upn, category]
    if name:
        sql += " AND s.name = ?"
        params.append(name)
    return db.q1(sql, params)["c"]


def spare_units(category: str, name: str | None = None) -> int:
    """Units not handed out, for this category or one named item."""
    sql = """SELECT COALESCE(SUM(s.quantity - COALESCE(
                 (SELECT SUM(quantity) FROM pooled_allocations a WHERE a.item_id = s.id), 0)), 0) c
             FROM pooled_items s WHERE s.category = ?"""
    params = [category]
    if name:
        sql += " AND s.name = ?"
        params.append(name)
    return max(0, db.q1(sql, params)["c"])


def take_one(upn: str, category: str, name: str | None = None) -> bool:
    """Hand out a single spare unit. False when the pool has none left."""
    sql = """SELECT s.id FROM pooled_items s
             WHERE s.category = ?
               AND s.quantity > COALESCE(
                     (SELECT SUM(quantity) FROM pooled_allocations a
                      WHERE a.item_id = s.id), 0)"""
    params = [category]
    if name:
        sql += " AND s.name = ?"
        params.append(name)
    row = db.q1(sql + " ORDER BY s.id LIMIT 1", params)
    if not row:
        return False
    return assign(row["id"], upn, 1) is None
