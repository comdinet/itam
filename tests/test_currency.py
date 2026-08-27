import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, fx, stock, pricing
db.init_db(); fx.ensure_base()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

print("--- the reporting currency is always present at exactly 1 ---")
check("USD seeded", bool(fx.get("USD")), True)
check("rate is 1", fx.rate_for("USD"), fx.MICRO)
check("cannot be deleted", fx.delete("USD") is not None, True)

# rates as published 2026-08-27
fx.add("ILS", "₪", "Israeli new shekel", 335683, "boi", "test", "2026-08-27")
fx.add("EUR", "€", "Euro", 1164183, "boi", "test", "2026-08-27")
fx.add("GBP", "£", "Pound sterling", 1358006, "boi", "test", "2026-08-27")

print("\n--- conversion is integer, half-up, and matches hand arithmetic ---")
check("11,900.00 ILS", fx.to_reporting(1190000, 335683), 399463)     # 3,994.63
check("779.00 EUR", fx.to_reporting(77900, 1164183), 90690)          # 906.90
check("16,800.00 GBP", fx.to_reporting(1680000, 1358006), 2281450)   # 22,814.50
check("USD unchanged", fx.to_reporting(249900, fx.MICRO), 249900)
check("zero stays zero", fx.to_reporting(0, 335683), 0)
check("missing rate treated as 1", fx.to_reporting(1000, None), 1000)
check("rounds half up, not to even", fx.to_reporting(1, 1500000), 2)

print("\n--- parsing and formatting a rate ---")
check("0.3357", fx.parse_rate("0.3357"), 335700)
check("comma decimal", fx.parse_rate("0,3357"), 335700)
check("junk refused", fx.parse_rate("abc"), None)
check("zero refused", fx.parse_rate("0"), None)
check("negative refused", fx.parse_rate("-1"), None)
check("formats without trailing zeros", fx.format_rate(335683), "0.335683")
check("base formats as 1", fx.format_rate(fx.MICRO), "1")

print("\n--- the rate is frozen: changing it later moves nothing already entered ---")
for upn in ("yael@x.com",):
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')",
               (upn, "Yael Bar-On"))
laptop = db.execute(
    """INSERT INTO assets (name, category, cost_cents, currency, rate_micro, serial,
                           assigned_upn, assigned_on)
       VALUES ('MacBook Pro 14','Laptop',1190000,'ILS',?,'C02YAEL1','yael@x.com',date('now'))""",
    (fx.rate_for("ILS"),))
before = db.q1("SELECT " + db.conv("cost_cents", "rate_micro") + " AS rep FROM assets WHERE id=?",
               (laptop,))["rep"]
check("laptop converts to 3,994.63", before, 399463)
fx.add("ILS", "₪", "Israeli new shekel", 250000, "manual", "test")   # shekel crashes
after = db.q1("SELECT " + db.conv("cost_cents", "rate_micro") + " AS rep FROM assets WHERE id=?",
              (laptop,))["rep"]
check("the stored laptop figure is unchanged", after, before)
check("but a new entry would use the new rate", fx.rate_for("ILS"), 250000)
fx.add("ILS", "₪", "Israeli new shekel", 335683, "boi", "test", "2026-08-27")  # restore

print("\n--- history records every approval ---")
check("four ILS entries so far", len(fx.history("ILS")), 3)
h = fx.history("ILS")[0]
check("most recent is the restored rate", h["rate_micro"], 335683)
check("carries who approved it", h["approved_by"], "test")

print("\n--- a currency in use cannot be removed or switched off ---")
check("ILS is in use", fx.in_use("ILS"), True)
check("delete refused", fx.delete("ILS") is not None, True)
check("switch-off refused", fx.set_active("ILS", False) is not None, True)
check("an unused one can go", fx.delete("GBP"), None)
fx.add("GBP", "£", "Pound sterling", 1358006, "boi", "test", "2026-08-27")

print("\n--- Yael: a laptop and mice in shekels, a licence in euros ---")
mice = stock.create("Logitech M185 mouse", "Peripheral", 9500, 10,
                    currency="ILS", rate_micro=fx.rate_for("ILS"))
jb = stock.create("JetBrains All Products Pack", "Software", 77900, 4,
                  currency="EUR", rate_micro=fx.rate_for("EUR"))
stock.assign(mice, "yael@x.com", 2)
stock.assign(jb, "yael@x.com", 1)

from app.main import USER_COSTS
row = db.q1(USER_COSTS + " WHERE u.upn = ?", ("yael@x.com",))
# hand arithmetic: laptop 11,900 ILS -> 3,994.63; 2 mice = 190 ILS -> 63.78;
#                  1 JetBrains 779 EUR -> 906.90
check("assets converted", row["asset_total"], 399463)
check("stock converted", row["stock_total"], 6378 + 90690)
check("one-off total", row["onetime_total"], 399463 + 6378 + 90690)
check("units held", row["stock_units"], 3)
check("currencies listed for the mixed case",
      sorted((row["onetime_currencies"] or "").split(",")), ["EUR", "ILS", "ILS"])

print("\n--- org totals are in the reporting currency ---")
t = stock.totals()
# Computed the same way the app does, rather than by hand: 3,116.00 EUR comes to
# 362,759.42 cents, which rounds DOWN, and a hand-rounded 362,760 was wrong.
expect_value = (fx.to_reporting(10 * 9500, 335683) + fx.to_reporting(4 * 77900, 1164183))
expect_alloc = (fx.to_reporting(2 * 9500, 335683) + fx.to_reporting(1 * 77900, 1164183))
check("stock value converted", t["value"], expect_value)
check("allocated value converted", t["allocated_value"], expect_alloc)
check("and that is 394,649 not 394,650", expect_value, 394649)

print("\n--- a pricing group carries its currency onto the assets it prices ---")
g = pricing.create("Latitude 5450 (Israel)", 221000, currency="ILS",
                   rate_micro=fx.rate_for("ILS"))
pricing.add_criterion(g, "model", "eq", "Latitude 5450")
for n in range(2):
    db.execute("""INSERT INTO assets (name, category, cost_cents, currency, rate_micro, serial)
                  VALUES ('Latitude 5450','Laptop',0,'USD',1000000,?)""", (f"DELL-{n}",))
group = pricing.get(g)
check("both matched", pricing.summary(group)["matched"], 2)
check("applied to both", pricing.apply(group)["changed"], 2)
priced = db.q("SELECT cost_cents, currency, rate_micro FROM assets WHERE serial LIKE 'DELL-%'")
check("amount taken from the group", [p["cost_cents"] for p in priced], [221000, 221000])
check("currency taken from the group", [p["currency"] for p in priced], ["ILS", "ILS"])
check("rate taken from the group", [p["rate_micro"] for p in priced], [335683, 335683])
check("re-applying is a no-op", pricing.apply(pricing.get(g))["changed"], 0)

print("\n--- money is shown with its own symbol ---")
check("shekels", fx.money(1190000, "ILS"), "₪ 11,900.00")
check("euros", fx.money(77900, "EUR"), "€ 779.00")
check("reporting currency", fx.money(249900, "USD"), "$ 2,499.00")

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
