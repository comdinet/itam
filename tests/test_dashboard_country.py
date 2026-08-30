"""By-currency, filtered to a country, and the Intune holder gap.

"How much do we pay for Israel" is two currencies at once: shekels for the kit,
dollars for the SaaS. The filter follows the holder, which means kit nobody
holds falls out of it - and that has to be said out loud, not left to make the
totals quietly disagree.
"""
import os, re, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "DashTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
from app import db, devices, fx, pooled
db.init_db(); fx.ensure_base()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

fx.add("ILS", "₪", "Israeli new shekel", 335683, "boi", "test", "2026-08-30")
fx.add("GBP", "£", "Pound sterling", 1358006, "boi", "test", "2026-08-30")

def user(upn, country):
    db.execute("INSERT INTO users (upn, display_name, country, source) VALUES (?,?,?,'entra')",
               (upn, upn.split("@")[0].title(), country))

user("yael@x.com", "Israel"); user("noa@x.com", "Israel")
user("ollie@x.com", "United Kingdom")
user("nowhere@x.com", "")

def asset(name, upn, cents, cur, serial):
    return db.execute(
        "INSERT INTO assets (name, category, cost_cents, currency, rate_micro, serial, "
        "assigned_upn) VALUES (?,'Laptop',?,?,?,?,?)",
        (name, cents, cur, fx.rate_for(cur), serial, upn))

asset("MacBook Air", "yael@x.com", 764900, "ILS", "IL-1")
asset("MacBook Air", "noa@x.com", 764900, "ILS", "IL-2")
asset("MacBook Air", "ollie@x.com", 129900, "GBP", "UK-1")
asset("MacBook Air", None, 500000, "ILS", "SPARE-1")          # nobody holds it

mon = pooled.create("Dell U2723QE", "Monitor", 210000, currency="ILS",
                    rate_micro=fx.rate_for("ILS"))
pooled.assign(mon, "yael@x.com", 2)
pooled.assign(mon, "ollie@x.com", 1)
pooled.take_back(mon, "ollie@x.com")                          # onto the shelf

sub = db.execute("INSERT INTO subscriptions (name, monthly_cost_cents, currency, rate_micro) "
                 "VALUES ('Claude AI Premium',15000,'USD',1000000)")
for upn in ("yael@x.com", "noa@x.com", "ollie@x.com"):
    db.execute("INSERT INTO subscription_seats (subscription_id, upn, assigned_on) "
               "VALUES (?,?,'2026-08-30')", (sub, upn))

def by_code(rows):
    return {r["code"]: r for r in rows}

print("--- everywhere ---")
rows = by_code(fx.breakdown())
check("shekel assets include the unheld one", rows["ILS"]["asset_raw"], 764900 * 2 + 500000)
check("counted units include the one on the shelf",
      rows["ILS"]["pooled_raw"], 3 * 210000)
check("sterling asset", rows["GBP"]["asset_raw"], 129900)
check("three dollar seats", rows["USD"]["monthly_raw"], 45000)

print("\n--- Israel: shekels for the kit, dollars for the SaaS ---")
rows = by_code(fx.breakdown("Israel"))
check("only the two Israeli laptops", rows["ILS"]["asset_raw"], 764900 * 2)
check("only the units Israelis hold", rows["ILS"]["pooled_raw"], 2 * 210000)
check("so a one-off total in shekels", rows["ILS"]["oneoff_raw"], 764900 * 2 + 2 * 210000)
check("two dollar seats", rows["USD"]["monthly_raw"], 30000)
check("no sterling here", rows["GBP"]["oneoff_raw"], 0)
check("both currencies still listed", sorted(k for k in rows if k), ["GBP", "ILS", "USD"])

print("\n--- United Kingdom ---")
rows = by_code(fx.breakdown("United Kingdom"))
check("the sterling laptop", rows["GBP"]["asset_raw"], 129900)
check("no shekel kit", rows["ILS"]["oneoff_raw"], 0)
check("the unit was handed back, so it is not his", rows["ILS"]["pooled_raw"], 0)
check("one dollar seat", rows["USD"]["monthly_raw"], 15000)

