"""Every change to an asset, and who made it.

Written by diffing the row, not by a hand-kept list of loggable fields: the
first you hear of a hand-kept list is when the history is missing the change
you needed.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "HistTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient           # noqa: E402
from app import api, db, events, fx, main, pricing  # noqa: E402

db.init_db()
fx.add("USD", "$", "US dollar", 1_000_000, "manual", "test")
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


def moves(asset_id):
    """field -> (old, new), flattened across the whole timeline."""
    out = {}
    for entry in events.for_asset(asset_id):
        for c in entry["changes"]:
            out.setdefault(c["field"], []).append((c["old"], c["new"]))
    return out


for upn, name in [("ann@x.com", "Ann"), ("ben@x.com", "Ben")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')",
               (upn, name))

with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "HistTest!2345"},
                follow_redirects=False)

    print("--- an asset created by hand opens its own history ---")
    client.post("/assets/new", data={
        "name": "MacBook Air", "category": "Laptop", "cost": "1200.00",
        "currency": "USD", "serial": "C49KKVM4TF", "purchased_on": "2026-01-15",
        "notes": "", "assigned_upn": "ann@x.com", "redirect": "/assets"},
        follow_redirects=False)
    aid = db.q1("SELECT id FROM assets WHERE serial = 'C49KKVM4TF'")["id"]
    timeline = events.for_asset(aid)
    check("one entry, not one per field", len(timeline), 1)
    check("it says created", timeline[0]["action"], "created")
    check("by the person signed in", timeline[0]["actor"], "admin")
    check("from the browser", timeline[0]["source"], "ui")
    opened = {c["field"]: c["new"] for c in timeline[0]["changes"]}
    check("with the state it arrived in", opened["cost_cents"], "1,200.00")
    check("including who got it", opened["assigned_upn"], "ann@x.com")
    check("and the cost reads as money, not as cents",
          "120000" in str(list(opened.values())), False)

    print("\n--- an edit records only what moved ---")
    client.post(f"/assets/{aid}/edit", data={
        "name": "MacBook Air 15", "category": "Laptop", "cost": "1200.00",
        "currency": "USD", "serial": "C49KKVM4TF", "purchased_on": "2026-01-15",
        "notes": "", "assigned_upn": "ann@x.com", "status": "Sold to employee"},
        follow_redirects=False)
    latest = events.for_asset(aid)[0]
    check("one entry for one form submission", len(latest["changes"]), 2)
    check("the name", ("MacBook Air", "MacBook Air 15") in
          [(c["old"], c["new"]) for c in latest["changes"]], True)
    check("and the status", ("Status", None, "Sold to employee") in
          [(c["label"], c["old"], c["new"]) for c in latest["changes"]], True)
    check("the cost did not move, so it is not in the history",
          sorted(c["field"] for c in latest["changes"]), ["name", "status"])

    print("\n--- handing it to somebody else, and taking it back ---")
    client.post("/users/ben@x.com/assign-asset", data={"asset_id": aid},
                follow_redirects=False)
    check("the handover is recorded with both ends",
          moves(aid)["assigned_upn"][0], ("ann@x.com", "ben@x.com"))
    client.post(f"/assets/{aid}/unassign", data={"redirect": "/assets"},
                follow_redirects=False)
    check("and a return reads as cleared, not as blank",
          moves(aid)["assigned_upn"][0], ("ben@x.com", None))

    print("\n--- a pricing rule is named, not filed under whoever pressed apply ---")
    gid = pricing.create("Mac fleet", 250000, currency="USD", rate_micro=1_000_000)
    pricing.add_criterion(gid, "model", "contains", "MacBook")
    pricing.apply(pricing.get(gid))
    latest = events.for_asset(aid)[0]
    check("the source says it was a rule", latest["source"], "rule")
    check("and which rule", "Mac fleet" in (latest["actor"] or ""), True)
    check("with the price it moved to",
          [c["new"] for c in latest["changes"] if c["field"] == "cost_cents"],
          ["2,500.00"])

    print("\n--- the API is a named actor too ---")
    token = api.create_key("frappe")
    client.patch("/api/v1/assets",
                 json={"where": {"serial": "C49KKVM4TF"},
                       "set": {"notes": "returned from Ben"}},
                 headers={"Authorization": f"Bearer {token}"})
    latest = events.for_asset(aid)[0]
    check("named by its key", latest["actor"], "frappe")
    check("and marked as the API", latest["source"], "api")

    print("\n--- a dry run changes nothing, and so records nothing ---")
    before = len(events.for_asset(aid))
    client.patch("/api/v1/assets",
                 json={"where": {"serial": "C49KKVM4TF"},
                       "set": {"notes": "nope"}, "dry_run": True},
                 headers={"Authorization": f"Bearer {token}"})
    check("no entry", len(events.for_asset(aid)), before)

    print("\n--- the history outlives the asset ---")
    client.post(f"/assets/{aid}/delete", data={"redirect": "/assets"},
                follow_redirects=False)
    check("the asset is gone", db.q1("SELECT 1 FROM assets WHERE id = ?", (aid,)), None)
    gone = [e for e in events.recent(200) if e["action"] == "deleted"]
    check("but the deletion is recorded", len(gone), 1)
    check("with the name it had", gone[0]["asset_name"], "MacBook Air 15")
    check("and by whom", gone[0]["actor"], "admin")
    check("its earlier history is still readable",
          len(events.recent(200)) > 1, True)

    print("\n--- and it is on the page, not only in the table ---")
    aid2 = db.execute("""INSERT INTO assets (name, category, cost_cents, serial)
                         VALUES ('Latitude 5440','Laptop',0,'6898QV3')""")
    events.created(aid2, "sync")
    page = client.get(f"/assets/{aid2}").text
    check("the asset page shows a timeline", 'class="timeline"' in page, True)
    check("a sync with no person is named by what it was", ">sync<" in page, True)
    feed = client.get("/activity").text
    check("the activity page lists it", "Latitude 5440" in feed, True)
    check("and links back to the asset", f'href="/assets/{aid2}"' in feed, True)
    check("a deleted asset is struck through rather than linked",
          "MacBook Air 15</span>" in feed, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
