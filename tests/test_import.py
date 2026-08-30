"""CSV import from ITAM's template: preview first, then apply.

The template is ITAM's, not the vendor's - so the same three columns work for
Claude, Cursor, Notion or a product nobody has heard of yet. What matters is
that the preview tells the truth, that people are never invented, and that
running it twice does nothing the second time.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, fx, imports
db.init_db(); fx.ensure_base()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

for upn in ("amit@x.com", "shay@x.com", "brachi@x.com", "noa@x.com"):
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')",
               (upn, upn.split("@")[0].title()))

# Exactly the shape of the file Edgar attached: Email, Subscription name,
# Subscription tier - two tiers of one product.
FILE = """Email,Subscription name,Subscription tier
amit@x.com,Claude AI,Premium
shay@x.com,Claude AI,Premium
brachi@x.com,Claude AI,Standard
nobody@elsewhere.com,Claude AI,Standard
,Claude AI,Premium
noa@x.com,,
"""

print("--- the preview, before anything is written ---")
plan = imports.plan_subscription_seats(FILE)
check("rows read", plan["rows"], 6)
check("two subscriptions, one per tier",
      [e["name"] for e in plan["subscriptions"]], ["Claude AI Premium", "Claude AI Standard"])
check("both are new", len(plan["to_create"]), 2)
check("three seats to assign", len(plan["seats"]), 3)
check("neither has a price yet", len(plan["unpriced"]), 2)
check("the unknown address is skipped, with a reason",
      [(s["what"], s["why"]) for s in plan["skipped"] if s["what"] == "nobody@elsewhere.com"],
      [("nobody@elsewhere.com", "nobody in ITAM has that UPN")])
check("so is the row with no email",
      [s["why"] for s in plan["skipped"] if s["what"] == "(no email)"],
      ["no email given"])
check("and the row with no subscription",
      [s["why"] for s in plan["skipped"] if s["what"] == "noa@x.com"],
      ["no subscription name given"])
check("nothing was written by previewing",
      db.q1("SELECT COUNT(*) c FROM subscriptions")["c"], 0)

print("\n--- applying it ---")
result = imports.apply_subscription_seats(FILE)
check("two created", result["created"], 2)
check("three seats", result["seats"], 3)
names = [r["name"] for r in db.q("SELECT name FROM subscriptions ORDER BY name")]
check("named product + tier", names, ["Claude AI Premium", "Claude AI Standard"])
check("Premium has two seats",
      db.q1("""SELECT COUNT(*) c FROM subscription_seats ss JOIN subscriptions s
               ON s.id = ss.subscription_id WHERE s.name = 'Claude AI Premium'""")["c"], 2)
check("Standard has one",
      db.q1("""SELECT COUNT(*) c FROM subscription_seats ss JOIN subscriptions s
               ON s.id = ss.subscription_id WHERE s.name = 'Claude AI Standard'""")["c"], 1)
check("nobody was invented", db.q1("SELECT COUNT(*) c FROM users")["c"], 4)

print("\n--- importing the same file again does nothing ---")
plan = imports.plan_subscription_seats(FILE)
check("nothing new to create", len(plan["to_create"]), 0)
check("no seats to add", len(plan["seats"]), 0)
check("three already held", len(plan["already"]), 3)
again = imports.apply_subscription_seats(FILE)
check("created nothing", again["created"], 0)
check("still three seats total",
      db.q1("SELECT COUNT(*) c FROM subscription_seats")["c"], 3)

print("\n--- prices, when the file carries them ---")
fx.add("ILS", "₪", "Israeli new shekel", 335683, "boi", "test", "2026-08-30")
PRICED = """Email,Subscription name,Subscription tier,Monthly cost,Currency,Vendor
noa@x.com,Cursor,Business,80.00,ILS,Cursor
amit@x.com,Cursor,Business,80.00,ILS,Cursor
"""
plan = imports.plan_subscription_seats(PRICED)
entry = plan["subscriptions"][0]
check("price parsed", entry["cost_cents"], 8000)
check("currency taken from the file", entry["currency"], "ILS")
check("and its rate frozen", entry["rate_micro"], 335683)
check("vendor carried", entry["vendor"], "Cursor")
check("so it is not flagged unpriced", plan["unpriced"], [])
imports.apply_subscription_seats(PRICED)
row = db.q1("SELECT * FROM subscriptions WHERE name = 'Cursor Business'")
check("stored with its currency",
      (row["monthly_cost_cents"], row["currency"], row["rate_micro"]), (8000, "ILS", 335683))

print("\n--- a cost with no currency is refused, not guessed ---")
NO_CUR = """Email,Subscription name,Subscription tier,Monthly cost
noa@x.com,Notion,Plus,10.00
"""
plan = imports.plan_subscription_seats(NO_CUR)
entry = plan["subscriptions"][0]
check("the cost is dropped", entry["cost_cents"], 0)
check("and it says why", len(entry["problems"]), 1)
check("naming the line", "line 2" in entry["problems"][0], True)
check("an unknown currency is refused too",
      len(imports.plan_subscription_seats(
          "Email,Subscription name,Monthly cost,Currency\nnoa@x.com,X,5.00,XYZ\n"
      )["subscriptions"][0]["problems"]), 1)

print("\n--- two prices for one subscription: the first wins, loudly ---")
CLASH = """Email,Subscription name,Monthly cost,Currency
noa@x.com,Linear,10.00,USD
amit@x.com,Linear,14.00,USD
"""
entry = imports.plan_subscription_seats(CLASH)["subscriptions"][0]
check("first price kept", entry["cost_cents"], 1000)
check("the clash is reported", len(entry["problems"]), 1)

print("\n--- headers ---")
check("order and case do not matter",
      imports.plan_subscription_seats(
          "subscription TIER,EMAIL,Subscription Name\nPremium,noa@x.com,Slack\n"
      )["subscriptions"][0]["name"], "Slack Premium")
check("a byte-order mark does not break the first column",
      imports.plan_subscription_seats(
          "\ufeffEmail,Subscription name\nnoa@x.com,Zoom\n"
      )["subscriptions"][0]["name"], "Zoom")

def refused(text):
    try:
        imports.plan_subscription_seats(text)
        return None
    except imports.ImportError_ as exc:
        return str(exc)

missing = refused("Email,Tier\nnoa@x.com,Premium\n")
check("a missing required column is refused", missing is not None, True)
check("saying which one", "Subscription name" in missing, True)
check("and showing what it did find", "Tier" in missing, True)
check("an empty file is refused", refused("") is not None, True)

print("\n--- the template ITAM hands out is one this importer accepts ---")
text = imports.template_csv(imports.SUBSCRIPTION_SEATS)
check("it round-trips", refused(text), None)
plan = imports.plan_subscription_seats(text)
check("its example rows are skipped as unknown people",
      len(plan["skipped"]), len(imports.SUBSCRIPTION_SEATS["example"]))
check("so importing the blank template does nothing", len(plan["seats"]), 0)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
