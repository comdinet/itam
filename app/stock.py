"""Pooled items: one record for many identical units.

A laptop has a serial and belongs to one person, so an asset row per machine is
right. A mouse does not: fifty identical mice as fifty rows is noise, and the
question worth answering is "how many do we own, how many are out, what did
they cost". The same shape fits licences bought in bulk - four JetBrains seats
are one purchase with a unit price, not four assets.

So a stock item carries a unit price and how many units are owned, and
allocations count against that. Cost follows the units: a person holding two
units of a 25.00 item carries 50.00.
"""
import datetime

from . import db


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def create(name: str, category: str, unit_cost_cents: int, quantity: int,
           vendor: str | None = None, notes: str | None = None) -> int:
    return db.execute(
        """INSERT INTO stock_items (name, category, unit_cost_cents, quantity,
                                    vendor, notes, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (name.strip(), category, max(0, unit_cost_cents), max(0, quantity),
         (vendor or "").strip() or None, (notes or "").strip() or None, _now()))


def update(item_id: int, name: str, category: str, unit_cost_cents: int,
           quantity: int, vendor: str | None, notes: str | None) -> str | None:
    """Returns a complaint if the change is impossible, else None."""
    allocated = allocated_units(item_id)
    if quantity < allocated:
        return (f"{allocated} unit(s) are already handed out, so the quantity "
                f"owned cannot drop below that. Take some back first.")
    db.execute(
        """UPDATE stock_items SET name=?, category=?, unit_cost_cents=?, quantity=?,
                                  vendor=?, notes=? WHERE id=?""",
        (name.strip(), category, max(0, unit_cost_cents), max(0, quantity),
         (vendor or "").strip() or None, (notes or "").strip() or None, item_id))
    return None


def delete(item_id: int) -> None:
    db.execute("DELETE FROM stock_items WHERE id = ?", (item_id,))


def get(item_id: int):
    return db.q1("SELECT * FROM stock_items WHERE id = ?", (item_id,))


def allocated_units(item_id: int) -> int:
    return db.q1("SELECT COALESCE(SUM(quantity),0) q FROM stock_allocations "
                 "WHERE item_id = ?", (item_id,))["q"]


def listing():
    return db.q(
        """SELECT s.*,
                  COALESCE((SELECT SUM(quantity) FROM stock_allocations a
                            WHERE a.item_id = s.id), 0) AS allocated,
                  COALESCE((SELECT COUNT(*) FROM stock_allocations a
                            WHERE a.item_id = s.id), 0) AS holders
           FROM stock_items s ORDER BY s.category, s.name""")


def summary(item) -> dict:
    allocated = allocated_units(item["id"])
    return {
        "allocated": allocated,
        "available": item["quantity"] - allocated,
        "total_value": item["quantity"] * item["unit_cost_cents"],
        "allocated_value": allocated * item["unit_cost_cents"],
        "holders": db.q(
            """SELECT a.*, u.display_name, u.account_enabled
               FROM stock_allocations a JOIN users u ON u.upn = a.upn
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
        """INSERT INTO stock_allocations (item_id, upn, quantity, assigned_on)
           VALUES (?,?,?,?)
           ON CONFLICT(item_id, upn) DO UPDATE SET
               quantity = quantity + excluded.quantity,
               assigned_on = excluded.assigned_on""",
        (item_id, upn, quantity, datetime.date.today().isoformat()))
    return None


def take_back(item_id: int, upn: str, quantity: int | None = None) -> str | None:
    """Return units to stock. Without a quantity, takes back all of them."""
    row = db.q1("SELECT quantity FROM stock_allocations WHERE item_id = ? AND upn = ?",
                (item_id, upn))
    if not row:
        return "That person holds none of this item"
    if quantity is None or quantity >= row["quantity"]:
        db.execute("DELETE FROM stock_allocations WHERE item_id = ? AND upn = ?",
                   (item_id, upn))
    else:
        db.execute(
            "UPDATE stock_allocations SET quantity = quantity - ? "
            "WHERE item_id = ? AND upn = ?", (max(1, quantity), item_id, upn))
    return None


def totals() -> dict:
    row = db.q1(
        """SELECT COALESCE(SUM(quantity),0) AS units,
                  COALESCE(SUM(quantity * unit_cost_cents),0) AS value,
                  COUNT(*) AS items FROM stock_items""")
    alloc = db.q1(
        """SELECT COALESCE(SUM(a.quantity),0) AS units,
                  COALESCE(SUM(a.quantity * s.unit_cost_cents),0) AS value
           FROM stock_allocations a JOIN stock_items s ON s.id = a.item_id""")
    # Not named "items": in a template, dict.items is the method, not the key.
    return {"item_count": row["items"], "units": row["units"], "value": row["value"],
            "allocated_units": alloc["units"], "allocated_value": alloc["value"],
            "spare_units": row["units"] - alloc["units"],
            "spare_value": row["value"] - alloc["value"]}


def for_user(upn: str):
    return db.q(
        """SELECT s.id, s.name, s.category, s.unit_cost_cents, a.quantity,
                  a.assigned_on, (a.quantity * s.unit_cost_cents) AS cost
           FROM stock_allocations a JOIN stock_items s ON s.id = a.item_id
           WHERE a.upn = ? ORDER BY s.category, s.name""", (upn,))
