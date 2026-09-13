"""One Add form per category, a status, and country names without the tail."""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "FormTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient           # noqa: E402
from app import db, fx, main, people                # noqa: E402

db.init_db()
# No defaults anywhere: a cost with no currency is refused, so the fixture has
# to set one up the same way a real install does.
fx.add("USD", "$", "US dollar", 1_000_000, "manual", "test")
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


with TestClient(main.app) as client:     # lifespan runs: bootstrap the admin
    client.post("/login", data={"username": "admin", "password": "FormTest!2345"},
                follow_redirects=False)


    print("--- a laptop always has a serial; a monitor need not ---")
    for category in ("Laptop", "Desktop"):
        page = client.get(f"/assets/c/{category}").text
        check(f"{category}: the serial form is offered",
              "tracked by its serial" in page, True)
        check(f"{category}: and no counted form",
              "&mdash; counted" in page or "— counted" in page, False)
    for category in ("Monitor", "Peripheral", "Software", "Other"):
        page = client.get(f"/assets/c/{category}").text
        check(f"{category}: no serial-tracked form",
              "tracked by its serial" in page, False)
        check(f"{category}: the counted form is offered",
              "— counted" in page or "&mdash; counted" in page, True)
        form = page.split("add-counted", 1)[1].split("</details>", 1)[0]
        check(f"{category}: with an optional serial",
              'name="serial" placeholder="optional"' in form, True)

    print("\n--- the All tab still offers both, because you pick the category there ---")
    page = client.get("/assets").text
    check("serial form", "tracked by its serial" in page, True)
    check("counted form", "&mdash; counted" in page or "— counted" in page, True)

    print("\n--- a serial on the counted form makes it one record for one unit ---")
    client.post("/assets/pooled/new",
                data={"name": "CalDigit TS4", "category": "Peripheral",
                      "unit_cost": "350.00", "currency": "USD", "serial": "TS4-9981",
                      "redirect": "/assets/c/Peripheral"}, follow_redirects=False)
    asset = db.q1("SELECT * FROM assets WHERE serial = 'TS4-9981'")
    check("it is an asset, not a counted item", asset is not None, True)
    check("in the category it was added under", asset["category"], "Peripheral")
    check("and nothing was counted",
          db.q1("SELECT COUNT(*) c FROM pooled_items WHERE name='CalDigit TS4'")["c"], 0)

    client.post("/assets/pooled/new",
                data={"name": "MX Master 3S", "category": "Peripheral",
                      "unit_cost": "109.00", "currency": "USD", "serial": "",
                      "redirect": "/assets/c/Peripheral"}, follow_redirects=False)
    check("without a serial it is counted, as before",
          db.q1("SELECT COUNT(*) c FROM pooled_items WHERE name='MX Master 3S'")["c"], 1)

    print("\n--- status: recorded, shown, filtered ---")
    aid = db.execute("""INSERT INTO assets (name, category, cost_cents, serial)
                        VALUES ('Latitude 5440','Laptop',95000,'6898QV3')""")
    check("a new asset has none",
          db.q1("SELECT status FROM assets WHERE id = ?", (aid,))["status"], None)
    client.post(f"/assets/{aid}/edit",
                data={"name": "Latitude 5440", "category": "Laptop", "cost": "950.00",
                      "currency": "USD", "serial": "6898QV3", "purchased_on": "",
                      "notes": "", "assigned_upn": "", "status": "Sold to employee"},
                follow_redirects=False)
    check("set from the edit form",
          db.q1("SELECT status FROM assets WHERE id = ?", (aid,))["status"],
          "Sold to employee")
    page = client.get("/assets/c/Laptop").text
    check("shown as a pill beside the name",
          '<span class="pill warn">Sold to employee</span>' in page, True)
    check("the filter finds it",
          "6898QV3" in client.get("/assets?status=Sold+to+employee").text, True)
    check("and 'No status' excludes it",
          "6898QV3" in client.get("/assets?status=none").text, False)

    print("\n--- country names lose the ISO tail, and stop being two countries ---")
    for i, country in enumerate([
            "United Kingdom of Great Britain and Northern Ireland (the)",
            "United Kingdom of Great Britain and Northern Ireland",
            "United States of America (the)", "Israel"]):
        db.execute("""INSERT INTO users (upn, display_name, source, country)
                      VALUES (?,?,'entra',?)""", (f"u{i}@x.com", f"P{i}", country))
    db.init_db()          # the migration is what fixes rows that are already there
    check("one United Kingdom, not two", fx.countries(),
          ["Israel", "United Kingdom of Great Britain and Northern Ireland",
           "United States of America"])
    check("and the sync stores the tidy form",
          people.tidy_country("United States of America (the)"), "United States of America")
    check("a country without the tail is untouched",
          people.tidy_country("Israel"), "Israel")
    check("blank stays blank", people.tidy_country("  "), None)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
