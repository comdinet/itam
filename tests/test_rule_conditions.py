import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, rules
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

def group(gid, name):
    db.execute("INSERT INTO groups (id, display_name, member_count) VALUES (?,?,0)", (gid, name))
def user(upn, name):
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))
def member(gid, upn):
    db.execute("INSERT INTO group_members (group_id, upn) VALUES (?,?)", (gid, upn))

group("g-il", "Israel"); group("g-cse", "CSE"); group("g-contract", "Contractors")
for upn, name in [("yael@x.com","Yael"), ("noa@x.com","Noa"), ("eli@x.com","Eli"),
                  ("dana@x.com","Dana"), ("omer@x.com","Omer")]:
    user(upn, name); 
for upn in ("yael@x.com","noa@x.com","eli@x.com","dana@x.com"):
    member("g-il", upn)
member("g-cse", "eli@x.com")          # in Israel and CSE
member("g-cse", "dana@x.com")         # in Israel and CSE
member("g-contract", "omer@x.com")    # not in Israel at all

print("--- your example: everyone in Israel, except CSE ---")
rid = rules.create("Israel gets two monitors", "g-il", "asset", 2, category="Monitor")
rule = rules.get(rid)
check("Israel alone covers four", sorted(rules.covered_upns(rule)),
      ["dana@x.com", "eli@x.com", "noa@x.com", "yael@x.com"])
check("adding the exclusion", rules.add_group(rid, "g-cse", "exclude"), None)
check("CSE members drop out", sorted(rules.covered_upns(rules.get(rid))),
      ["noa@x.com", "yael@x.com"])
check("evaluate lists only those two",
      sorted(r["upn"] for r in rules.evaluate(rules.get(rid))),
      ["noa@x.com", "yael@x.com"])

print("\n--- an extra include widens it, and exclude still wins ---")
check("also include contractors", rules.add_group(rid, "g-contract", "include"), None)
check("Omer joins", sorted(rules.covered_upns(rules.get(rid))),
      ["noa@x.com", "omer@x.com", "yael@x.com"])
member("g-cse", "omer@x.com")
check("but excluding beats including", sorted(rules.covered_upns(rules.get(rid))),
      ["noa@x.com", "yael@x.com"])
db.execute("DELETE FROM group_members WHERE group_id='g-cse' AND upn='omer@x.com'")
rules.remove_group(rid, "g-contract", "include")
check("condition removed", sorted(rules.covered_upns(rules.get(rid))),
      ["noa@x.com", "yael@x.com"])
check("the rule's own group cannot be added again",
      rules.add_group(rid, "g-il", "include") is not None, True)
check("an unknown group is refused",
      rules.add_group(rid, "no-such-group", "exclude") is not None, True)
check("an unknown mode is refused",
      rules.add_group(rid, "g-cse", "maybe") is not None, True)

print("\n--- exceptions can be set when the rule is created ---")
# The create form posts exclude/include alongside the rule itself, so an
# exception does not require a second trip to the rule's own page.
rid2 = rules.create("Israel, not CSE", "g-il", "asset", 1, category="Monitor")
check("exclusion applied at creation time", rules.add_group(rid2, "g-cse", "exclude"), None)
check("covers the non-CSE members only", sorted(rules.covered_upns(rules.get(rid2))),
      ["noa@x.com", "yael@x.com"])
rules.delete(rid2)

print("\n--- a rule serves each person once ---")
for i in range(4):
    db.execute("INSERT INTO assets (name,category,cost_cents) VALUES (?,'Monitor',59900)",
               (f"Monitor {i+1}",))
rule = rules.get(rid)
s = rules.summarise(rule)
check("two people covered", s["members"], 2)
check("nobody served yet", s["fulfilled"], 0)
check("four needed", s["needed"], 4)
r = rules.apply(rule)
check("granted four", r["granted"], 4)
s = rules.summarise(rules.get(rid))
check("both now recorded as served", s["fulfilled"], 2)
check("nothing outstanding", s["needed"], 0)

