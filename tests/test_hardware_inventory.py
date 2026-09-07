"""Windows CPU, RAM and disk, from Intune's Device inventory.

macOS reports a spec through a custom attribute script. Windows does not - it
reports through Device inventory, which is a beta and (at the time of writing)
undocumented Graph endpoint. So nothing here assumes a category id or a property
name: the categories are read back from the tenant and whatever properties come
with them are stored. Three Graph details have already been wrong by assumption.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "HwTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
from app import db, entra, settings
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

aid = db.execute("INSERT INTO assets (name, category, cost_cents, serial) "
                 "VALUES ('ThinkPad X1','Laptop',0,'SN-W1')")
db.execute("""INSERT INTO devices (id, device_name, model, os, serial_number,
                                   asset_id, synced_at)
              VALUES ('w1','WIN-1','21CB','Windows','SN-W1',?,
                      '2026-09-06T00:00:00+00:00')""", (aid,))
db.execute("""INSERT INTO devices (id, device_name, os, ignored_reason, synced_at)
              VALUES ('vm1','BUILD-VM','Windows','Model contains "Virtual"',
                      '2026-09-06T00:00:00+00:00')""")

# The shape Intune returns, as far as anyone outside Microsoft can tell.
CATEGORIES = [{"id": "Cpu", "displayName": "CPU"},
              {"id": "MemoryInfo", "displayName": "Memory Info"},
              {"id": "DiskDrive", "displayName": "Disk Drive"},
              {"id": "Battery", "displayName": "Battery"}]
DETAIL = {
    "Cpu": {"instances": [{"properties": [
        {"displayName": "Name", "value": "Intel(R) Core(TM) Ultra 7 165U"},
        {"displayName": "Number of cores", "value": 12},
        {"displayName": "Nothing", "value": None}]}]},
    "MemoryInfo": {"instances": [{"properties": [
        {"displayName": "Total physical memory (GB)", "value": 32}]}]},
    "DiskDrive": {"instances": [
        {"properties": [{"displayName": "Size (GB)", "value": 512},
                        {"displayName": "Model", "value": "SAMSUNG MZVL2512"}]},
        {"properties": [{"displayName": "Size (GB)", "value": 1024}]}]},
    "Battery": {"instances": [{"properties": [
        {"displayName": "Health", "value": {"value": "94"}}]}]},
}
calls = []
entra._get_all = lambda path, params=None, base=None, advanced=False: (
    calls.append(path), list(CATEGORIES) if "deviceInventories" in path else [])[1]
entra._get_one = lambda path, params=None, base=None: (
    calls.append(path),
    DETAIL[[k for k in DETAIL if f"('{k}')" in path][0]])[1]

print("--- everything Intune offers, stored as device attributes ---")
r = entra.sync_hardware_inventory()
check("one device read (the ignored VM is skipped)", r["devices"], 1)
check("the categories are reported back, not assumed",
      r["categories"], ["Battery", "CPU", "Disk Drive", "Memory Info"])

got = {a["name"]: a["value"] for a in
       db.q("SELECT name, value FROM device_attributes WHERE device_id='w1'")}
check("CPU model, which is the one that matters",
      got.get("CPU / Name"), "Intel(R) Core(TM) Ultra 7 165U")
check("RAM total", got.get("Memory Info / Total physical memory (GB)"), "32")
check("a single-instance category is not numbered", "CPU / Name" in got, True)
check("two disks are numbered apart",
      (got.get("Disk Drive 1 / Size (GB)"), got.get("Disk Drive 2 / Size (GB)")),
      ("512", "1024"))
check("a property with no value is dropped, not stored empty",
      "CPU / Nothing" in got, False)
check("a nested value is unwrapped", got.get("Battery / Health"), "94")
check("the ignored VM got nothing",
      db.q1("SELECT COUNT(*) c FROM device_attributes WHERE device_id='vm1'")["c"], 0)

print("\n--- re-running updates in place rather than duplicating ---")
before = db.q1("SELECT COUNT(*) c FROM device_attributes")["c"]
DETAIL["MemoryInfo"]["instances"][0]["properties"][0]["value"] = 64
entra.sync_hardware_inventory()
check("same number of rows", db.q1("SELECT COUNT(*) c FROM device_attributes")["c"], before)
check("with the new value",
      db.q1("SELECT value FROM device_attributes WHERE device_id='w1' "
            "AND name='Memory Info / Total physical memory (GB)'")["value"], "64")

print("\n--- the attribute filter keeps the call count down ---")
settings.set_value("INTUNE_ATTRIBUTE_FILTER", "CPU, Memory*", "test")
calls.clear()
entra.sync_hardware_inventory()
fetched = [c for c in calls if "deviceInventories('" in c]
check("only the two wanted categories were fetched", len(fetched), 2)
check("CPU among them", any("('Cpu')" in c for c in fetched), True)
check("and not the battery", any("('Battery')" in c for c in fetched), False)
settings.set_value("INTUNE_ATTRIBUTE_FILTER", "", "test")

print("\n--- a refusal is a FAILURE, not a job that succeeded with nothing ---")
# It used to return the error in a field, so the nightly log printed
# "OK  Intune hardware inventory: devices=0, stored=0, unavailable=403..."
# and cron exited zero on a total failure.
def refuse(path, params=None, base=None, advanced=False):
    calls.append(path)
    raise entra.GraphError("403 Forbidden from Graph: refused")
entra._get_all = refuse
db.execute("""INSERT INTO devices (id, device_name, os, synced_at)
              VALUES ('w2','WIN-2','Windows','2026-09-06T00:00:00+00:00')""")
calls.clear()
raised = None
try:
    entra.sync_hardware_inventory()
except entra.GraphError as exc:
    raised = str(exc)
check("it raises", raised is not None, True)
check("saying the inventory is not readable",
      "not readable" in (raised or ""), True)
check("and it stopped after the first refusal rather than asking twice",
      len(calls), 1)

entra.is_configured = lambda: True
from app import jobs                               # noqa: E402
check("so the job reports FAILED and exits non-zero",
      jobs.run(["hardware"], source="test"), 1)
check("and the run is recorded as not ok",
      db.q1("SELECT ok FROM sync_runs WHERE job='hardware' ORDER BY id DESC LIMIT 1")["ok"], 0)

print("\n--- the 403 hint names nothing it cannot vouch for ---")
class Refused:
    status_code = 403
    def json(self):
        return {"error": {"code": "Forbidden", "message": "An error has occurred"}}

msg = entra._explain(Refused(), "/deviceManagement/managedDevices('x')/deviceInventories")
check("it does not send you after a permission you already have",
      "DeviceManagementManagedDevices.Read.All" in msg, False)
check("it says the permission is undocumented", "not documented" in msg, True)
check("and raises the real possibility",
      "application (client-credentials) token" in msg, True)
plain = entra._explain(Refused(), "/deviceManagement/managedDevices")
check("while the resource itself still names its permission",
      "DeviceManagementManagedDevices.Read.All" in plain, True)

print("\n--- total RAM, without Device inventory at all ---")
entra._get_all = lambda path, params=None, base=None, advanced=False: [
    {"id": "w1", "physicalMemoryInBytes": 34359738368},
    {"id": "w2", "physicalMemoryInBytes": 0},
    {"id": "ghost", "physicalMemoryInBytes": 17179869184}]
r = entra.sync_physical_memory()
check("stored for the one that reported", r["stored"], 1)
check("a 0 is not stored as 0GB", r["reported_zero"], 1)
check("a device ITAM does not have is counted, not invented", r["not_in_itam"], 1)
check("and it lands on the device row, in bytes",
      db.q1("SELECT memory_total FROM devices WHERE id='w1'")["memory_total"],
      34359738368)

print("\n--- the person's card links the asset and shows its spec ---")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('yael@x.com','Yael','entra')")
db.execute("UPDATE assets SET assigned_upn='yael@x.com' WHERE id=?", (aid,))
db.execute("UPDATE devices SET storage_total = 512110190592 WHERE id = 'w1'")
from fastapi.testclient import TestClient          # noqa: E402
from app import main                               # noqa: E402
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "HwTest!2345"},
                follow_redirects=False)
    page = client.get("/users/yael@x.com").text
    check("the asset is a link", f'href="/assets/{aid}"' in page, True)
    row = page.split(f'href="/assets/{aid}"', 1)[1].split("</td>", 1)[0]
    # Whatever Intune collected, under the name it collected it under. The card
    # does not second-guess the names: an earlier version matched them against
    # "cpu|memory|ram" and silently dropped every attribute a Mac reports.
    stored = db.q("SELECT name, value FROM device_attributes WHERE device_id='w1' "
                  "ORDER BY name")
    missing = [r["name"] for r in stored
               if f"<b>{r['name']}</b> {r['value']}" not in row]
    check("every attribute collected is on the card, under its own name",
          missing, [])
    check("and nothing ITAM worked out itself alongside them",
          "<b>SSD</b>" in row, False)

print("\n--- a Mac shows its own tag, and only its own tag ---")
from app import devices as dev                      # noqa: E402
db.execute("DELETE FROM device_attributes")
db.execute("UPDATE devices SET storage_total = 512110190592, memory_total = 25769803776 "
           "WHERE id = 'w1'")
db.execute("""INSERT INTO device_attributes (device_id, name, value, collected_at)
              VALUES ('w1','Mac HW TAG','MBA-13.6\"-M5/24/512G-10CPU-10GPU',
                      '2026-09-07T00:00:00+00:00')""")
check("the tag, exactly as the script reported it",
      dev.specs_for("yael@x.com")[aid],
      [("Mac HW TAG", 'MBA-13.6"-M5/24/512G-10CPU-10GPU')])
check("no SSD line beside it - the tag already says 512G",
      any(n == "SSD" for n, _ in dev.specs_for("yael@x.com")[aid]), False)

print("\n--- a Windows machine, which has no script, shows RAM and disk ---")
db.execute("DELETE FROM device_attributes")
check("from the fields Graph carries for every managed device",
      dev.specs_for("yael@x.com")[aid], [("RAM", "26GB"), ("SSD", "512GB")])
check("and the devices list shows the same, not a blank cell",
      dev.all_specs()["w1"], [("RAM", "26GB"), ("SSD", "512GB")])
db.execute("UPDATE devices SET memory_total = NULL WHERE id = 'w1'")
check("RAM is left out rather than shown as 0GB when Intune reports none",
      dev.specs_for("yael@x.com")[aid], [("SSD", "512GB")])

print("\n--- imported or scripted CPU/RAM/Disk win over the fallback ---")
for name, value in [("CPU", "Intel(R) Core(TM) Ultra 7 165U"), ("RAM", "32GB"),
                    ("Disk", "1TB")]:
    db.execute("""INSERT INTO device_attributes (device_id, name, value, collected_at)
                  VALUES ('w1',?,?,'2026-09-07T00:00:00+00:00')""", (name, value))
check("all three, verbatim, no arithmetic",
      sorted(dev.specs_for("yael@x.com")[aid]),
      [("CPU", "Intel(R) Core(TM) Ultra 7 165U"), ("Disk", "1TB"), ("RAM", "32GB")])

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
