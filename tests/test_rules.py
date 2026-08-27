import os, tempfile, sys, tempfile, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, rules
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

# Design group: 3 people
db.execute("INSERT INTO groups (id, display_name, member_count) VALUES ('g-design','Design',3)")
for upn, name in [("hedy@x.com","Hedy"), ("ada@x.com","Ada"), ("gone@x.com","Departed")]:
    db.execute("INSERT INTO users (upn, display_name, account_enabled, source) VALUES (?,?,?,'entra')",
               (upn, name, 0 if upn == "gone@x.com" else 1))
    db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-design',?)", (upn,))

# Hedy already has one monitor; two spare monitors in stock
db.execute("INSERT INTO assets (name,category,cost_cents,assigned_upn) VALUES ('Dell U27','Monitor',59900,'hedy@x.com')")
for i in range(2):
    db.execute("INSERT INTO assets (name,category,cost_cents) VALUES (?,'Monitor',59900)", (f"Spare Monitor {i+1}",))
# an unrelated spare that must not be touched
db.execute("INSERT INTO assets (name,category,cost_cents) VALUES ('Spare Laptop','Laptop',200000)")

rid = rules.create("Designers get two monitors", "g-design", "asset", 2, category="Monitor")
rule = rules.get(rid)
s = rules.summarise(rule)
print("--- 'everyone in Design gets 2 monitors' ---")
check("members", s["members"], 3)
check("compliant", s["compliant"], 0)
check("short", s["short"], 3)
check("total needed", s["needed"], 5)          # Hedy 1 + Ada 2 + Departed 2
check("spares available", s["available"], 2)

r = rules.apply(rule)
print("\n--- apply with only 2 spares ---")
check("granted exactly the spares", r["granted"], 2)
check("shortfall reported", len(r["shortfall"]) > 0, True)
check("no assets invented", db.q1("SELECT COUNT(*) c FROM assets WHERE category='Monitor'")["c"], 3)
check("no monitors left spare",
      db.q1("SELECT COUNT(*) c FROM assets WHERE category='Monitor' AND assigned_upn IS NULL")["c"], 0)
check("unrelated laptop untouched",
      db.q1("SELECT assigned_upn FROM assets WHERE name='Spare Laptop'")["assigned_upn"], None)
# Served alphabetically: Ada (short 2) consumes both spares before Hedy (short 1).
check("ada filled first, to her full target",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='ada@x.com' AND category='Monitor'")["c"], 2)
check("hedy untouched, no spares left",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='hedy@x.com' AND category='Monitor'")["c"], 1)
check("shortfall names the two who missed out", sorted(u["upn"] for u in r["shortfall"]),
      ["gone@x.com", "hedy@x.com"])
check("shortfall counts are per person, not run-wide",
      sorted(u["still_short"] for u in r["shortfall"]), [1, 2])

s2 = rules.summarise(rules.get(rid))
check("compliant after apply", s2["compliant"], 1)
check("still needed", s2["needed"], 3)

# add stock and re-apply: should finish the job
for i in range(3):
    db.execute("INSERT INTO assets (name,category,cost_cents) VALUES (?,'Monitor',59900)", (f"New Monitor {i+1}",))
r2 = rules.apply(rules.get(rid))
s3 = rules.summarise(rules.get(rid))
print("\n--- restock and re-apply ---")
check("granted the remaining 3", r2["granted"], 3)
check("no shortfall now", r2["shortfall"], [])
check("everyone compliant", s3["compliant"], 3)
check("nothing needed", s3["needed"], 0)

# applying again is a no-op
r3 = rules.apply(rules.get(rid))
check("re-apply grants nothing", r3["granted"], 0)
check("no over-assignment",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NOT NULL AND category='Monitor'")["c"], 6)

# over-provisioning is reported, not corrected
db.execute("INSERT INTO assets (name,category,cost_cents,assigned_upn) VALUES ('Extra','Monitor',59900,'ada@x.com')")
s4 = rules.summarise(rules.get(rid))
print("\n--- over-provisioned ---")
check("over counted", s4["over"], 1)
check("apply does not remove the extra", rules.apply(rules.get(rid))["granted"], 0)
check("extra still assigned",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='ada@x.com' AND category='Monitor'")["c"], 3)

# subscription rule
sid = db.execute("INSERT INTO subscriptions (name, vendor, monthly_cost_cents) VALUES ('Figma','Figma',1500)")
rid2 = rules.create("Designers get Figma", "g-design", "subscription", 5, subscription_id=sid)
check("licence quantity clamped to 1", rules.get(rid2)["quantity"], 1)
sub_rule = rules.get(rid2)
print("\n--- subscription rule ---")
check("all short initially", rules.summarise(sub_rule)["short"], 3)
rs = rules.apply(sub_rule)
check("seats granted", rs["granted"], 3)
check("no shortfall for licences", rs["shortfall"], [])
check("compliant now", rules.summarise(rules.get(rid2))["compliant"], 3)
check("seats in db", db.q1("SELECT COUNT(*) c FROM subscription_seats WHERE subscription_id=?", (sid,))["c"], 3)
check("re-apply is idempotent", rules.apply(rules.get(rid2))["granted"], 0)

# paused rules are excluded from the overview summary
rules.set_active(rid2, False)
paused = [o for o in rules.compliance_overview() if o["rule"]["id"] == rid2][0]
check("paused rule has no summary", paused["summary"], None)

# deleting a group removes its rules
db.execute("DELETE FROM groups WHERE id='g-design'")
check("rules cascade with the group", db.q1("SELECT COUNT(*) c FROM rules")["c"], 0)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
