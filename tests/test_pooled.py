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

for upn, name in [("ada@x.com","Ada"), ("grace@x.com","Grace"), ("hedy@x.com","Hedy")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))

print("--- mice: 10 units at 25.00, no serials ---")
mice = pooled.create("Logitech M185 mouse", "Peripheral", 2500, 10, vendor="Logitech")
item = pooled.get(mice)
s = pooled.summary(item)
check("owned", item["quantity"], 10)
check("nothing out yet", s["allocated"], 0)
check("total value is unit price x owned", s["total_value"], 25000)
check("all available", s["available"], 10)

check("handing out one succeeds", pooled.assign(mice, "ada@x.com", 1), None)
check("handing out three more to someone else", pooled.assign(mice, "grace@x.com", 3), None)
s = pooled.summary(pooled.get(mice))
check("four out", s["allocated"], 4)
check("six left", s["available"], 6)
check("cost in people's hands", s["allocated_value"], 10000)
check("two holders", len(s["holders"]), 2)

print("\n--- handing out more to the same person raises the count, not the rows ---")
check("second unit to Ada", pooled.assign(mice, "ada@x.com", 1), None)
check("still two holders", len(pooled.summary(pooled.get(mice))["holders"]), 2)
check("Ada now holds 2",
      db.q1("SELECT quantity FROM pooled_allocations WHERE item_id=? AND upn='ada@x.com'",
            (mice,))["quantity"], 2)

print("\n--- cost follows the units ---")
ada = [k for k in pooled.for_user("ada@x.com")][0]
check("Ada carries 2 x 25.00", ada["cost"], 5000)
grace = [k for k in pooled.for_user("grace@x.com")][0]
check("Grace carries 3 x 25.00", grace["cost"], 7500)

print("\n--- you cannot hand out what you do not have ---")
problem = pooled.assign(mice, "hedy@x.com", 99)
check("refused", problem is not None, True)
check("says how many are available", "5 unit(s) available" in problem, True)
check("nothing changed", pooled.summary(pooled.get(mice))["allocated"], 5)

print("\n--- taking back ---")
check("take one back from Grace", pooled.take_back(mice, "grace@x.com", 1), None)
check("Grace holds 2",
      db.q1("SELECT quantity FROM pooled_allocations WHERE item_id=? AND upn='grace@x.com'",
            (mice,))["quantity"], 2)
check("take back all of Grace's", pooled.take_back(mice, "grace@x.com"), None)
check("Grace holds none",
      db.q1("SELECT COUNT(*) c FROM pooled_allocations WHERE item_id=? AND upn='grace@x.com'",
            (mice,))["c"], 0)
check("taking back from someone with none is refused",
      pooled.take_back(mice, "grace@x.com") is not None, True)

print("\n--- quantity owned cannot drop below what is handed out ---")
s = pooled.summary(pooled.get(mice))
problem = pooled.update(mice, "Logitech M185 mouse", "Peripheral", 2500, 1, None, None)
check("refused", problem is not None, True)
check("mentions how many are out", "2 unit(s) are already handed out" in problem, True)
check("quantity unchanged", pooled.get(mice)["quantity"], 10)
check("raising it is fine",
      pooled.update(mice, "Logitech M185 mouse", "Peripheral", 2500, 20, None, None), None)
check("now 20 owned", pooled.get(mice)["quantity"], 20)

print("\n--- the JetBrains case: licences bought once, handed to several people ---")
jb = pooled.create("JetBrains All Products Pack", "Software", 77900, 4, vendor="JetBrains")
for upn in ("ada@x.com", "grace@x.com", "hedy@x.com"):
    check(f"seat to {upn}", pooled.assign(jb, upn, 1), None)
sj = pooled.summary(pooled.get(jb))
check("three of four seats used", sj["allocated"], 3)
check("one spare", sj["available"], 1)
check("purchase value", sj["total_value"], 311600)
check("value in use", sj["allocated_value"], 233700)
check("a fourth is fine", pooled.assign(jb, "ada@x.com", 1), None)
check("a fifth is refused", pooled.assign(jb, "grace@x.com", 1) is not None, True)

print("\n--- org totals ---")
t = pooled.totals()
# "items" would be shadowed by dict.items in a template, so the key is
# deliberately named item_count.
check("item count key is template-safe", "items" in t, False)
check("two items", t["item_count"], 2)
check("units owned", t["units"], 24)          # 20 mice + 4 licences
check("total value", t["value"], 20*2500 + 4*77900)
check("units out", t["allocated_units"], 2 + 4)   # Ada 2 mice; 4 JetBrains seats
check("spare units", t["spare_units"], 24 - 6)

print("\n--- per-person rollup matches the item view ---")
ada_total = sum(k["cost"] for k in pooled.for_user("ada@x.com"))
check("Ada: 2 mice + 2 JetBrains", ada_total, 2*2500 + 2*77900)

print("\n--- deleting an item takes its allocations with it ---")
pooled.delete(jb)
check("allocations gone",
      db.q1("SELECT COUNT(*) c FROM pooled_allocations WHERE item_id=?", (jb,))["c"], 0)
check("the other item is untouched", pooled.get(mice)["quantity"], 20)

print("\n--- a departing person's allocations go with the user row ---")
db.execute("DELETE FROM users WHERE upn='ada@x.com'")
check("their allocations are removed",
      db.q1("SELECT COUNT(*) c FROM pooled_allocations WHERE upn='ada@x.com'")["c"], 0)
check("units return to the pool", pooled.summary(pooled.get(mice))["available"], 20)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
