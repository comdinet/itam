"""Entitlement rules against the counted-asset model.

A rule hands out counted items and licence seats. Neither draws against a
shelf, so applying one closes every gap in a single pass - there is no
"waiting for stock". Serial-tracked machines are deliberately out of reach: a
rule cannot conjure a serial number.
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

def held(upn):
    return pooled.held_by(upn, "Monitor", "Dell U2723QE")

# Design group: 3 people, one of them a disabled account.
db.execute("INSERT INTO groups (id, display_name, member_count) VALUES ('g-design','Design',3)")
for upn, name in [("hedy@x.com", "Hedy"), ("ada@x.com", "Ada"), ("gone@x.com", "Departed")]:
    db.execute("INSERT INTO users (upn, display_name, account_enabled, source) VALUES (?,?,?,'entra')",
               (upn, name, 0 if upn == "gone@x.com" else 1))
    db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-design',?)", (upn,))

mon = pooled.create("Dell U2723QE", "Monitor", 59900)
pooled.assign(mon, "hedy@x.com", 1)         # Hedy already has one

# A serial-tracked laptop that no rule may touch.
db.execute("INSERT INTO assets (name,category,cost_cents,serial) "
           "VALUES ('MacBook Air 13 M4','Laptop',129900,'SN-1')")

rid = rules.create("Designers get two monitors", "g-design", "asset", 2,
                   category="Monitor", asset_name="Dell U2723QE")
rule = rules.get(rid)
s = rules.summarise(rule)
print("--- 'everyone in Design gets 2 monitors' ---")
check("members", s["members"], 3)
check("compliant", s["compliant"], 0)
check("short", s["short"], 3)
check("total to hand out", s["needed"], 5)     # Hedy 1 + Ada 2 + Departed 2
check("no availability ceiling is reported", "available" in s, False)

print("\n--- apply closes every gap in one pass ---")
r = rules.apply(rule)
check("granted all five", r["granted"], 5)
check("nobody left short", r["shortfall"], [])
check("Hedy topped up to two", held("hedy@x.com"), 2)
check("Ada has two", held("ada@x.com"), 2)
check("even the disabled account is served", held("gone@x.com"), 2)
check("the serial-tracked laptop was not touched",
      db.q1("SELECT assigned_upn FROM assets WHERE serial='SN-1'")["assigned_upn"], None)
check("no asset rows were invented",
      db.q1("SELECT COUNT(*) c FROM assets WHERE category='Monitor'")["c"], 0)

s2 = rules.summarise(rules.get(rid))
check("everyone compliant", s2["compliant"], 3)
check("nothing left to hand out", s2["needed"], 0)

print("\n--- re-applying does nothing ---")
check("re-apply grants nothing", rules.apply(rules.get(rid))["granted"], 0)
check("units unchanged", pooled.summary(pooled.get(mon))["assigned"], 6)

print("\n--- returned units are re-used before new ones are counted ---")
pooled.take_back(mon, "gone@x.com")            # the leaver hands both back
check("two went on the shelf", pooled.get(mon)["spare"], 2)
check("total owned is unchanged by a return",
      pooled.summary(pooled.get(mon))["owned"], 6)
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('tal@x.com','Tal','entra')")
db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-design','tal@x.com')")
r = rules.apply(rules.get(rid))
check("the newcomer is served", r["granted"], 2)
check("from the shelf", pooled.get(mon)["spare"], 0)
check("so nothing new was bought", pooled.summary(pooled.get(mon))["owned"], 6)

print("\n--- over-provisioning is reported, not corrected ---")
pooled.assign(mon, "ada@x.com", 1)
s4 = rules.summarise(rules.get(rid))
check("over counted", s4["over"], 1)
check("apply does not take it away", rules.apply(rules.get(rid))["granted"], 0)
check("Ada still has three", held("ada@x.com"), 3)

print("\n--- a rule pointing at a deleted item says so ---")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('rio@x.com','Rio','entra')")
db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-design','rio@x.com')")
pooled.delete(mon)
r = rules.apply(rules.get(rid))
check("granted nothing", r["granted"], 0)
check("and names who missed out", [u["upn"] for u in r["shortfall"]], ["rio@x.com"])
check("without marking them served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='rio@x.com'", (rid,))),
      False)

print("\n--- serial-tracked items are not offered to rules at all ---")
db.execute("INSERT INTO assets (name,category,cost_cents,serial) "
           "VALUES ('MacBook Air 13 M4','Laptop',129900,'SN-2')")
check("the laptop is absent from the picker",
      "Laptop" in rules.assets_by_category(), False)

print("\n--- subscription rule ---")
sid = db.execute("INSERT INTO subscriptions (name, vendor, monthly_cost_cents) VALUES ('Figma','Figma',1500)")
rid2 = rules.create("Designers get Figma", "g-design", "subscription", 5, subscription_id=sid)
check("licence quantity clamped to 1", rules.get(rid2)["quantity"], 1)
sub_rule = rules.get(rid2)
check("all short initially", rules.summarise(sub_rule)["short"], 5)
rs = rules.apply(sub_rule)
check("seats granted", rs["granted"], 5)
check("no shortfall for licences", rs["shortfall"], [])
check("compliant now", rules.summarise(rules.get(rid2))["compliant"], 5)
check("seats in db",
      db.q1("SELECT COUNT(*) c FROM subscription_seats WHERE subscription_id=?", (sid,))["c"], 5)
check("re-apply is idempotent", rules.apply(rules.get(rid2))["granted"], 0)

print("\n--- housekeeping ---")
rules.set_active(rid2, False)
paused = [o for o in rules.compliance_overview() if o["rule"]["id"] == rid2][0]
check("paused rule has no summary", paused["summary"], None)

db.execute("DELETE FROM groups WHERE id='g-design'")
check("rules cascade with the group", db.q1("SELECT COUNT(*) c FROM rules")["c"], 0)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
