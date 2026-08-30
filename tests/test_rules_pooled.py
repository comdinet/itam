"""Rules must see pooled assets, not just the individually tracked ones.

Before Stock was folded into Assets a rule could only grant an asset row, so
"everyone gets a mouse" was unanswerable for the mice you actually own - they
were counted in a pool the rule could not read. These check that both kinds
count towards a rule, and that applying one draws from both.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, pooled, rules
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

GROUP = "g-design"
db.execute("INSERT INTO groups (id, display_name, member_count) VALUES (?,?,3)",
           (GROUP, "Design"))
for upn, name in [("ada@x.com", "Ada"), ("grace@x.com", "Grace"), ("hedy@x.com", "Hedy")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))
    db.execute("INSERT INTO group_members (group_id, upn) VALUES (?,?)", (GROUP, upn))

print("--- a pooled item is offered to the rule form ---")
mice = pooled.create("Logitech MX Master 3S", "Peripheral", 10900, 3)
by_cat = rules.assets_by_category()
check("pooled name appears under its category",
      "Logitech MX Master 3S" in by_cat.get("Peripheral", []), True)

print("\n--- a rule naming a pooled item grants from the pool ---")
rid = rules.create("Everyone gets a mouse", GROUP, "asset", 1,
                   category="Peripheral", asset_name="Logitech MX Master 3S")
s = rules.summarise(rules.get(rid))
check("three people short", s["short"], 3)
check("three units available", s["available"], 3)

result = rules.apply(rules.get(rid))
check("all three served", result["granted"], 3)
check("nobody left short", result["shortfall"], [])
check("pool fully handed out", pooled.summary(pooled.get(mice))["allocated"], 3)
check("Ada holds one", pooled.held_by("ada@x.com", "Peripheral", "Logitech MX Master 3S"), 1)

s = rules.summarise(rules.get(rid))
check("rule now reads compliant", s["short"], 0)
check("and nothing spare", s["available"], 0)

print("\n--- holdings count across both kinds ---")
# One monitor with a serial, two in a pool: three units for two people wanting
# two each. The individual one goes first, then the pool covers the rest.
db.execute("INSERT INTO assets (name, category, cost_cents) VALUES ('LG 32UN880','Monitor',59900)")
pool_mon = pooled.create("LG 32UN880", "Monitor", 59900, 2)
rid2 = rules.create("Design gets 2 monitors", GROUP, "asset", 2, category="Monitor")
db.execute("DELETE FROM group_members WHERE group_id = ? AND upn = 'hedy@x.com'", (GROUP,))
s = rules.summarise(rules.get(rid2))
check("two members now", s["members"], 2)
check("three monitors available in total", s["available"], 3)

result = rules.apply(rules.get(rid2))
check("three handed out", result["granted"], 3)
check("one person still short by one", result["shortfall"],
      [{"upn": "grace@x.com", "display_name": "Grace", "still_short": 1}])
check("the serialled monitor went out",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NOT NULL")["c"], 1)
check("and both pooled units too", pooled.summary(pooled.get(pool_mon))["allocated"], 2)

print("\n--- a person's pooled units satisfy the rule without re-granting ---")
ada_has = rules._held("ada@x.com", "Monitor", None)
check("Ada counts two monitors, one of each kind", ada_has, 2)

print("\nFAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
