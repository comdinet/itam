"""A database written before Stock moved into Assets must come across intact.

Two migrations run over it. The tables were renamed, and CREATE TABLE IF NOT
EXISTS would happily make an empty pooled_items alongside the real stock_items,
leaving every unit stranded in a table nothing reads. Then the typed "how many
did we buy" went away: what you owned but had not handed out is exactly what is
on the shelf, so that is what it becomes.
"""
import os, sqlite3, sys, tempfile
DB = os.path.join(tempfile.mkdtemp(), "legacy.db")
os.environ["ITAM_DB"] = DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

# The old shape, as an installed copy would have it on disk.
old = sqlite3.connect(DB)
old.executescript("""
CREATE TABLE users (upn TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    job_title TEXT, department TEXT, entra_id TEXT,
                    account_enabled INTEGER NOT NULL DEFAULT 1, country TEXT,
                    usage_location TEXT, source TEXT NOT NULL DEFAULT 'manual',
                    synced_at TEXT);
CREATE TABLE stock_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Peripheral',
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    quantity INTEGER NOT NULL DEFAULT 0, vendor TEXT, notes TEXT,
    created_at TEXT, currency TEXT, rate_micro INTEGER);
CREATE TABLE stock_allocations (
    item_id INTEGER NOT NULL REFERENCES stock_items(id) ON DELETE CASCADE,
    upn TEXT NOT NULL REFERENCES users(upn) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 1, assigned_on TEXT,
    PRIMARY KEY (item_id, upn));
CREATE INDEX idx_stock_alloc_upn ON stock_allocations(upn);
INSERT INTO users (upn, display_name) VALUES ('ada@x.com', 'Ada');
INSERT INTO stock_items (id, name, category, unit_cost_cents, quantity, currency, rate_micro)
     VALUES (1, 'Logitech M185 mouse', 'Peripheral', 2500, 10, 'USD', 1000000);
INSERT INTO stock_allocations (item_id, upn, quantity, assigned_on)
     VALUES (1, 'ada@x.com', 3, '2026-01-05');
""")
old.commit()
old.close()

from app import db, pooled          # noqa: E402 - the old DB has to exist first
db.init_db()

check("the item came across", pooled.get(1)["name"], "Logitech M185 mouse")
check("owned-but-not-handed-out became the shelf count", pooled.get(1)["spare"], 7)
check("the typed quantity is gone",
      "quantity" in pooled.get(1).keys(), False)
check("and owned still totals ten", pooled.summary(pooled.get(1))["owned"], 10)
check("its currency and frozen rate survived",
      (pooled.get(1)["currency"], pooled.get(1)["rate_micro"]), ("USD", 1000000))
check("the allocation came across", pooled.held_by("ada@x.com", "Peripheral"), 3)
check("nothing was left behind in an empty new table",
      db.q1("SELECT COUNT(*) c FROM pooled_items")["c"], 1)

names = {r["name"] for r in
         db.q("SELECT name FROM sqlite_master WHERE type='table'")}
check("the old tables are gone", "stock_items" in names or "stock_allocations" in names, False)
idx = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='index'")}
check("the old index was dropped, not duplicated", "idx_stock_alloc_upn" in idx, False)
check("the new index exists", "idx_pooled_alloc_upn" in idx, True)

# The foreign key must have followed the rename, or a delete would orphan rows.
db.execute("DELETE FROM pooled_items WHERE id = 1")
check("cascade still works", db.q1("SELECT COUNT(*) c FROM pooled_allocations")["c"], 0)

# Running it a second time must be a no-op, not an error.
db.init_db()
check("re-running init_db is harmless",
      db.q1("SELECT COUNT(*) c FROM pooled_items")["c"], 0)

print("\nFAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
