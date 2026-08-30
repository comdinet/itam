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

print("\n--- a rule can name one item, not just a category ---")
dell = pooled.create("Dell U2723QE", "Monitor", 59900)
lg = pooled.create("LG UltraFine 32", "Monitor", 89900)
named = rules.create("Dells only", "g-il", "asset", 1,
                     category="Monitor", asset_name="Dell U2723QE")
rules.add_group(named, "g-cse", "exclude")
nr = rules.get(named)
check("the item is stored", nr["asset_name"], "Dell U2723QE")
check("label names the item", rules.grants_label(nr), "Dell U2723QE")
r = rules.apply(nr)
check("both covered people got a Dell", r["granted"], 2)
check("the LG was left alone", pooled.summary(pooled.get(lg))["assigned"], 0)
check("holding a Dell counts only for the Dell rule",
      pooled.held_by("yael@x.com", "Monitor", "Dell U2723QE"), 1)

check("the picker lists counted items, grouped by category",
      sorted(rules.assets_by_category().get("Monitor", [])),
      ["Dell U2723QE", "LG UltraFine 32"])
db.execute("INSERT INTO assets (name,category,cost_cents,serial) "
           "VALUES ('MacBook Air 13 M4','Laptop',129900,'SN-9')")
check("and never serial-tracked machines",
      "Laptop" in rules.assets_by_category(), False)
rules.delete(named)
pooled.delete(dell); pooled.delete(lg)

print("\n--- a rule serves each person once ---")
mon = pooled.create("Dell U2723QE", "Monitor", 59900)
rule = rules.get(rid)                     # Israel except CSE: Noa and Yael
s = rules.summarise(rule)
check("two people covered", s["members"], 2)
check("nobody served yet", s["fulfilled"], 0)
check("four to hand out", s["needed"], 4)
r = rules.apply(rule)
check("granted four", r["granted"], 4)
s = rules.summarise(rules.get(rid))
check("both now recorded as served", s["fulfilled"], 2)
check("nothing outstanding", s["needed"], 0)

print("\n--- and does not serve them again when they hand one back ---")
pooled.take_back(mon, "yael@x.com", 1)
check("Yael is down to one monitor", pooled.held_by("yael@x.com", "Monitor"), 1)
check("and it went on the shelf", pooled.get(mon)["spare"], 1)
s = rules.summarise(rules.get(rid))
check("she is not counted short", s["short"], 0)
check("nothing needed", s["needed"], 0)
check("applying grants nothing", rules.apply(rules.get(rid))["granted"], 0)
check("she still holds one, not two", pooled.held_by("yael@x.com", "Monitor"), 1)

print("\n--- a new joiner is served in full, the served are not ---")
user("tal@x.com", "Tal"); member("g-il", "tal@x.com")
s = rules.summarise(rules.get(rid))
check("three covered now", s["members"], 3)
check("only the newcomer is short", s["short"], 1)
check("needing two", s["needed"], 2)
r = rules.apply(rules.get(rid))
check("served in one go, not limited by the shelf", r["granted"], 2)
check("no shortfall", r["shortfall"], [])
check("Tal has two", pooled.held_by("tal@x.com", "Monitor"), 2)
check("the shelved one was re-used first", pooled.get(mon)["spare"], 0)
check("Yael untouched", pooled.held_by("yael@x.com", "Monitor"), 1)
check("he is recorded as served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='tal@x.com'", (rid,))),
      True)

print("\n--- someone already equipped is marked served without being given anything ---")
user("gil@x.com", "Gil"); member("g-il", "gil@x.com")
pooled.assign(mon, "gil@x.com", 2)
owned_before = pooled.summary(pooled.get(mon))["owned"]
rules.apply(rules.get(rid))
check("nothing extra was handed out",
      pooled.summary(pooled.get(mon))["owned"], owned_before)
check("but he is recorded as served",
      bool(db.q1("SELECT 1 FROM rule_fulfilments WHERE rule_id=? AND upn='gil@x.com'", (rid,))),
      True)

print("\n--- a serial-tracked monitor also counts as having one ---")
user("shir@x.com", "Shir"); member("g-il", "shir@x.com")
db.execute("INSERT INTO assets (name,category,cost_cents,serial,assigned_upn) "
           "VALUES ('Dell U2723QE','Monitor',59900,'SN-M1','shir@x.com')")
s = rules.summarise(rules.get(rid))
check("she counts as holding one", [r_["have"] for r_ in s["rows"]
                                    if r_["upn"] == "shir@x.com"], [1])
r = rules.apply(rules.get(rid))
check("so only the missing one is handed out", r["granted"], 1)
check("and it came from the counted item, not another asset row",
      db.q1("SELECT COUNT(*) c FROM assets WHERE category='Monitor'")["c"], 1)

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
