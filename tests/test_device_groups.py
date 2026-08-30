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
    raise AssertionError(f"unexpected path {path}")

entra._get_all = fake_get_all

print("--- a Dynamic Device group is told apart by its own rule ---")
check("device rule", entra.is_device_rule(TENANT["groups"][0]), True)
check("user rule, same groupTypes", entra.is_device_rule(TENANT["groups"][1]), False)
check("assigned membership", entra.is_device_rule(TENANT["groups"][2]), False)

print("\n--- the default mode looks only in those ---")
r = entra.sync_device_groups("dynamic")
check("one candidate found", r["dynamic_device_groups"], 1)
check("and only it was looked in", r["scanned"], 1)
check("one listing plus one members call", len(calls), 2)
check("it holds devices", r["device_groups"], 1)
check("the kiosk group is not found this way",
      [g["id"] for g in devices.groups_listing()], ["g-vm"])
check("it casts to device, never to user",
      any("microsoft.graph.user" in c for c in calls), False)
check("and follows nested groups",
      all("transitiveMembers" in c for c in calls if c != "/groups"), True)

print("\n--- 'every group' is the thorough pass, for Assigned membership ---")
calls.clear()
r = entra.sync_device_groups("all")
check("every group looked in", r["scanned"], 3)
check("two hold devices", r["device_groups"], 2)
check("three memberships", r["devices"], 3)
check("one call per group, plus the listing", len(calls), 4)
check("now the kiosk group is there",
      sorted(g["id"] for g in devices.groups_listing()), ["g-kiosk", "g-vm"])
check("the dynamic user group is still not kept",
      "g-people" in [g["id"] for g in devices.groups_listing()], False)

print("\n--- Edgar's mistake: a filter that matches nothing says so ---")
calls.clear()
settings.set_value("ENTRA_DEVICE_GROUP_FILTER", "startsWith(displayName, 'Virtual-')", "test")
TENANT["groups"] = []                    # what Graph returns for that filter
r = entra.sync_device_groups("dynamic")
check("nothing listed", r["groups_listed"], 0)
check("nothing scanned", r["scanned"], 0)
check("and the filter is reported back so the message can name it",
      r["filter"], "startsWith(displayName, 'Virtual-')")
check("no group was deleted for being unreachable",
      sorted(g["id"] for g in devices.groups_listing()), ["g-kiosk", "g-vm"])

settings.set_value("ENTRA_DEVICE_GROUP_FILTER", "startsWith(displayName, 'Virtual')", "test")
TENANT["groups"] = [{"id": "g-vm", "displayName": "Virtual Machines",
                     "description": "Build agents", "groupTypes": ["DynamicMembership"],
                     "membershipRule": '(device.deviceOSType -eq "Windows")'}]
r = entra.sync_device_groups("dynamic")
check("the corrected filter finds it", r["device_groups"], 1)
check("and its rule is kept for the page",
      devices.group("g-vm")["membership_rule"], '(device.deviceOSType -eq "Windows")')
check("flagged as dynamic", devices.group("g-vm")["dynamic"], 1)

print("\n--- a group that stops holding devices is dropped ---")
TENANT["groups"] = [{"id": "g-kiosk", "displayName": "Kiosks", "description": None,
                     "groupTypes": [], "membershipRule": None}]
TENANT["members"]["g-kiosk"] = []
settings.set_value("ENTRA_DEVICE_GROUP_FILTER", "", "test")
r = entra.sync_device_groups("all")
check("reported as dropped", r["dropped"], 1)
check("gone from the list", [g["id"] for g in devices.groups_listing()], ["g-vm"])
check("and its membership rows went with it",
      db.q1("SELECT COUNT(*) c FROM device_group_members WHERE group_id='g-kiosk'")["c"], 0)

print("\n--- membership drives the ignore rule ---")
for did, azure in [("d1", "aaaa-1"), ("d2", "aaaa-2"), ("d3", "cccc-9")]:
    db.execute("""INSERT INTO devices (id, device_name, model, os, azure_device_id,
                                       synced_at)
                  VALUES (?,?,'Virtual Machine','Windows',?,'2026-08-30T00:00:00+00:00')""",
               (did, did.upper(), azure))
check("device ids are stored lower-cased, so matching is case-safe",
      sorted(r["azure_device_id"] for r in
             db.q("SELECT azure_device_id FROM device_group_members WHERE group_id='g-vm'")),
      ["aaaa-1", "aaaa-2"])
check("ignoring the group", devices.add_rule("group", "eq", "g-vm", "Virtual machines"), None)
check("hides exactly its members", devices.recompute(), 2)
check("the device outside the group is untouched",
      db.q1("SELECT ignored_reason FROM devices WHERE id='d3'")["ignored_reason"], None)

print("\n--- the page shows what ITAM knows of each group ---")
row = [g for g in devices.groups_listing() if g["id"] == "g-vm"][0]
check("members from Entra", row["members"], 2)
check("matched to Intune records", row["matched"], 2)
check("and that a rule is using it", row["ignoring"], 1)

TENANT["groups"] = [{"id": "g-vm", "displayName": "Virtual Machines", "description": None,
                     "groupTypes": ["DynamicMembership"],
                     "membershipRule": '(device.deviceOSType -eq "Windows")'}]
TENANT["members"]["g-vm"] = TENANT["members"]["g-vm"] + [
    {"id": "o9", "deviceId": "DDDD-9", "displayName": "BUILD-VM-09"}]
entra.sync_device_groups("dynamic")
row = [g for g in devices.groups_listing() if g["id"] == "g-vm"][0]
check("a member Intune has not synced still shows", row["members"], 3)
check("but is not counted as known", row["matched"], 2)
members = devices.group_members("g-vm")
check("and is listed by its Entra name",
      [m["entra_name"] for m in members if not m["device_id"]], ["BUILD-VM-09"])

print("\n--- refreshing just the rules' groups is cheap ---")
calls.clear()
check("one call, for the one group a rule names", entra.refresh_ignore_groups(), 1)
check("and it did not list every group", "/groups" in calls, False)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
