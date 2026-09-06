"""Syncing only the devices in a chosen Entra group.

Intune's managedDevices cannot be filtered by group membership at the API - its
$filter supports very little - so the whole list comes down and is narrowed
here against the membership the device-group sync recorded.

The failure worth guarding is the quiet one: a scope group whose members were
never fetched would match nothing and read as a sync that simply found no
devices.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, entra
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

FLEET = [
    {"id": "m1", "deviceName": "MAC-1", "azureADDeviceId": "AAAA-1",
     "operatingSystem": "macOS", "serialNumber": "SN-1", "model": "Mac17,4"},
    {"id": "m2", "deviceName": "MAC-2", "azureADDeviceId": "AAAA-2",
     "operatingSystem": "macOS", "serialNumber": "SN-2", "model": "Mac17,4"},
    {"id": "vm1", "deviceName": "BUILD-VM", "azureADDeviceId": "BBBB-1",
     "operatingSystem": "Windows", "serialNumber": "SN-3", "model": "VMware7,1"},
    {"id": "old", "deviceName": "NO-AZURE-ID", "azureADDeviceId": None,
     "operatingSystem": "Windows", "serialNumber": "SN-4", "model": "Latitude"},
]
entra._get_all = lambda path, params=None, base=None, advanced=False: (
    list(FLEET) if "managedDevices" in path else [])
entra.refresh_ignore_groups = lambda: 0

db.execute("""INSERT INTO entra_groups (id, display_name, looks_like, discovered_at)
              VALUES ('g-phys','Physical MDM devices','device','2026-09-06T00:00:00+00:00')""")

print("--- with no scope, everything Intune manages comes in ---")
r = entra.sync_devices()
check("all four", r["devices"], 4)
check("none left out", r["out_of_scope"], 0)

print("\n--- a scope group with no membership refuses, rather than syncing none ---")
db.execute("UPDATE entra_groups SET scope_devices = 1 WHERE id = 'g-phys'")
check("it is scoped", entra.scope_group_ids(), ["g-phys"])
try:
    entra.sync_devices()
    check("refused", False, True)
except entra.GraphError as exc:
    check("refused with a reason", "no members recorded" in str(exc), True)
    check("and says what to do", "Sync device groups first" in str(exc), True)
check("nothing was wiped by the refusal",
      db.q1("SELECT COUNT(*) c FROM devices")["c"], 4)

print("\n--- with membership, only that group's devices come in ---")
for azure_id, name in [("aaaa-1", "MAC-1"), ("aaaa-2", "MAC-2")]:
    db.execute("""INSERT INTO device_group_members (group_id, azure_device_id, device_name)
                  VALUES ('g-phys',?,?)""", (azure_id, name))
db.execute("DELETE FROM devices")
r = entra.sync_devices()
check("two kept", r["devices"], 2)
check("two left out and counted", r["out_of_scope"], 2)
check("the right two", sorted(x["id"] for x in db.q("SELECT id FROM devices")), ["m1", "m2"])
check("the VM never arrived", db.q1("SELECT COUNT(*) c FROM devices WHERE id='vm1'")["c"], 0)
check("nor the one with no Entra id, which cannot be matched to a group",
      db.q1("SELECT COUNT(*) c FROM devices WHERE id='old'")["c"], 0)

print("\n--- membership is matched case-insensitively on the Entra id ---")
# Intune returns AAAA-1; membership was stored lower-cased by its own sync.
check("uppercase from Intune still matched",
      db.q1("SELECT azure_device_id FROM devices WHERE id='m1'")["azure_device_id"],
      "aaaa-1")

print("\n--- a device already here that falls out of scope is kept, not deleted ---")
db.execute("""INSERT INTO devices (id, device_name, azure_device_id, synced_at)
              VALUES ('stray','STRAY','cccc-9','2026-09-01T00:00:00+00:00')""")
r = entra.sync_devices()
check("still here after a scoped sync",
      db.q1("SELECT COUNT(*) c FROM devices WHERE id='stray'")["c"], 1)
check("but it was not refreshed",
      db.q1("SELECT synced_at FROM devices WHERE id='stray'")["synced_at"],
      "2026-09-01T00:00:00+00:00")

print("\n--- clearing the scope brings the rest back ---")
db.execute("UPDATE entra_groups SET scope_devices = 0")
r = entra.sync_devices()
check("everything again", r["devices"], 4)
check("nothing left out", r["out_of_scope"], 0)

print("\n--- a scope group's membership is fetched even if it is not tracked ---")
db.execute("UPDATE entra_groups SET scope_devices = 1, sync_devices = 0 WHERE id = 'g-phys'")
asked = []
entra.fetch_group_devices = lambda gid: (asked.append(gid), {
    "devices": [{"azure_device_id": "aaaa-1", "device_name": "MAC-1"}],
    "returned": 1, "no_device_id": 0, "looked_up": 0,
    "lookup_error": None, "probe": None})[1]
entra._get_all = lambda path, params=None, base=None, advanced=False: []
entra.sync_device_groups()
check("its members were fetched", asked, ["g-phys"])
check("but it is not listed as a tracked device group",
      db.q1("SELECT COUNT(*) c FROM device_groups WHERE id='g-phys'")["c"], 0)

print("\n--- the rule the page hands out names the right property ---")
# device.managementType is documented as "for mobile devices" and does not match
# managed Macs or PCs, which is exactly how this went wrong the first time.
import pathlib                                    # noqa: E402
page = (pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
        / "settings_devices.html").read_text()
check("uses deviceManagementAppId", "device.deviceManagementAppId" in page, True)
check("with Intune's application id",
      "0000000a-0000-0000-c000-000000000000" in page, True)
check("and never offers managementType as the way to do it",
      'device.managementType -eq' in page, False)
check("it warns about that one by name", "device.managementType" in page, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
