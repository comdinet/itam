"""Pricing narrowed by who holds the device, and the suggestions that feed it.

Price follows the purchase, and the purchase follows the region. The same
laptop model bought in Israel and in the UK carries two prices, so a shekel
group has to be able to say "not the UK fleet" - otherwise applying it silently
rewrites a pound purchase into shekels.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, pricing
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

def group(gid, name):
    db.execute("INSERT INTO groups (id, display_name, member_count) VALUES (?,?,0)", (gid, name))

def user(upn, name, *groups):
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))
    for gid in groups:
        db.execute("INSERT INTO group_members (group_id, upn) VALUES (?,?)", (gid, upn))

def asset(name, upn=None, cost=0, serial=None, currency="USD"):
    aid = db.execute(
        "INSERT INTO assets (name, category, cost_cents, serial, assigned_upn, "
        "currency, rate_micro) VALUES (?,'Laptop',?,?,?,?,1000000)",
        (name, cost, serial, upn, currency))
    db.execute("""INSERT INTO devices (id, device_name, model, manufacturer, os,
                                       asset_id, synced_at)
                  VALUES (?,?,?,'Dell','Windows',?,'2026-08-27T00:00:00+00:00')""",
               (serial, serial, name, aid))
    return aid

group("g-il", "Israel"); group("g-uk", "United Kingdom"); group("g-cse", "CSE")
user("yael@x.com", "Yael", "g-il")
user("noa@x.com", "Noa", "g-il", "g-cse")
user("ollie@x.com", "Ollie", "g-uk")

il1 = asset("Latitude 5450", "yael@x.com", serial="IL-1")
il2 = asset("Latitude 5450", "noa@x.com", serial="IL-2")
uk1 = asset("Latitude 5450", "ollie@x.com", cost=48000, serial="UK-1", currency="GBP")
spare = asset("Latitude 5450", None, serial="SPARE-1")

pg = pricing.create("Latitude 5450 (Israel)", 221000, currency="ILS", rate_micro=335683)
pricing.add_criterion(pg, "model", "eq", "Latitude 5450")

print("--- without a condition it covers the UK machine too ---")
check("all four match", pricing.summary(pricing.get(pg))["matched"], 4)

print("\n--- your case: not devices held by the UK group ---")
check("adding the exclusion", pricing.add_group(pg, "g-uk", "exclude"), None)
matched = {a["serial"] for a in pricing.matching_assets(pg)}
check("the UK laptop drops out", "UK-1" in matched, False)
check("the Israeli ones stay", {"IL-1", "IL-2"} <= matched, True)
check("and the unassigned one stays, having no holder", "SPARE-1" in matched, True)
check("three left", len(matched), 3)

print("\n--- applying leaves the UK purchase alone ---")
before = db.q1("SELECT cost_cents, currency FROM assets WHERE id = ?", (uk1,))
r = pricing.apply(pricing.get(pg))
after = db.q1("SELECT cost_cents, currency FROM assets WHERE id = ?", (uk1,))
check("three repriced", r["changed"], 3)
check("the UK machine kept its pounds",
      (after["cost_cents"], after["currency"]),
      (before["cost_cents"], before["currency"]))
check("an Israeli one took the shekel price",
      db.q1("SELECT cost_cents, currency, rate_micro FROM assets WHERE id = ?", (il1,))["currency"],
      "ILS")

print("\n--- 'only devices held by' is the other way round ---")
pg2 = pricing.create("Latitude 5450 (UK)", 48000, currency="GBP", rate_micro=1358006)
pricing.add_criterion(pg2, "model", "eq", "Latitude 5450")
pricing.add_group(pg2, "g-uk", "include")
matched = {a["serial"] for a in pricing.matching_assets(pg2)}
check("only the UK machine", matched, {"UK-1"})
check("the unassigned one is left out, having no holder", "SPARE-1" in matched, False)

print("\n--- excluding beats including ---")
pricing.add_group(pg2, "g-il", "include")
check("Israel joins", len(pricing.matching_assets(pg2)), 3)
pricing.add_group(pg2, "g-cse", "exclude")
matched = {a["serial"] for a in pricing.matching_assets(pg2)}
check("but Noa's CSE membership removes hers", "IL-2" in matched, False)
check("two left", matched, {"UK-1", "IL-1"})

print("\n--- a condition can never make an empty group match ---")
empty = pricing.create("No spec", 100)
pricing.add_group(empty, "g-il", "include")
check("still matches nothing", pricing.matching_assets(empty), [])

print("\n--- a device created from Intune is auto-priced by its holder's region ---")
# Same model, same criteria, two groups: the holder decides which price lands.
fresh = asset("Latitude 5450", "yael@x.com", serial="IL-3")
check("Yael's is priced in shekels",
      (pricing.price_for_asset(fresh)["currency"],
       pricing.price_for_asset(fresh)["price_cents"]), ("ILS", 221000))
uk_fresh = asset("Latitude 5450", "ollie@x.com", serial="UK-2")
check("Ollie's is priced in pounds",
      (pricing.price_for_asset(uk_fresh)["currency"],
       pricing.price_for_asset(uk_fresh)["price_cents"]), ("GBP", 48000))
orphan = asset("Latitude 5450", None, serial="NOBODY-1")
check("an unassigned one takes the group with no 'only' condition",
      pricing.price_for_asset(orphan)["currency"], "ILS")

# Deliberately last: this deletes the UK Entra group, which cascades and takes
# the exclusion above with it.
print("\n--- conditions are described, removable, and cascade ---")
check("described for the page",
      sorted(pricing.describe_condition(c) for c in pricing.group_conditions(pg2)),
      ["Not devices held by CSE", "Only devices held by Israel",
       "Only devices held by United Kingdom"])
pricing.remove_group(pg2, "g-cse", "exclude")
check("removed", len(pricing.group_conditions(pg2)), 2)
check("an unknown group is refused", pricing.add_group(pg2, "nope", "exclude") is not None, True)
check("an unknown mode is refused", pricing.add_group(pg2, "g-il", "maybe") is not None, True)
pricing.delete(pg2)
check("deleting the price group takes its conditions",
      db.q1("SELECT COUNT(*) c FROM price_group_groups WHERE group_id = ?", (pg2,))["c"], 0)
db.execute("DELETE FROM groups WHERE id = 'g-uk'")
check("deleting the Entra group takes the condition with it",
      db.q1("SELECT COUNT(*) c FROM price_group_groups WHERE entra_id = 'g-uk'")["c"], 0)

print("\n--- value suggestions ---")
db.execute("""INSERT INTO device_attributes (device_id, name, value, collected_at)
              VALUES ('IL-1','CPU and RAM','Apple M4 / 16GB','2026-08-27T00:00:00+00:00')""")
db.execute("""INSERT INTO device_attributes (device_id, name, value, collected_at)
              VALUES ('IL-2','CPU and RAM','Apple M4 / 24GB','2026-08-27T00:00:00+00:00')""")
db.execute("""INSERT INTO device_attributes (device_id, name, value, collected_at)
              VALUES ('UK-1','Disk','512GB','2026-08-27T00:00:00+00:00')""")
vals = pricing.attribute_values()
check("values grouped by attribute name",
      vals["CPU and RAM"], ["Apple M4 / 16GB", "Apple M4 / 24GB"])
check("a second attribute is separate", vals["Disk"], ["512GB"])
check("attribute names still listed on their own",
      pricing.attribute_names(), ["CPU and RAM", "Disk"])

fv = pricing.field_values()
check("models still suggested", "Latitude 5450" in fv["model"], True)
check("manufacturers too", fv["manufacturer"], ["Dell"])
check("and operating systems", fv["os"], ["Windows"])
check("categories come from the asset categories", "Laptop" in fv["category"], True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