print("\n--- what a country filter necessarily leaves out ---")
ex = fx.excluded_by_country()
check("the unassigned laptop", ex["assets"], 1)
check("the unit on the shelf", ex["shelf_units"], 1)
check("valued together in the reporting currency", ex["value"],
      fx.to_reporting(500000, 335683) + fx.to_reporting(210000, 335683))
check("and the person with no country", ex["people_without_country"], 1)

check("countries offered come from the people",
      fx.countries(), ["Israel", "United Kingdom"])

print("\n--- an unknown country matches nothing, rather than everything ---")
rows = by_code(fx.breakdown("Atlantis"))
check("no kit", sum(r["oneoff_raw"] for r in rows.values()), 0)
check("no seats", sum(r["monthly_raw"] for r in rows.values()), 0)
check("but the currencies are still listed", len([k for k in rows if k]), 3)

print("\n--- the Intune holder gap ---")
def device(did, asset_id, upn):
    db.execute("""INSERT INTO devices (id, device_name, model, os, primary_upn,
                                       asset_id, synced_at)
                  VALUES (?,?, 'MacBook Air','macOS',?,?,'2026-08-30T00:00:00+00:00')""",
               (did, did, upn, asset_id))

orphan = asset("MacBook Air", None, 0, "ILS", "IL-9")     # created before the sync
device("dev-orphan", orphan, "noa@x.com")                 # Intune knows who has it
ghost = asset("MacBook Air", None, 0, "ILS", "IL-10")
device("dev-ghost", ghost, "leaver@x.com")                # somebody ITAM never saw
shared = asset("MacBook Air", None, 0, "ILS", "IL-11")
device("dev-shared", shared, None)                        # genuinely nobody's
device("dev-yael", db.q1("SELECT id FROM assets WHERE serial='IL-1'")["id"], "ollie@x.com")

gap = devices.holder_gap()
check("one asset can take a holder Intune knows",
      [r["primary_upn"] for r in gap["fillable"]], ["noa@x.com"])
check("one names somebody ITAM has never seen", len(gap["unknown"]), 1)
check("one has no primary user either", len(gap["nobody"]), 1)
check("one disagreement, reported not acted on",
      [(r["assigned_upn"], r["primary_upn"]) for r in gap["mismatch"]],
      [("yael@x.com", "ollie@x.com")])

check("filling in touches exactly the fillable one", devices.fill_holders_from_intune(), 1)
check("the orphan now has its holder",
      db.q1("SELECT assigned_upn FROM assets WHERE id = ?", (orphan,))["assigned_upn"],
      "noa@x.com")
check("the disagreement was left alone",
      db.q1("SELECT assigned_upn FROM assets WHERE serial='IL-1'")["assigned_upn"],
      "yael@x.com")
check("the unknown one is untouched",
      db.q1("SELECT assigned_upn FROM assets WHERE id = ?", (ghost,))["assigned_upn"], None)
check("running it again does nothing", devices.fill_holders_from_intune(), 0)

print("\n--- the headline cards and the table must be the same number ---")
# Three times now a figure on this page has disagreed with the table under it,
# most recently because the card added counted assets that the table's total
# already contained. The cards are derived from the table for that reason, and
# this is what says so.
from fastapi.testclient import TestClient          # noqa: E402
from app import main                               # noqa: E402

def card_and_footer(page: str) -> tuple[str, str]:
    card = re.search(r"Hardware &amp; software value.*?class=\"big\">[A-Z]{3} ([\d,.]+)",
                     page, re.S)
    monthly = re.search(r"Monthly SaaS run-rate.*?class=\"big\">[A-Z]{3} ([\d,.]+)",
                        page, re.S)
    foot = re.search(r"Consolidated.*?<strong>([\d,.]+)</strong>.*?<strong>([\d,.]+)</strong>",
                     page, re.S)
    return (card.group(1), monthly.group(1)), (foot.group(1), foot.group(2))

with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "DashTest!2345"},
                follow_redirects=False)
    for where in ("", "?country=Israel", "?country=United+Kingdom", "?country=Atlantis"):
        cards, footer = card_and_footer(client.get("/" + where).text)
        check(f"one-off card matches the footer {where or '(everywhere)'}",
              cards[0], footer[0])
        check(f"monthly card matches the footer {where or '(everywhere)'}",
              cards[1], footer[1])

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