print("\n--- and does not serve them again when they hand one back ---")
one = db.q1("SELECT id FROM assets WHERE assigned_upn='yael@x.com' LIMIT 1")["id"]
db.execute("UPDATE assets SET assigned_upn=NULL WHERE id=?", (one,))
check("Yael is down to one monitor",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='yael@x.com'")["c"], 1)
s = rules.summarise(rules.get(rid))
check("she is not counted short", s["short"], 0)
check("nothing needed", s["needed"], 0)
check("applying grants nothing", rules.apply(rules.get(rid))["granted"], 0)
check("she still holds one, not two",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='yael@x.com'")["c"], 1)

print("\n--- a new joiner is served, the served are not ---")
user("tal@x.com", "Tal"); member("g-il", "tal@x.com")
s = rules.summarise(rules.get(rid))
check("three covered now", s["members"], 3)
check("only the newcomer is short", s["short"], 1)
check("needing two", s["needed"], 2)
spare_now = db.q1("SELECT COUNT(*) c FROM assets "
                  "WHERE assigned_upn IS NULL AND category='Monitor'")["c"]
check("only the returned monitor is spare", spare_now, 1)
r = rules.apply(rules.get(rid))
check("granted what existed, not what was wanted", r["granted"], 1)
check("Tal has one",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='tal@x.com'")["c"], 1)
check("Yael untouched",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='yael@x.com'")["c"], 1)

print("\n--- partial service does not finish someone off ---")
check("Tal is not recorded as served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='tal@x.com'", (rid,))),
      False)
check("he is still short one", rules.summarise(rules.get(rid))["needed"], 1)
db.execute("INSERT INTO assets (name,category,cost_cents) VALUES ('Late arrival','Monitor',59900)")
check("restocking completes him", rules.apply(rules.get(rid))["granted"], 1)
check("Tal now has two",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='tal@x.com'")["c"], 2)
check("and is now recorded as served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='tal@x.com'", (rid,))),
      True)

print("\n--- someone already equipped is marked served without being given anything ---")
user("gil@x.com", "Gil"); member("g-il", "gil@x.com")
db.execute("INSERT INTO assets (name,category,cost_cents,assigned_upn) VALUES ('Own','Monitor',0,'gil@x.com')")
db.execute("INSERT INTO assets (name,category,cost_cents,assigned_upn) VALUES ('Own2','Monitor',0,'gil@x.com')")
before = db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NULL AND category='Monitor'")["c"]
rules.apply(rules.get(rid))
check("no spares consumed for him",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NULL AND category='Monitor'")["c"],
      before)
check("but he is recorded as served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='gil@x.com'", (rid,))),
      True)

print("\n--- a shortfall is not recorded, so restocking serves them ---")
user("shir@x.com", "Shir"); member("g-il", "shir@x.com")
check("no spares left",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn IS NULL AND category='Monitor'")["c"], 0)
r = rules.apply(rules.get(rid))
check("granted nothing", r["granted"], 0)
check("named in the shortfall", [u["upn"] for u in r["shortfall"]], ["shir@x.com"])
check("and NOT marked served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='shir@x.com'", (rid,))),
      False)
for i in range(2):
    db.execute("INSERT INTO assets (name,category,cost_cents) VALUES (?,'Monitor',59900)", (f"Restock {i}",))
check("after restocking she is served", rules.apply(rules.get(rid))["granted"], 2)

print("\n--- a mistake can be undone ---")
rules.clear_fulfilment(rid, "yael@x.com")
s = rules.summarise(rules.get(rid))
check("Yael is short again", s["short"], 1)
n = rules.clear_all_fulfilments(rid)
check("clearing everything", n > 0, True)
check("nobody recorded", rules.summarise(rules.get(rid))["fulfilled"], 0)

print("\n--- deleting the rule takes its conditions and records with it ---")
rules.add_group(rid, "g-cse", "exclude")
rules.apply(rules.get(rid))
rules.delete(rid)
check("conditions gone", db.q1("SELECT COUNT(*) c FROM rule_groups WHERE rule_id=?", (rid,))["c"], 0)
check("records gone", db.q1("SELECT COUNT(*) c FROM rule_fulfilments WHERE rule_id=?", (rid,))["c"], 0)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
