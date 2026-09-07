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
check("nothing was said to be unavailable", r["unavailable"], None)

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

print("\n--- a tenant without Device inventory is told so, once ---")
def refuse(path, params=None, base=None, advanced=False):
    calls.append(path)
    raise entra.GraphError("404 from Graph (ResourceNotFound): deviceInventories")
entra._get_all = refuse
db.execute("""INSERT INTO devices (id, device_name, os, synced_at)
              VALUES ('w2','WIN-2','Windows','2026-09-06T00:00:00+00:00')""")
calls.clear()
r = entra.sync_hardware_inventory()
check("reported as unavailable", "ResourceNotFound" in (r["unavailable"] or ""), True)
check("nothing claimed as stored", r["stored"], 0)
check("and it stopped after the first refusal rather than asking 97 times",
      len(calls), 1)

print("\n--- the person's card links the asset and shows its spec ---")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('yael@x.com','Yael','entra')")
db.execute("UPDATE assets SET assigned_upn='yael@x.com' WHERE id=?", (aid,))
from fastapi.testclient import TestClient          # noqa: E402
from app import main                               # noqa: E402
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "HwTest!2345"},
                follow_redirects=False)
    page = client.get("/users/yael@x.com").text
    check("the asset is a link", f'href="/assets/{aid}"' in page, True)
    check("the CPU model is on the card",
          "Intel(R) Core(TM) Ultra 7 165U" in page, True)
    check("so is the RAM", "Memory Info / Total physical memory (GB)" in page, True)
    check("and the device name it came from", "WIN-1" in page, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
