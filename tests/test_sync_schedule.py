"""The sync has to actually happen, and it has to fill in holders.

Two things went wrong together on the real deployment. Nothing installed the
cron entry, so ITAM only synced when somebody pressed a button; and even a sync
that ran did not update who holds a machine, because the holder is copied from
the device when the ASSET is created and never afterwards. A laptop handed to
somebody a week ago showed up in Intune and stayed unassigned in ITAM.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, devices, entra, jobs, people
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

print("--- every job is in the running order, and holders comes last ---")
check("nothing is defined but unscheduled", sorted(jobs.JOBS), sorted(jobs.ORDER))
check("users first", jobs.ORDER[0], "users")
check("holders last, needing both people and devices", jobs.ORDER[-1], "holders")

print("\n--- the case from the real deployment ---")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('yael@x.com','Yael','entra')")
aid = db.execute("""INSERT INTO assets (name, category, cost_cents, serial)
                    VALUES ('MacBook Air 13 M4','Laptop',0,'SN-1')""")
db.execute("""INSERT INTO devices (id, device_name, model, os, serial_number,
                                   primary_upn, asset_id, synced_at)
              VALUES ('d1','MAC-1','MacBook Air 13 M4','macOS','SN-1',
                      'yael@x.com',?,'2026-08-24T00:00:00+00:00')""", (aid,))
check("Intune knows who has it",
      db.q1("SELECT primary_upn FROM devices WHERE id='d1'")["primary_upn"], "yael@x.com")
check("but the asset does not",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"], None)

result = jobs.JOBS["holders"][1]()
check("the holders job fills it in", result["filled"], 1)
check("and now it agrees",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"], "yael@x.com")
check("running it again does nothing", jobs.JOBS["holders"][1]()["filled"], 0)

print("\n--- a disagreement is reported, never overruled ---")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('noa@x.com','Noa','entra')")
db.execute("UPDATE devices SET primary_upn='noa@x.com' WHERE id='d1'")
result = jobs.JOBS["holders"][1]()
check("nothing was filled", result["filled"], 0)
check("it is counted as a disagreement", result["disagreements"], 1)
check("and the hand-set holder stands",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"], "yael@x.com")

print("\n--- the device sync re-applies the ignore rules ---")
db.execute("""INSERT INTO devices (id, device_name, model, os, synced_at)
              VALUES ('vm1','BUILD-VM','Virtual Machine','Windows','2026-08-24T00:00:00+00:00')""")
devices.add_rule("model", "contains", "Virtual Machine")
calls = []
entra.sync_devices = lambda: (calls.append("devices"), {"devices": 2})[1]
out = jobs.JOBS["devices"][1]()
check("the sync ran", calls, ["devices"])
check("and the rules were re-applied in the same step", out["ignored"], 1)

print("\n--- the user sync re-applies the people rules ---")
people.add_rule("upn", "starts", "svc-")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('svc-a@x.com','Svc','entra')")
entra.sync = lambda: {"fetched": 3, "created": 1, "updated": 2}
out = jobs.JOBS["users"][1]()
check("a newcomer cannot walk past a rule written before them", out["ignored"], 1)

print("\n--- every run is written down, so 'did it happen' is answerable ---")
entra.is_configured = lambda: True
db.execute("DELETE FROM sync_runs")
rc = jobs.run(["users", "devices"], source="cron")
check("both succeeded", rc, 0)
runs = db.q("SELECT job, ok, source FROM sync_runs ORDER BY id")
check("both recorded", [(r["job"], r["ok"], r["source"]) for r in runs],
      [("users", 1, "cron"), ("devices", 1, "cron")])

def boom():
    raise entra.GraphError("403 Forbidden: DeviceManagementManagedDevices.Read.All")
entra.sync_devices = boom
rc = jobs.run(["devices"], source="cron")
check("a failure is a non-zero exit, so cron reports it", rc, 1)
failed = db.q1("SELECT * FROM sync_runs WHERE ok = 0 ORDER BY id DESC LIMIT 1")
check("and the reason is kept", "403 Forbidden" in failed["detail"], True)

print("\n--- the log is capped rather than growing without limit ---")
for _ in range(120):
    jobs.record("users", jobs._iso(), True, "x", "cron")
check("kept to the last 200", db.q1("SELECT COUNT(*) c FROM sync_runs")["c"] <= 200, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
