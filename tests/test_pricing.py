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

def asset(name, category="Laptop", cost=0, serial=None):
    return db.execute(
        "INSERT INTO assets (name, category, cost_cents, serial) VALUES (?,?,?,?)",
        (name, category, cost, serial))

def device(did, asset_id, model, manufacturer="Apple", os_name="macOS", dev_name=None):
    db.execute("""INSERT INTO devices (id, device_name, model, manufacturer, os,
                                       asset_id, synced_at)
                  VALUES (?,?,?,?,?,?,'2026-08-27T00:00:00+00:00')""",
               (did, dev_name or did, model, manufacturer, os_name, asset_id))

def attr(did, name, value):
    db.execute("""INSERT INTO device_attributes (device_id, name, value, collected_at)
                  VALUES (?,?,?,'2026-08-27T00:00:00+00:00')""", (did, name, value))

# --- the Dell example: price by model alone -----------------------------
d1 = asset("Latitude 5450", serial="DELL-1")
d2 = asset("Latitude 5450", serial="DELL-2", cost=59900)
d3 = asset("Latitude 7450", serial="DELL-3")
device("dev1", d1, "Latitude 5450", "Dell", "Windows")
device("dev2", d2, "Latitude 5450", "Dell", "Windows")
device("dev3", d3, "Latitude 7450", "Dell", "Windows")

g = pricing.create("Latitude 5450", 59900)
pricing.add_criterion(g, "model", "eq", "Latitude 5450")
group = pricing.get(g)
s = pricing.summary(group)
print("--- model is exactly 'Latitude 5450' -> $599 ---")
check("matched only the 5450s", s["matched"], 2)
check("one already at the price", s["at_price"], 1)
check("one would change", s["to_change"], 1)
check("the 7450 is excluded",
      all(a["serial"] != "DELL-3" for a in s["assets"]), True)
r = pricing.apply(group)
check("applied to one", r["changed"], 1)
check("both now 59900",
      [a["cost_cents"] for a in pricing.matching_assets(g)], [59900, 59900])
check("the 7450 untouched", db.q1("SELECT cost_cents FROM assets WHERE serial='DELL-3'")["cost_cents"], 0)
check("re-applying changes nothing", pricing.apply(group)["changed"], 0)

# --- the Mac example: model plus custom attributes ----------------------
print("\n--- MacBook Air 13 M4 / 16 GB / 512 GB ---")
m1 = asset("MacBook Air 13", serial="MBA-1")
m2 = asset("MacBook Air 13", serial="MBA-2")
m3 = asset("MacBook Air 13", serial="MBA-3")      # 8 GB, must not match
m4 = asset("MacBook Pro 14", serial="MBP-1")      # wrong model
device("mac1", m1, "MacBook Air (13-inch, M4, 2025)")
device("mac2", m2, "MacBook Air (13-inch, M4, 2025)")
device("mac3", m3, "MacBook Air (13-inch, M4, 2025)")
device("mac4", m4, "MacBook Pro (14-inch, M4, 2025)")
attr("mac1", "CPU and RAM", "Apple M4 / 16 GB"); attr("mac1", "Disk", "512 GB")
attr("mac2", "CPU and RAM", "Apple M4 / 16 GB"); attr("mac2", "Disk", "512 GB")
attr("mac3", "CPU and RAM", "Apple M4 / 8 GB");  attr("mac3", "Disk", "256 GB")
attr("mac4", "CPU and RAM", "Apple M4 Pro / 16 GB"); attr("mac4", "Disk", "512 GB")

gm = pricing.create("MBA 13 M4 16/512", 129900)
pricing.add_criterion(gm, "model", "contains", "MacBook Air")
pricing.add_criterion(gm, "attribute", "contains", "16 GB", attr_name="CPU and RAM")
pricing.add_criterion(gm, "attribute", "eq", "512 GB", attr_name="Disk")
gmg = pricing.get(gm)
sm = pricing.summary(gmg)
check("matched exactly the two 16/512 Airs", sorted(a["serial"] for a in sm["assets"]),
      ["MBA-1", "MBA-2"])
