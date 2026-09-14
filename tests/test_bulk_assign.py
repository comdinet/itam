"""Handing the same thing to many people at once, from the People page.

The workflow this exists for: filter to a country, tick the header box, assign.
So the important properties are that it takes a list, that a licence is one seat
per person however many times you run it, and that it refuses the things that
make no sense in bulk.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "BulkTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from app import db, fx, pooled, main

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

with TestClient(main.app) as client:
    fx.ensure_base()
    for upn, country in [("yael@x.com", "Israel"), ("noa@x.com", "Israel"),
                         ("ollie@x.com", "United Kingdom")]:
        db.execute("INSERT INTO users (upn, display_name, country, source) "
                   "VALUES (?,?,?,'entra')", (upn, upn.split("@")[0].title(), country))
    mon = pooled.create("Dell U2723QE", "Monitor", 59900,
                        currency="USD", rate_micro=1000000)
    sub = db.execute("INSERT INTO subscriptions (name, monthly_cost_cents, currency, "
                     "rate_micro) VALUES ('Claude AI Premium',15000,'USD',1000000)")
    client.post("/login", data={"username": "admin", "password": "BulkTest!2345"},
                follow_redirects=False)

    def bulk(**data):
        return client.post("/users/bulk-assign", data=data, follow_redirects=False)

    print("--- two monitors each to the two Israelis ---")
    r = bulk(upn=["yael@x.com", "noa@x.com"], target=f"pooled:{mon}", quantity="2")
    check("redirects rather than erroring", r.status_code, 303)
    check("Yael has two", pooled.held_by("yael@x.com", "Monitor"), 2)
    check("Noa has two", pooled.held_by("noa@x.com", "Monitor"), 2)
    check("Ollie was not ticked, so has none", pooled.held_by("ollie@x.com", "Monitor"), 0)
    check("four units now exist", pooled.summary(pooled.get(mon))["owned"], 4)

    print("\n--- shelf units are re-used before new ones are counted ---")
    pooled.take_back(mon, "yael@x.com", 2)
    check("two on the shelf", pooled.get(mon)["spare"], 2)
    bulk(upn=["yael@x.com"], target=f"pooled:{mon}", quantity="2")
    check("shelf emptied", pooled.get(mon)["spare"], 0)
    check("and nothing new was bought", pooled.summary(pooled.get(mon))["owned"], 4)

    print("\n--- a licence is one seat each, however many times you run it ---")
    bulk(upn=["yael@x.com", "noa@x.com", "ollie@x.com"], target=f"sub:{sub}")
    check("three seats", db.q1("SELECT COUNT(*) c FROM subscription_seats")["c"], 3)
    r = bulk(upn=["yael@x.com", "noa@x.com"], target=f"sub:{sub}")
    check("re-running adds none", db.q1("SELECT COUNT(*) c FROM subscription_seats")["c"], 3)
    check("and says so", "already+had+one" in (r.headers.get("location") or ""), True)

    print("\n--- refusals ---")
    def complaint(**data):
        loc = bulk(**data).headers.get("location", "")
        return loc.split("msg=")[-1] if "msg=" in loc else ""

    check("nobody ticked", complaint(upn=[], target=f"pooled:{mon}"), "Tick+somebody+first")
    check("nothing chosen", complaint(upn=["yael@x.com"], target=""), "Choose+what+to+assign")
    check("a serial-tracked asset cannot be named",
          complaint(upn=["yael@x.com"], target="asset:1"), "Choose+what+to+assign")
    check("a made-up item", "No+such+item" in complaint(
        upn=["yael@x.com"], target="pooled:9999"), True)
    check("a made-up subscription", "No+such+subscription" in complaint(
        upn=["yael@x.com"], target="sub:9999"), True)
    check("an unknown person is reported, not created",
          "could+not+be+done" in complaint(
              upn=["ghost@x.com"], target=f"pooled:{mon}", quantity="1"), True)
    check("and no user was invented", db.q1("SELECT COUNT(*) c FROM users")["c"], 3)

    print("\n--- the page offers both kinds, and a box per person ---")
    page = client.get("/users").text
    check("a checkbox per person", page.count('class="pick"'), 3)
    check("counted assets are offered", 'value="pooled:%d"' % mon in page, True)
    check("licences too", 'value="sub:%d"' % sub in page, True)
    check("the filter is carried back so you land where you were",
          'value="/users?country=Israel"' in client.get("/users?country=Israel").text, True)

print("\n--- handing several things to one person, in one go ---")
# The old shape was a link per item: four things meant four page loads and no
# way to see what you had already chosen.
kb = pooled.create("Logitech MX Keys", "Peripheral", 49900)
pad = pooled.create("Apple Magic Trackpad", "Peripheral", 34900)
stand = pooled.create("Laptop stand 360", "Peripheral", 19900)
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "BulkTest!2345"},
                follow_redirects=False)
    who = db.q1("SELECT upn FROM users ORDER BY upn LIMIT 1")["upn"]
    r = client.post(f"/users/{who}/hand-out", follow_redirects=False,
                    data={"item": [str(kb), str(pad)],
                          f"qty-{kb}": "1", f"qty-{pad}": "3"})
    check("one redirect, not three", r.status_code, 303)
    check("the keyboard", pooled.held_by(who, "Peripheral", "Logitech MX Keys"), 1)
    check("three trackpads", pooled.held_by(who, "Peripheral", "Apple Magic Trackpad"), 3)
    check("and nothing that was not ticked",
          pooled.held_by(who, "Peripheral", "Laptop stand 360"), 0)
    check("the message names what went out",
          "Apple+Magic+Trackpad" in r.headers["location"], True)

    print("\n--- a picker that shows what is on the shelf before you choose ---")
    pooled.assign(stand, who, 1)
    pooled.take_back(stand, who)
    page = client.get(f"/users/{who}").text
    dialog = page.split('<dialog id="hand-out"', 1)[1].split("</dialog>", 1)[0]
    check("every item is offered", 'value="' + str(stand) + '"' in dialog, True)
    check("with what came back already on the shelf", "1 on the shelf" in dialog, True)
    check("and a quantity per item, not one for all of them",
          f'name="qty-{stand}"' in dialog, True)
    check("the row of one-click links is gone", "+ Logitech MX Keys" in page, False)

    print("\n--- an item deleted while the dialog was open is reported ---")
    gone = pooled.create("Sony ULT900", "Peripheral", 29900)
    pooled.delete(gone)
    r = client.post(f"/users/{who}/hand-out", follow_redirects=False,
                    data={"item": [str(gone), str(kb)]})
    check("the one that still exists goes out",
          pooled.held_by(who, "Peripheral", "Logitech MX Keys"), 2)
    check("and the missing one is named, not silently dropped",
          "no+longer+exists" in r.headers["location"], True)

print("\nFAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
