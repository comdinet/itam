"""Counted assets: no stock control, but returns are remembered.

Handing something out is recording who has it, not drawing from a shelf, so it
must never be refused. The one real count is what came BACK - already paid for,
so re-issuing it must not add to the spend.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, pooled
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

for upn, name in [("ada@x.com", "Ada"), ("grace@x.com", "Grace"), ("hedy@x.com", "Hedy")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))

print("--- a new item owns nothing until somebody is given one ---")
mice = pooled.create("Logitech M185 mouse", "Peripheral", 2500, vendor="Logitech")
s = pooled.summary(pooled.get(mice))
check("nothing handed out", s["assigned"], 0)
check("nothing on the shelf", s["spare"], 0)
check("so nothing owned", s["owned"], 0)
check("and nothing spent", s["total_value"], 0)

print("\n--- handing out is never refused ---")
check("one to Ada", pooled.assign(mice, "ada@x.com", 1), None)
check("three to Grace", pooled.assign(mice, "grace@x.com", 3), None)
check("ninety-nine to Hedy, with no stock anywhere",
      pooled.assign(mice, "hedy@x.com", 99), None)
s = pooled.summary(pooled.get(mice))
check("all of it recorded", s["assigned"], 103)
check("owned follows what was handed out", s["owned"], 103)
check("spend follows the units", s["total_value"], 103 * 2500)
check("three holders", len(s["holders"]), 3)

print("\n--- a second unit adds to the same row, not a new one ---")
check("one more to Ada", pooled.assign(mice, "ada@x.com", 1), None)
check("still three holders", len(pooled.summary(pooled.get(mice))["holders"]), 3)
check("Ada's row went to two",
      db.q1("SELECT quantity FROM pooled_allocations WHERE item_id=? AND upn='ada@x.com'",
            (mice,))["quantity"], 2)

print("\n--- what someone holds, and what it costs them ---")
ada = pooled.for_user("ada@x.com")[0]
check("Ada holds two", ada["quantity"], 2)
check("costing 50.00", ada["cost"], 5000)
check("Grace holds three", pooled.for_user("grace@x.com")[0]["quantity"], 3)

print("\n--- taking back puts units on the shelf, not into thin air ---")
before = pooled.summary(pooled.get(mice))["owned"]
check("take one back from Grace", pooled.take_back(mice, "grace@x.com", 1), None)
s = pooled.summary(pooled.get(mice))
check("Grace is down to two",
      db.q1("SELECT quantity FROM pooled_allocations WHERE item_id=? AND upn='grace@x.com'",
            (mice,))["quantity"], 2)
check("one is on the shelf", s["spare"], 1)
check("total owned is unchanged", s["owned"], before)
check("and so is the spend", s["total_value"], before * 2500)

check("take back all of Grace's", pooled.take_back(mice, "grace@x.com"), None)
check("her row is gone",
      db.q1("SELECT COUNT(*) c FROM pooled_allocations WHERE item_id=? AND upn='grace@x.com'",
            (mice,))["c"], 0)
check("three on the shelf now", pooled.get(mice)["spare"], 3)
check("taking back from someone holding none is refused",
      pooled.take_back(mice, "grace@x.com") is not None, True)

print("\n--- re-issuing from the shelf costs nothing new ---")
before = pooled.summary(pooled.get(mice))["owned"]
pooled.assign(mice, "grace@x.com", 2)
s = pooled.summary(pooled.get(mice))
check("one left on the shelf", s["spare"], 1)
check("owned did not grow", s["owned"], before)
check("so the spend did not either", s["total_value"], before * 2500)

print("\n--- handing out more than the shelf holds tops up from new ---")
before = pooled.summary(pooled.get(mice))["owned"]
pooled.assign(mice, "hedy@x.com", 5)          # 1 from the shelf, 4 new
s = pooled.summary(pooled.get(mice))
check("shelf emptied", s["spare"], 0)
check("owned grew by the shortfall only", s["owned"], before + 4)

print("\n--- refusals that still apply ---")
check("an unknown person", pooled.assign(mice, "nobody@x.com", 1) is not None, True)
check("a quantity below one", pooled.assign(mice, "ada@x.com", 0) is not None, True)
check("an unknown item", pooled.assign(99999, "ada@x.com", 1) is not None, True)

print("\n--- editing ---")
check("a negative shelf count is refused",
      pooled.update(mice, "Logitech M185", "Peripheral", 2500, None, None, spare=-1) is not None,
      True)
check("correcting the shelf count",
      pooled.update(mice, "Logitech M185 mouse", "Peripheral", 2500, "Logitech", None,
                    spare=4), None)
check("it took", pooled.get(mice)["spare"], 4)
check("price change is accepted",
      pooled.update(mice, "Logitech M185 mouse", "Peripheral", 2900, "Logitech", None), None)
check("and applies to every unit",
      pooled.summary(pooled.get(mice))["total_value"],
      pooled.summary(pooled.get(mice))["owned"] * 2900)

print("\n--- listing and totals ---")
pooled.create("JetBrains All Products Pack", "Software", 77900)
check("both items listed", len(pooled.listing()), 2)
check("filtered to one category", len(pooled.listing("Peripheral")), 1)
t = pooled.totals()
check("item count", t["item_count"], 2)
check("units total matches the item", t["units"], pooled.summary(pooled.get(mice))["owned"])
check("shelf total", t["spare_units"], 4)

print("\n--- deleting takes the allocations with it ---")
pooled.delete(mice)
check("allocations gone",
      db.q1("SELECT COUNT(*) c FROM pooled_allocations WHERE item_id=?", (mice,))["c"], 0)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
