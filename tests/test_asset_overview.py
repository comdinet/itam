"""The Assets landing page: counts, not a 118-row table.

The number that matters for counted kit is units, not rows and not people:
three monitors handed to one person is three monitors.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "Overview!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"   # the test client speaks http

from app import db, devices, pooled            # noqa: E402

db.init_db()
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


for upn, name in [("a@x.com", "Ann"), ("b@x.com", "Ben"), ("c@x.com", "Cara")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')",
               (upn, name))

print("--- laptops, macOS and Windows ---")
laptops = []
for i, (holder, os_name) in enumerate([("a@x.com", "macOS"), ("b@x.com", "Windows"),
                                       ("c@x.com", "Windows"), (None, "macOS")]):
    aid = db.execute("""INSERT INTO assets (name, category, cost_cents, assigned_upn)
                        VALUES (?,'Laptop',0,?)""", (f"Machine {i}", holder))
    laptops.append(aid)
    db.execute("""INSERT INTO devices (id, device_name, os, asset_id)
                  VALUES (?,?,?,?)""", (f"d{i}", f"HOST{i}", os_name, aid))
# A phone, and an ignored machine: neither is a laptop anybody was issued.
pid = db.execute("INSERT INTO assets (name, category, cost_cents) VALUES ('iPhone','Other',0)")
db.execute("""INSERT INTO devices (id, device_name, os, asset_id)
              VALUES ('d9','PHONE','iOS',?)""", (pid,))
iid = db.execute("INSERT INTO assets (name, category, cost_cents) VALUES ('VM','Laptop',0)")
db.execute("""INSERT INTO devices (id, device_name, os, asset_id, ignored_reason)
              VALUES ('d8','VMWARE','Windows',?,'virtual')""", (iid,))

o = devices.overview()
check("laptops counted per machine", o["laptops"]["total"], 5)
check("and how many are with somebody", o["laptops"]["assigned"], 3)
check("macOS", o["families"]["macOS"], 2)
check("Windows, without the ignored VM", o["families"]["Windows"], 2)
check("a phone is in neither family",
      sum(o["families"].values()), 4)

print("\n--- monitors are counted in monitors, not in rows or people ---")
u27 = pooled.create("Dell U2725QE", "Monitor", 50000)
p27 = pooled.create("Lenovo P27", "Monitor", 40000)
pooled.assign(u27, "a@x.com", 2)          # two of the same to one person
pooled.assign(u27, "b@x.com", 1)
pooled.assign(p27, "c@x.com", 1)
pooled.take_back(p27, "c@x.com")          # back on the shelf, still owned
pooled.assign(p27, "a@x.com", 1)          # re-issued from the shelf

o = devices.overview()
check("three Dells handed out, from two people", o["monitors"]["models"][0],
      {"name": "Dell U2725QE", "assigned": 3})
check("and one Lenovo", o["monitors"]["models"][1],
      {"name": "Lenovo P27", "assigned": 1})
check("handed out is units, not people", o["monitors"]["handed_out"], 4)
check("total is every monitor that exists", o["monitors"]["total"], 4)

print("\n--- a model nobody holds is not a line in the breakdown ---")
pooled.create("Old Acer", "Monitor", 10000)
o = devices.overview()
check("still two models listed", len(o["monitors"]["models"]), 2)

print("\n--- a monitor with a serial is counted too, and said so separately ---")
db.execute("""INSERT INTO assets (name, category, cost_cents, serial, assigned_upn)
              VALUES ('Studio Display','Monitor',0,'SD123','b@x.com')""")
o = devices.overview()
check("the total takes it in", o["monitors"]["total"], 5)
check("handed out too", o["monitors"]["handed_out"], 5)
check("and it is named as tracked by serial", o["monitors"]["serial"], 1)

print("\n--- peripherals, the same way ---")
mouse = pooled.create("MX Master 3S", "Peripheral", 10900)
pooled.assign(mouse, "a@x.com", 1)
pooled.assign(mouse, "b@x.com", 2)
o = devices.overview()
check("units, not rows", o["peripherals"]["handed_out"], 3)
check("one model", o["peripherals"]["models"],
      [{"name": "MX Master 3S", "assigned": 3}])

print("\n--- the landing page shows the widgets and no 100-row table ---")
from fastapi.testclient import TestClient          # noqa: E402
from app import main                               # noqa: E402
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "Overview!2345"},
                follow_redirects=False)
    page = client.get("/assets").text
    check("Laptops replaces Tracked by serial", "Tracked by serial" in page, False)
    for label in ("Laptops", "macOS", "Windows", "Monitors", "Peripherals"):
        check(f"{label} widget", f">{label}</span>" in page, True)
    check("On the shelf is gone", "On the shelf" in page, False)
    check("Counted items is gone", ">Counted items<" in page, False)
    check("and the model breakdown is there", "Dell U2725QE" in page, True)
    check("but not the table of every machine", "HOST0" in page or "Machine 0" in page,
          False)

    print("\n--- a filter brings the list back, or the cost filter is useless ---")
    page = client.get("/assets?priced=unpriced").text
    check("the rows are there when asked for", "Machine 0" in page, True)

    print("\n--- a category tab is unchanged: it lists its own kit ---")
    page = client.get("/assets/c/Laptop").text
    check("the laptops are listed", "Machine 0" in page, True)
    check("with the old cards, not the overview", "Tracked by serial" in page, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
