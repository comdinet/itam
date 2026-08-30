"""Counted assets: one record for many identical units.

A laptop has a serial and belongs to one person, so an asset row per machine is
right. A mouse does not: fifty identical mice as fifty rows is noise, and the
question worth answering is "who has one, and what did it cost". The same shape
fits licences bought in bulk - four JetBrains seats are one record with a unit
price, not four assets.

This is deliberately not stock control. There is no "how many did we buy": a
unit comes into existence by being handed to somebody, and cost follows the
units, so a person holding two of a 25.00 item carries 50.00. The one place a
count is real is `spare` - kit that came BACK, when somebody left or swapped
machines. Those units are already paid for, so handing one out again costs
nothing new.

It lives under the same categories as the serial-tracked assets, on the same
category page.
"""
import datetime

from . import db


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def create(name: str, category: str, unit_cost_cents: int,
           vendor: str | None = None, notes: str | None = None,
           currency: str | None = None, rate_micro: int | None = None,
           spare: int = 0) -> int:
    return db.execute(
        """INSERT INTO pooled_items (name, category, unit_cost_cents, spare,
                                     vendor, notes, created_at, currency, rate_micro)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (name.strip(), category, max(0, unit_cost_cents), max(0, spare),
         (vendor or "").strip() or None, (notes or "").strip() or None, _now(),
         currency, rate_micro))


def update(item_id: int, name: str, category: str, unit_cost_cents: int,
           vendor: str | None, notes: str | None,
           currency: str | None = None, rate_micro: int | None = None,
           spare: int | None = None) -> str | None:
    """Returns a complaint if the change is impossible, else None."""
    if spare is not None and spare < 0:
        return "The number on the shelf cannot be negative"
    sets = ["name=?", "category=?", "unit_cost_cents=?", "vendor=?", "notes=?",
            "currency=?", "rate_micro=?"]
    params = [name.strip(), category, max(0, unit_cost_cents),
              (vendor or "").strip() or None, (notes or "").strip() or None,
              currency, rate_micro]
    if spare is not None:
        sets.append("spare=?")
        params.append(spare)
    params.append(item_id)
    db.execute(f"UPDATE pooled_items SET {', '.join(sets)} WHERE id=?", params)
    return None


def delete(item_id: int) -> None:
    db.execute("DELETE FROM pooled_items WHERE id = ?", (item_id,))


def get(item_id: int):
    return db.q1("SELECT * FROM pooled_items WHERE id = ?", (item_id,))


def assigned_units(item_id: int) -> int:
    return db.q1("SELECT COALESCE(SUM(quantity),0) q FROM pooled_allocations "
                 "WHERE item_id = ?", (item_id,))["q"]


# Owned is derived, never typed: what people hold, plus what came back.
ASSIGNED = """COALESCE((SELECT SUM(quantity) FROM pooled_allocations a
                        WHERE a.item_id = s.id), 0)"""
OWNED = f"({ASSIGNED} + s.spare)"


def listing(category: str | None = None):
    sql = ("""SELECT s.*, """ + ASSIGNED + """ AS assigned,
                  """ + OWNED + """ AS owned,
                  """ + db.conv(OWNED + " * s.unit_cost_cents", "s.rate_micro") + """ AS value_rep,
                  COALESCE((SELECT COUNT(*) FROM pooled_allocations a
                            WHERE a.item_id = s.id), 0) AS holders
           FROM pooled_items s""")
    params = ()
    if category:
        sql += " WHERE s.category = ?"
        params = (category,)
    return db.q(sql + " ORDER BY s.category, s.name", params)


def summary(item) -> dict:
    assigned = assigned_units(item["id"])
    from . import fx
    rate = item["rate_micro"]
    unit = item["unit_cost_cents"]
    owned = assigned + item["spare"]
    return {
        "assigned": assigned,
        "spare": item["spare"],
        "owned": owned,
        "total_value": owned * unit,
        "assigned_value": assigned * unit,
        "spare_value": item["spare"] * unit,
        "total_value_rep": fx.to_reporting(owned * unit, rate),
        "assigned_value_rep": fx.to_reporting(assigned * unit, rate),
        "holders": db.q(
            """SELECT a.*, u.display_name, u.account_enabled
               FROM pooled_allocations a JOIN users u ON u.upn = a.upn
               WHERE a.item_id = ? ORDER BY u.display_name""", (item["id"],)),
    }


def assign(item_id: int, upn: str, quantity: int = 1) -> str | None:
    """Hand out units. Returns a complaint, or None on success.

    This never refuses for lack of units. Assigning is recording who has what,
    not issuing from a shelf: if something came back it is re-used first, and
    otherwise the unit simply starts existing here.
    """
    item = get(item_id)
    if not item:
        return "No such item"
    if quantity < 1:
        return "Quantity must be at least 1"
    if not db.q1("SELECT 1 FROM users WHERE upn = ?", (upn,)):
        return "No such person"
    reused = min(quantity, item["spare"])
    if reused:
        db.execute("UPDATE pooled_items SET spare = spare - ? WHERE id = ?",
                   (reused, item_id))
    db.execute(
        """INSERT INTO pooled_allocations (item_id, upn, quantity, assigned_on)
           VALUES (?,?,?,?)
           ON CONFLICT(item_id, upn) DO UPDATE SET
               quantity = quantity + excluded.quantity,
               assigned_on = excluded.assigned_on""",
        (item_id, upn, quantity, datetime.date.today().isoformat()))
    return None


def take_back(item_id: int, upn: str, quantity: int | None = None) -> str | None:
    """Take units back. Without a quantity, takes back all of them.

    Returned units go on the shelf rather than vanishing: they were paid for,
    and the next person to need one should get that one.
    """
    row = db.q1("SELECT quantity FROM pooled_allocations WHERE item_id = ? AND upn = ?",
                (item_id, upn))
    if not row:
        return "That person holds none of this item"
    returned = row["quantity"] if quantity is None else min(max(1, quantity), row["quantity"])
    if quantity is None or quantity >= row["quantity"]:
        db.execute("DELETE FROM pooled_allocations WHERE item_id = ? AND upn = ?",
                   (item_id, upn))
    else:
        db.execute(
            "UPDATE pooled_allocations SET quantity = quantity - ? "
            "WHERE item_id = ? AND upn = ?", (returned, item_id, upn))
    db.execute("UPDATE pooled_items SET spare = spare + ? WHERE id = ?",
               (returned, item_id))
    return None


def totals(category: str | None = None) -> dict:
    # Values are in the reporting currency: counted items may be priced in
    # several, and raw sums across them would be meaningless.
    where = " WHERE s.category = ?" if category else ""
    params = (category,) if category else ()
    row = db.q1(
        """SELECT COALESCE(SUM(""" + OWNED + """),0) AS units,
                  COALESCE(SUM(""" + db.conv(OWNED + " * s.unit_cost_cents", "s.rate_micro") + """),0) AS value,
                  COALESCE(SUM(s.spare),0) AS spare_units,
                  COALESCE(SUM(""" + db.conv("s.spare * s.unit_cost_cents", "s.rate_micro") + """),0) AS spare_value,
                  COUNT(*) AS items FROM pooled_items s""" + where, params)
    alloc = db.q1(
        """SELECT COALESCE(SUM(a.quantity),0) AS units,
                  COALESCE(SUM(""" + db.conv("a.quantity * s.unit_cost_cents", "s.rate_micro") + """),0) AS value
           FROM pooled_allocations a JOIN pooled_items s ON s.id = a.item_id"""
        + (" WHERE s.category = ?" if category else ""), params)
    # Not named "items": in a template, dict.items is the method, not the key.
    return {"item_count": row["items"], "units": row["units"], "value": row["value"],
            "assigned_units": alloc["units"], "assigned_value": alloc["value"],
            "spare_units": row["spare_units"], "spare_value": row["spare_value"]}


def for_user(upn: str):
    return db.q(
        """SELECT s.id, s.name, s.category, s.unit_cost_cents, s.currency,
                  s.rate_micro, a.quantity, a.assigned_on,
                  (a.quantity * s.unit_cost_cents) AS cost,
                  """ + db.conv("a.quantity * s.unit_cost_cents", "s.rate_micro") + """ AS cost_rep
           FROM pooled_allocations a JOIN pooled_items s ON s.id = a.item_id
           WHERE a.upn = ? ORDER BY s.category, s.name""", (upn,))


def names_by_category() -> dict:
    """Distinct counted-item names, grouped by category.

    This is what entitlement rules may grant. Serial-tracked assets are not
    here on purpose: each is a specific physical machine, assigned by hand or
    from Intune, and no rule can conjure a serial number.
    """
    out: dict[str, list[str]] = {}
    for row in db.q("""SELECT DISTINCT category, name FROM pooled_items
                       WHERE TRIM(COALESCE(name,'')) != '' ORDER BY category, name"""):
        out.setdefault(row["category"], []).append(row["name"])
    return out


def find(category: str, name: str | None = None):
    """One counted item. Without a name, whichever came first in the category.

    Rules made since the item picker existed always name one. Older ones say
    only "a monitor", and picking the first is better than reporting them
    permanently unfulfillable.
    """
    if name:
        return db.q1("SELECT * FROM pooled_items WHERE category = ? AND name = ? "
                     "ORDER BY id LIMIT 1", (category, name))
    return db.q1("SELECT * FROM pooled_items WHERE category = ? ORDER BY id LIMIT 1",
                 (category,))


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
