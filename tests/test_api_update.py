"""The machine-to-machine API: read, update, and hand out in bulk.

Two selectors, because those are the two things anybody knows offhand - the
serial printed on the machine, or the person holding it.
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "ApiTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient           # noqa: E402
from app import api, db, main, pooled               # noqa: E402

db.init_db()
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


for upn, name in [("ann@x.com", "Ann"), ("ben@x.com", "Ben"), ("cara@x.com", "Cara")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')",
               (upn, name))
db.execute("INSERT INTO groups (id, display_name) VALUES ('g1','Israel Staff')")
for upn in ("ann@x.com", "ben@x.com"):
    db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g1',?)", (upn,))

ann_laptop = db.execute(
    """INSERT INTO assets (name, category, cost_cents, serial, assigned_upn)
       VALUES ('MacBook Air','Laptop',120000,'C49KKVM4TF','ann@x.com')""")
ann_dock = db.execute(
    """INSERT INTO assets (name, category, cost_cents, serial, assigned_upn)
       VALUES ('Dock','Peripheral',9900,'DK-1','ann@x.com')""")
ben_laptop = db.execute(
    """INSERT INTO assets (name, category, cost_cents, serial, assigned_upn)
       VALUES ('Latitude 5440','Laptop',95000,'6898QV3','ben@x.com')""")

token = api.create_key("test key")
AUTH = {"Authorization": f"Bearer {token}"}
client = TestClient(main.app)

print("--- the token is the door ---")
check("no token, no answer", client.get("/api/v1/assets?serial=C49KKVM4TF").status_code, 401)
check("a wrong token likewise",
      client.patch("/api/v1/assets", json={"set": {}},
                   headers={"Authorization": "Bearer itam_nope"}).status_code, 401)

print("\n--- read by serial, and by who holds it ---")
r = client.get("/api/v1/assets?serial=C49KKVM4TF", headers=AUTH).json()
check("one asset", r["matched"], 1)
check("the right one", r["assets"][0]["asset_id"], ann_laptop)
check("serials are matched however they are typed",
      client.get("/api/v1/assets?serial=c49kkvm4tf", headers=AUTH).json()["matched"], 1)
check("everything one person holds",
      sorted(a["asset_id"] for a in
             client.get("/api/v1/assets?upn=ann@x.com", headers=AUTH).json()["assets"]),
      sorted([ann_laptop, ann_dock]))
check("a serial nobody has is an answer, not an error",
      client.get("/api/v1/assets?serial=NOPE", headers=AUTH).json()["matched"], 0)

print("\n--- a status you can set, and one you cannot ---")
r = client.patch("/api/v1/assets",
                 json={"where": {"serial": "C49KKVM4TF"},
                       "set": {"status": "Sold to employee"}}, headers=AUTH)
check("accepted", r.status_code, 200)
check("and it stuck",
      db.q1("SELECT status FROM assets WHERE id = ?", (ann_laptop,))["status"],
      "Sold to employee")
r = client.patch("/api/v1/assets",
                 json={"where": {"serial": "6898QV3"}, "set": {"status": "Stolen"}},
                 headers=AUTH)
check("an invented status is refused", r.status_code, 422)
check("and the message says what is allowed",
      "Sold to employee" in r.json()["error"], True)
check("nothing was written",
      db.q1("SELECT status FROM assets WHERE id = ?", (ben_laptop,))["status"], None)

print("\n--- dry_run answers the question without doing it ---")
r = client.patch("/api/v1/assets",
                 json={"where": {"assigned_upn": "ann@x.com"},
                       "set": {"status": ""}, "dry_run": True}, headers=AUTH).json()
check("it says what it would hit", r["matched"], 2)
check("and calls itself a rehearsal", r["status"], "would_update")
check("the status is untouched",
      db.q1("SELECT status FROM assets WHERE id = ?", (ann_laptop,))["status"],
      "Sold to employee")

print("\n--- update everything one person holds ---")
r = client.patch("/api/v1/assets",
                 json={"where": {"assigned_upn": "ann@x.com"},
                       "set": {"notes": "handed over 2026-09-13"}}, headers=AUTH).json()
check("both of Ann's", r["matched"], 2)
check("written", {a["notes"] for a in r["assets"]}, {"handed over 2026-09-13"})

print("\n--- reassigning stamps the date, and only when it changed ---")
before = db.q1("SELECT assigned_on FROM assets WHERE id = ?", (ben_laptop,))["assigned_on"]
client.patch("/api/v1/assets", json={"where": {"serial": "6898QV3"},
                                     "set": {"assigned_upn": "cara@x.com"}}, headers=AUTH)
after = db.q1("SELECT assigned_upn, assigned_on FROM assets WHERE id = ?", (ben_laptop,))
check("the new holder", after["assigned_upn"], "cara@x.com")
check("dated today", after["assigned_on"] is not None and after["assigned_on"] != before, True)
r = client.patch("/api/v1/assets", json={"where": {"serial": "6898QV3"},
                                         "set": {"assigned_upn": "cara@x.com"}},
                 headers=AUTH).json()
check("assigning the same person again is not a new handover",
      db.q1("SELECT assigned_on FROM assets WHERE id = ?", (ben_laptop,))["assigned_on"],
      after["assigned_on"])

print("\n--- what an update may not touch ---")
r = client.patch("/api/v1/assets", json={"where": {"serial": "DK-1"},
                                         "set": {"external_id": "haha"}}, headers=AUTH)
check("a field outside the list is refused", r.status_code, 400)
check("and it lists what is settable", "status" in r.json()["error"], True)
r = client.patch("/api/v1/assets", json={"set": {"notes": "x"}}, headers=AUTH)
check("no selector is refused, not treated as everything", r.status_code, 400)
r = client.patch("/api/v1/assets",
                 json={"where": {"serial": "DK-1", "assigned_upn": "ann@x.com"},
                       "set": {"notes": "x"}}, headers=AUTH)
check("two selectors are refused too", r.status_code, 400)
r = client.patch("/api/v1/assets", json={"where": {"serial": "DK-1"},
                                         "set": {"assigned_upn": "ghost@x.com"}},
                 headers=AUTH)
check("a UPN nobody has is refused", r.status_code, 422)

print("\n--- mass assign to a group ---")
monitor = pooled.create("Dell U2725QE", "Monitor", 50000)
r = client.post("/api/v1/assign",
                json={"item": "Dell U2725QE", "to": {"group": "Israel Staff"},
                      "dry_run": True}, headers=AUTH).json()
check("it would reach the whole group", r["people"], 2)
check("without handing anything out", pooled.assigned_units(monitor), 0)

r = client.post("/api/v1/assign",
                json={"item": "Dell U2725QE", "to": {"group": "Israel Staff"},
                      "quantity": 2}, headers=AUTH).json()
check("two each", r["units"], 4)
check("and the units are really out", pooled.assigned_units(monitor), 4)
check("counted in units, not people", r["people"], 2)

print("\n--- mass assign to people you name ---")
r = client.post("/api/v1/assign",
                json={"item": "Dell U2725QE",
                      "to": {"upns": ["cara@x.com", "ghost@x.com"]}}, headers=AUTH).json()
check("the real one is served", [a["upn"] for a in r["assigned"]], ["cara@x.com"])
check("and the typo is reported, not swallowed",
      r["problems"], [{"upn": "ghost@x.com", "why": "nobody in ITAM has that UPN"}])

print("\n--- the refusals that save you from a silent bulk mistake ---")
r = client.post("/api/v1/assign", json={"item": "Dell U2725QE",
                                        "to": {"group": "Nobody"}}, headers=AUTH)
check("an unknown group is refused", r.status_code, 422)
check("and names the ones it has", "Israel Staff" in r.json()["error"], True)
r = client.post("/api/v1/assign", json={"item": "MacBook Air",
                                        "to": {"group": "Israel Staff"}}, headers=AUTH)
check("a serial-tracked thing cannot be mass assigned", r.status_code, 422)
check("and it says what to do instead", "PATCH" in r.json()["error"], True)
r = client.post("/api/v1/assign", json={"item": "Dell U2725QE",
                                        "to": {"group": "Israel Staff",
                                               "upns": ["ann@x.com"]}}, headers=AUTH)
check("group and upns together is refused", r.status_code, 400)

print("\n--- a key without permission cannot assign ---")
readonly = api.create_key("read only", can_create_assets=True, can_assign=False)
r = client.patch("/api/v1/assets",
                 json={"where": {"serial": "DK-1"}, "set": {"assigned_upn": "ben@x.com"}},
                 headers={"Authorization": f"Bearer {readonly}"})
check("refused", r.status_code, 403)
r = client.patch("/api/v1/assets",
                 json={"where": {"serial": "DK-1"}, "set": {"notes": "fine"}},
                 headers={"Authorization": f"Bearer {readonly}"})
check("but it may still edit what it is allowed to", r.status_code, 200)

print("\n--- every call is in the log, which is where you look afterwards ---")
recent = [r["endpoint"] for r in api.recent_log(50)]
for endpoint in ("PATCH /api/v1/assets", "POST /api/v1/assign", "GET /api/v1/assets"):
    check(f"{endpoint} logged", endpoint in recent, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
