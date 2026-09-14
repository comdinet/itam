"""People Entra stopped sending.

The sync upserts and never deletes, so the row count only grows: a tenant of
100 read as 106 because six people had left. The difference has to be
visible and actionable, not a number nobody can reconcile with the portal.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "StaleTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient           # noqa: E402
from app import db, entra, main, people, pooled     # noqa: E402

db.init_db()
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


def graph(users):
    entra._get_all = lambda path, params=None, base=None, advanced=False: users


def person(upn, name, enabled=True):
    return {"userPrincipalName": upn, "displayName": name, "id": "id-" + upn,
            "accountEnabled": enabled, "department": "Business"}


print("--- a first sync brings four people ---")
graph([person(f"p{i}@x.com", f"P{i}") for i in range(4)])
entra.sync()
check("all four tracked", people.seen_last_sync(), 4)
check("nobody is stale yet", people.not_in_entra(), [])

print("\n--- two leave, and the next sync stops sending them ---")
# One of them is holding kit, which is exactly why nothing is deleted for you.
aid = db.execute("""INSERT INTO assets (name, category, cost_cents, assigned_upn)
                    VALUES ('MacBook Air','Laptop',120000,'p3@x.com')""")
mouse = pooled.create("MX Master 3S", "Peripheral", 10900)
pooled.assign(mouse, "p3@x.com", 2)
import time                                          # noqa: E402
time.sleep(1.1)                                      # a later second, so the stamp moves
graph([person(f"p{i}@x.com", f"P{i}") for i in range(2)])
entra.sync()

check("the last sync returned two", people.seen_last_sync(), 2)
check("but four rows are still held",
      db.q1("SELECT COUNT(*) c FROM users WHERE source='entra'")["c"], 4)
stale = people.not_in_entra()
check("and the two who left are named",
      [p["upn"] for p in stale], ["p2@x.com", "p3@x.com"])
check("with what they are still holding",
      [(p["assets"], p["pooled"]) for p in stale], [(0, 0), (1, 2)])
check("nothing was deleted for you",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"],
      "p3@x.com")

print("\n--- the page reconciles with the number in the portal ---")
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "StaleTest!2345"},
                follow_redirects=False)
    page = client.get("/settings/entra/users").text
    check("it reports what Entra returned, not the row count",
          "2 returned by Entra" in page, True)
    check("and names the gap", "Entra no longer sends" in page, True)
    check("p3 is listed", "p3@x.com" in page.split("Entra no longer sends", 1)[1], True)

    print("\n--- stop tracking somebody, and say what it released ---")
    r = client.post("/settings/entra/users/forget", data={"upn": "p3@x.com"},
                    follow_redirects=False)
    msg = dict(r.headers).get("location", "")
    check("the asset is named", "1+asset" in msg, True)
    check("and the counted units", "2+counted" in msg, True)
    check("the person is gone",
          db.q1("SELECT 1 FROM users WHERE upn='p3@x.com'"), None)
    check("their laptop came back as spare, not deleted with them",
          db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"],
          None)
    check("one left in the stale list", [p["upn"] for p in people.not_in_entra()],
          ["p2@x.com"])

print("\n--- somebody Entra sends again stops being stale ---")
time.sleep(1.1)
graph([person(f"p{i}@x.com", f"P{i}") for i in range(3)])
entra.sync()
check("all three returned", people.seen_last_sync(), 3)
check("and nobody is stale", people.not_in_entra(), [])

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
