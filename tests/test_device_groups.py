"""Entra groups whose members are devices.

The Groups tab syncs the PEOPLE in a group: it asks Graph for
transitiveMembers/microsoft.graph.user. A group full of virtual machines has no
user members, so it comes back empty and looks like nothing is there - which is
why a group filter was no help for leaving VMs out. Device groups are their own
thing, cast to microsoft.graph.device, kept in their own table.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, devices, entra, settings
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

# --- a Graph that answers from a fixture, so no tenant is involved --------
TENANT = {
    "groups": [
        # Exactly Edgar's group: Security, Dynamic Device, rule against device.
        {"id": "g-vm", "displayName": "Virtual Machines", "description": "Build agents",
         "groupTypes": ["DynamicMembership"],
         "membershipRule": '(device.deviceOSType -eq "Windows")'},
        # A dynamic USER group: same groupTypes, rule against user.
        {"id": "g-people", "displayName": "Israel", "description": None,
         "groupTypes": ["DynamicMembership"],
         "membershipRule": '(user.country -eq "Israel")'},
        # Assigned membership, but devices in it anyway.
        {"id": "g-kiosk", "displayName": "Kiosks", "description": None,
         "groupTypes": [], "membershipRule": None},
    ],
    "members": {
        "g-vm": [{"id": "o1", "deviceId": "AAAA-1", "displayName": "BUILD-VM-01"},
                 {"id": "o2", "deviceId": "AAAA-2", "displayName": "BUILD-VM-02"}],
        "g-people": [],                       # a user group: no device members
        "g-kiosk": [{"id": "o3", "deviceId": "BBBB-1", "displayName": "RECEPTION-IPAD"}],
    },
}
calls = []

def fake_get_all(path, params=None, base=None, advanced=False):
    calls.append(path)
    if path == "/groups":
        return list(TENANT["groups"])
    for gid, members in TENANT["members"].items():
        if path == f"/groups/{gid}/transitiveMembers/microsoft.graph.device":
            return list(members)
        if path == f"/groups/{gid}/transitiveMembers":
            return list(TENANT.get("raw", {}).get(gid, []))
    raise AssertionError(f"unexpected path {path}")


def tick(*ids):
    db.execute("UPDATE entra_groups SET sync_devices = 0")
    for gid in ids:
        db.execute("UPDATE entra_groups SET sync_devices = 1 WHERE id = ?", (gid,))

entra._get_all = fake_get_all

print("--- discovery lists every group once, and classifies it for the eye ---")
d = entra.discover_groups()
check("all three listed", d["groups"], 3)
check("one call, not one per group", calls, ["/groups"])
check("classified", {g["id"]: g["looks_like"] for g in db.q("SELECT id, looks_like FROM entra_groups")},
      {"g-vm": "device", "g-people": "user", "g-kiosk": "assigned"})
check("nothing is ticked to start with",
      db.q1("SELECT COUNT(*) c FROM entra_groups WHERE sync_devices = 1")["c"], 0)

print("\n--- nothing is fetched until a group is ticked ---")
calls.clear()
r = entra.sync_device_groups()
check("no group picked", r["picked"], 0)
check("so no calls at all", calls, [])

print("\n--- Edgar's group, ticked ---")
tick("g-vm")
calls.clear()
r = entra.sync_device_groups()
check("one group synced", r["device_groups"], 1)
check("its two devices", r["devices"], 2)
check("exactly one call", calls, ["/groups/g-vm/transitiveMembers/microsoft.graph.device"])
check("never cast to user", any("microsoft.graph.user" in c for c in calls), False)
check("the rule is kept for the page",
      devices.group("g-vm")["membership_rule"], '(device.deviceOSType -eq "Windows")')
check("and flagged dynamic", devices.group("g-vm")["dynamic"], 1)

print("\n--- an Assigned-membership group works the same way ---")
tick("g-vm", "g-kiosk")
r = entra.sync_device_groups()
check("both synced", r["device_groups"], 2)
check("three devices between them", r["devices"], 3)
check("no guessing from names or rules was involved",
      sorted(g["id"] for g in devices.groups_listing()), ["g-kiosk", "g-vm"])

print("\n--- unticking a group drops what it brought in ---")
tick("g-vm")
r = entra.sync_device_groups()
check("one dropped", r["dropped"], 1)
check("gone", [g["id"] for g in devices.groups_listing()], ["g-vm"])
check("with its membership", db.q1(
    "SELECT COUNT(*) c FROM device_group_members WHERE group_id='g-kiosk'")["c"], 0)

print("\n--- a ticked group that comes back empty is diagnosed, not shrugged off ---")
TENANT["members"]["g-vm"] = []
TENANT["raw"] = {"g-vm": [{"id": "o1", "@odata.type": "#microsoft.graph.device"},
                          {"id": "o2", "@odata.type": "#microsoft.graph.device"},
                          {"id": "o3", "@odata.type": "#microsoft.graph.device"}]}
r = entra.sync_device_groups()
check("reported as empty", len(r["empty"]), 1)
check("it asked again without the cast", r["empty"][0]["probe"]["total"], 3)
check("and says what is actually in there",
      r["empty"][0]["probe"]["kinds"], {"device": 3})
check("what it already had is kept, since an empty answer may be a permission",
      [g["id"] for g in devices.groups_listing()], ["g-vm"])

print("\n--- members returned without a deviceId are counted, not dropped ---")
TENANT["members"]["g-vm"] = [{"id": "o1", "deviceId": None, "displayName": "arieintune"},
                             {"id": "o2", "deviceId": "", "displayName": "daniel-win11"}]
TENANT.pop("raw", None)
r = entra.sync_device_groups()
check("still reported empty", len(r["empty"]), 1)
check("but it says two came back", r["empty"][0]["returned"], 2)
check("and that neither carried a deviceId", r["empty"][0]["no_device_id"], 2)

print("\n--- membership drives the ignore rule ---")
TENANT["members"]["g-vm"] = [
    {"id": "o1", "deviceId": "AAAA-1", "displayName": "arieintune"},
    {"id": "o2", "deviceId": "AAAA-2", "displayName": "daniel-win11"}]
entra.sync_device_groups()
for did, azure in [("d1", "aaaa-1"), ("d2", "aaaa-2"), ("d3", "cccc-9")]:
    db.execute("""INSERT INTO devices (id, device_name, model, os, azure_device_id,
                                       synced_at)
                  VALUES (?,?,'Virtual Machine','Windows',?,'2026-08-30T00:00:00+00:00')""",
               (did, did.upper(), azure))
check("device ids are stored lower-cased, so matching is case-safe",
      sorted(r["azure_device_id"] for r in
             db.q("SELECT azure_device_id FROM device_group_members WHERE group_id='g-vm'")),
      ["aaaa-1", "aaaa-2"])
check("ignoring the group", devices.add_rule("group", "eq", "g-vm", "Virtual Machines"), None)
check("hides exactly its members", devices.recompute(), 2)
check("the device outside the group is untouched",
      db.q1("SELECT ignored_reason FROM devices WHERE id='d3'")["ignored_reason"], None)

print("\n--- the page shows what ITAM knows of each group ---")
row = [g for g in devices.groups_listing() if g["id"] == "g-vm"][0]
check("members from Entra", row["members"], 2)
check("matched to Intune records", row["matched"], 2)
check("and that a rule is using it", row["ignoring"], 1)

TENANT["members"]["g-vm"] = TENANT["members"]["g-vm"] + [
    {"id": "o9", "deviceId": "DDDD-9", "displayName": "BUILD-VM-09"}]
entra.sync_device_groups()
row = [g for g in devices.groups_listing() if g["id"] == "g-vm"][0]
check("a member Intune has not synced still shows", row["members"], 3)
check("but is not counted as known", row["matched"], 2)
check("and is listed by its Entra name",
      [m["entra_name"] for m in devices.group_members("g-vm") if not m["device_id"]],
      ["BUILD-VM-09"])

print("\n--- refreshing just the rules' groups is cheap ---")
calls.clear()
check("one call, for the one group a rule names", entra.refresh_ignore_groups(), 1)
check("and it did not list every group", "/groups" in calls, False)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