check("the 8 GB Air excluded", all(a["serial"] != "MBA-3" for a in sm["assets"]), True)
check("the Pro excluded", all(a["serial"] != "MBP-1" for a in sm["assets"]), True)
check("applying prices both", pricing.apply(gmg)["changed"], 2)
check("prices written", [a["cost_cents"] for a in pricing.matching_assets(gm)],
      [129900, 129900])

print("\n--- attribute name is matched case-insensitively ---")
g2 = pricing.create("case", 1)
pricing.add_criterion(g2, "attribute", "contains", "16 gb", attr_name="cpu AND ram")
check("still matches", pricing.summary(pricing.get(g2))["matched"], 3)  # 2 Airs + 1 Pro

print("\n--- a group with no criteria matches nothing ---")
empty = pricing.create("empty", 99900)
check("no assets matched", pricing.summary(pricing.get(empty))["matched"], 0)
check("applying changes nothing", pricing.apply(pricing.get(empty))["changed"], 0)
total_before = db.q1("SELECT SUM(cost_cents) s FROM assets")["s"]
pricing.apply(pricing.get(empty))
check("estate value untouched by an empty group",
      db.q1("SELECT SUM(cost_cents) s FROM assets")["s"], total_before)

print("\n--- assets with no linked device fall back to the asset name ---")
lone = asset("Latitude 5450", serial="NO-DEVICE")
check("unlinked asset still matched by model",
      any(a["serial"] == "NO-DEVICE" for a in pricing.matching_assets(g)), True)

print("\n--- other fields ---")
g3 = pricing.create("all Dell", 1)
pricing.add_criterion(g3, "manufacturer", "eq", "Dell")
check("manufacturer criterion", pricing.summary(pricing.get(g3))["matched"], 3)
g4 = pricing.create("windows", 1)
pricing.add_criterion(g4, "os", "starts", "Win")
check("os starts-with criterion", pricing.summary(pricing.get(g4))["matched"], 3)
g5 = pricing.create("cat", 1)
pricing.add_criterion(g5, "category", "eq", "Laptop")
laptops = db.q1("SELECT COUNT(*) c FROM assets WHERE category='Laptop'")["c"]
check("category criterion counts every laptop",
      pricing.summary(pricing.get(g5))["matched"], laptops)

print("\n--- a value with SQL in it is data, not code ---")
g6 = pricing.create("injection", 1)
pricing.add_criterion(g6, "model", "eq", "x'; DROP TABLE assets; --")
check("no match, and nothing broke", pricing.summary(pricing.get(g6))["matched"], 0)
check("assets table intact", db.q1("SELECT COUNT(*) c FROM assets")["c"], laptops)

print("\n--- bad field or operator is refused ---")
for field, op in (("nonsense", "eq"), ("model", "regex"), ("attribute", "eq")):
    try:
        pricing.add_criterion(g6, field, op, "v")
        fails.append(f"accepted bad criterion {field}/{op}")
        print(f"FAIL  accepted {field}/{op}")
    except ValueError:
        print(f"PASS  refused {field}/{op}")

print("\n--- price_for_asset, used when creating an asset from Intune ---")
# price_for_asset now returns the price with its currency, so an asset created
# from Intune inherits a figure that knows what it is denominated in.
hit = pricing.price_for_asset(m1)
check("a covered asset gets its group price", hit["price_cents"], 129900)
check("and the group's currency travels with it", "currency" in hit, True)
other = pricing.price_for_asset(db.q1("SELECT id FROM assets WHERE serial='MBP-1'")["id"])
check("an uncovered asset gets nothing, or only a trivial group",
      other is None or other["price_cents"] in (1,), True)

print("\n--- deleting a group leaves prices alone ---")
before = db.q1("SELECT cost_cents FROM assets WHERE serial='MBA-1'")["cost_cents"]
pricing.delete(gm)
check("criteria cascade away", db.q1("SELECT COUNT(*) c FROM price_group_criteria WHERE group_id=?", (gm,))["c"], 0)
check("asset price kept", db.q1("SELECT cost_cents FROM assets WHERE serial='MBA-1'")["cost_cents"], before)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
