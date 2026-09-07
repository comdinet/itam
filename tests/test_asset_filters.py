"""Finding the assets nobody has priced.

A cost of zero means "nobody has said what this cost", not "it was free". Kit
created from an Intune sync lands at zero unless a pricing group covers it, so
without a way to list those, the gap between what the estate cost and what ITAM
says it cost is invisible.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "AssetTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
from app import db, fx, pooled
db.init_db(); fx.ensure_base()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

db.execute("INSERT INTO users (upn, display_name, source) VALUES ('yael@x.com','Yael','entra')")

def asset(name, category, cents, serial, upn=None):
    db.execute("""INSERT INTO assets (name, category, cost_cents, currency, rate_micro,
                                      serial, assigned_upn)
                  VALUES (?,?,?,'USD',1000000,?,?)""", (name, category, cents, serial, upn))

asset("MacBook Air 13 M4", "Laptop", 129900, "SN-1", "yael@x.com")
asset("MacBook Air 13 M4", "Laptop", 0, "SN-2", "yael@x.com")   # from Intune, unpriced
asset("MacBook Air 13 M4", "Laptop", 0, "SN-3")                 # unpriced and spare
asset("Dell U2723QE", "Monitor", 59900, "SN-4")
priced_pool = pooled.create("Logitech MX Master 3S", "Peripheral", 10900,
                            currency="USD", rate_micro=1000000)
free_pool = pooled.create("Keychron K3", "Peripheral", 0,
                          currency="USD", rate_micro=1000000)

from app.main import asset_rows                        # noqa: E402

print("--- the query itself ---")
def serials(**kw):
    return sorted(r["serial"] for r in asset_rows(**kw))

check("everything", serials(), ["SN-1", "SN-2", "SN-3", "SN-4"])
check("no cost set", serials(priced="unpriced"), ["SN-2", "SN-3"])
check("has a cost", serials(priced="priced"), ["SN-1", "SN-4"])
check("composes with the category", serials(category="Laptop", priced="unpriced"),
      ["SN-2", "SN-3"])
check("and with the state", serials(state="spare", priced="unpriced"), ["SN-3"])
check("and with the search", serials(q="MacBook", priced="unpriced"), ["SN-2", "SN-3"])
check("a category with nothing unpriced", serials(category="Monitor", priced="unpriced"), [])

print("\n--- through the page, including counted items ---")
from fastapi.testclient import TestClient              # noqa: E402
from app import main                                   # noqa: E402
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "AssetTest!2345"},
                follow_redirects=False)

    def shown(url):
        page = client.get(url).text
        # Only the counted TABLE, not the whole page: the Add form uses
        # "Logitech MX Master 3S" as its placeholder, so searching the page
        # finds it whether or not the row is there.
        body = page.split('<h2>Counted', 1)[-1]
        return {"serials": sorted(s for s in ("SN-1", "SN-2", "SN-3", "SN-4") if s in page),
                "pooled": sorted(n for n in ("Logitech MX Master 3S", "Keychron K3")
                                 if f">{n}</a>" in body),
                "page": page}

    # The landing page is a summary now, so "everything" is asked for with a
    # filter that excludes nothing rather than by loading a bare /assets.
    all_of_it = shown("/assets?state=")
    check("the landing page lists nothing until asked",
          shown("/assets")["serials"], [])
    everything = shown("/assets?q=SN-")
    check("all four serials listed", everything["serials"],
          ["SN-1", "SN-2", "SN-3", "SN-4"])
    counted = shown("/assets?priced=unpriced")["pooled"] + \
        shown("/assets?priced=priced")["pooled"]
    check("both counted items listed", sorted(counted),
          ["Keychron K3", "Logitech MX Master 3S"])

    unpriced = shown("/assets?priced=unpriced")
    check("only the unpriced serials", unpriced["serials"], ["SN-2", "SN-3"])
    check("and only the unpriced counted item", unpriced["pooled"], ["Keychron K3"])

    priced = shown("/assets?priced=priced")
    check("only the priced serials", priced["serials"], ["SN-1", "SN-4"])
    check("and only the priced counted item", priced["pooled"], ["Logitech MX Master 3S"])

    print("\n--- a zero reads as absent, not as the number nought ---")
    check("flagged in the table", "no cost set" in unpriced["page"], True)
    check("the count is offered before you filter",
          "3</strong>\n  record(s) have no cost set" in all_of_it["page"]
          or "<strong>3</strong>" in all_of_it["page"], True)
    check("and the summary still offers it, with no list on screen",
          "Show just those" in shown("/assets")["page"], True)
    check("the hint is not shown once you are already filtered",
          "Show just those" in unpriced["page"], False)

    print("\n--- it survives into a category page ---")
    laptops = shown("/assets/c/Laptop?priced=unpriced")
    check("two unpriced laptops", laptops["serials"], ["SN-2", "SN-3"])
    check("the monitor is not there", "SN-4" in laptops["page"], False)

    print("\n--- the total only counts what is shown ---")
    # SN-1 alone: 1,299.00. The unpriced ones add nothing by definition.
    check("filtered to unpriced, the total is zero",
          "USD 0.00" in unpriced["page"], True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
