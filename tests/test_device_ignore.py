"""Ignoring devices Intune manages but nobody holds.

Virtual machines are the case that prompted this: they live in an Entra group,
nobody carries one, and each one becoming an asset makes the estate look bigger
than it is. The properties that matter are that an ignored device never becomes
an asset, that it is still on record so you can see what is hidden and why, and
that un-ignoring is instant.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, devices
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

def device(did, name, model="MacBook Air 13 M4", manufacturer="Apple",
           os_name="macOS", azure_id=None):
    db.execute("""INSERT INTO devices (id, device_name, model, manufacturer, os,
                                       azure_device_id, serial_number, synced_at)
                  VALUES (?,?,?,?,?,?,?,'2026-08-30T00:00:00+00:00')""",
               (did, name, model, manufacturer, os_name, azure_id, "SN-" + did))

device("d1", "MAC-YAEL")
device("d2", "MAC-NOA")
device("vm1", "BUILD-VM-01", model="Virtual Machine", manufacturer="VMware, Inc.",
       os_name="Windows", azure_id="aaaa-1")
device("vm2", "TEST-VM-02", model="Virtual Machine", manufacturer="VMware, Inc.",
       os_name="Windows", azure_id="aaaa-2")
device("kiosk", "RECEPTION-IPAD", model="iPad", os_name="iOS", azure_id="bbbb-1")

print("--- with no rules, nothing is ignored ---")
check("recompute hides none", devices.recompute(), 0)
check("counts agree", devices.counts()["ignored"], 0)

print("--- your case: a group holding the virtual machines ---")
db.execute("INSERT INTO groups (id, display_name, member_count) VALUES ('g-vm','Virtual machines',2)")
# Refreshed from Graph on every device sync; seeded directly here.
for azure_id in ("aaaa-1", "aaaa-2"):
    db.execute("INSERT INTO device_group_members (group_id, azure_device_id) VALUES ('g-vm',?)",
               (azure_id,))
check("adding the rule", devices.add_rule("group", "eq", "g-vm", "Virtual machines"), None)
check("both VMs hidden", devices.recompute(), 2)
check("by id", sorted(devices.ignored_ids()), ["vm1", "vm2"])
check("and it says which rule",
      db.q1("SELECT ignored_reason FROM devices WHERE id='vm1'")["ignored_reason"],
      "In Entra group “Virtual machines”")
check("the real laptops are untouched",
      db.q1("SELECT ignored_reason FROM devices WHERE id='d1'")["ignored_reason"], None)

print("\n--- a device with no Entra id is never swept up by a group rule ---")
device("noazure", "OLD-ENROLMENT", azure_id=None)
devices.recompute()
check("left visible",
      db.q1("SELECT ignored_reason FROM devices WHERE id='noazure'")["ignored_reason"], None)

print("\n--- or any other group/device ---")
check("by model", devices.add_rule("model", "contains", "iPad"), None)
check("now three hidden", devices.recompute(), 3)
check("one specific device", devices.add_rule("device", "eq", "d2", "MAC-NOA"), None)
check("four hidden", devices.recompute(), 4)
check("only Yael's laptop and the old enrolment are left",
      sorted(r["id"] for r in db.q("SELECT id FROM devices WHERE ignored_reason IS NULL")),
      ["d1", "noazure"])
check("a rule matching nothing is still allowed",
      devices.add_rule("manufacturer", "eq", "Nobody Ltd"), None)
check("and hides nothing", devices.recompute(), 4)

print("\n--- rules are described, deduplicated, and validated ---")
check("a duplicate is refused",
      devices.add_rule("group", "eq", "g-vm") is not None, True)
check("an unknown field", devices.add_rule("colour", "eq", "red") is not None, True)
check("an unknown match", devices.add_rule("model", "sounds-like", "x") is not None, True)
check("an empty value", devices.add_rule("model", "eq", "  ") is not None, True)
check("descriptions read as English",
      sorted(devices.describe(r) for r in devices.rules()),
      ['In Entra group “Virtual machines”',
       'Manufacturer is exactly “Nobody Ltd”',
       'Model contains “iPad”',
       'The device “MAC-NOA”'])

print("\n--- un-ignoring is instant, with no re-sync ---")
rule = [r for r in devices.rules() if r["field"] == "group"][0]
devices.delete_rule(rule["id"])
check("the VMs come back", devices.recompute(), 2)
check("and they were never lost", db.q1("SELECT COUNT(*) c FROM devices")["c"], 6)

print("\n--- an ignored device is kept out of the query the app runs ---")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import _device_query                     # noqa: E402
sql, params = _device_query("", "", "")
check("the default query hides them",
      sorted(r["id"] for r in db.q(sql, params)), ["d1", "noazure", "vm1", "vm2"])
sql, params = _device_query("", "", "", include_ignored=True)
check("and can be asked for them", len(db.q(sql, params)), 6)
sql, params = _device_query("", "", "unlinked")
check("bulk-create never sees them either",
      "d2" in [r["id"] for r in db.q(sql, params)], False)

print("\n--- an asset made before the rule existed keeps its record ---")
aid = db.execute("INSERT INTO assets (name, category, cost_cents, serial) "
                 "VALUES ('Virtual Machine','Other',0,'SN-vm1')")
db.execute("UPDATE devices SET asset_id = ? WHERE id = 'vm1'", (aid,))
devices.add_rule("model", "contains", "Virtual Machine")
devices.recompute()
check("the pairing is reported", devices.counts()["ignored_with_asset"], 1)
check("unlinking touches one", devices.unlink_ignored(), 1)
check("the link is gone",
      db.q1("SELECT asset_id FROM devices WHERE id='vm1'")["asset_id"], None)
check("but the asset is not deleted",
      db.q1("SELECT COUNT(*) c FROM assets WHERE id = ?", (aid,))["c"], 1)

print("\n--- and the holder check skips them ---")
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('yael@x.com','Yael','entra')")
a2 = db.execute("INSERT INTO assets (name, category, cost_cents, serial) "
                "VALUES ('MacBook Air','Laptop',0,'SN-hidden')")
db.execute("UPDATE devices SET asset_id = ?, primary_upn = 'yael@x.com' WHERE id = 'd2'", (a2,))
check("d2 is ignored", bool(db.q1("SELECT ignored_reason FROM devices WHERE id='d2'")["ignored_reason"]), True)
check("so it is not offered as fillable",
      [r["id"] for r in devices.holder_gap()["fillable"]], [])
check("and filling in does nothing", devices.fill_holders_from_intune(), 0)

print("\n--- every route into asset creation refuses an ignored device ---")
# The bulk path filters in SQL; the single-device route is only reachable while
# ignored devices are on screen, but that is exactly when it would be used by
# mistake. Both are checked, because one of them was wrong.
import warnings; warnings.filterwarnings("ignore")     # noqa: E402
os.environ["ITAM_ADMIN_PASSWORD"] = "IgnoreTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
from fastapi.testclient import TestClient               # noqa: E402
from app import main                                    # noqa: E402

with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "IgnoreTest!2345"},
                follow_redirects=False)
    before = db.q1("SELECT COUNT(*) c FROM assets")["c"]
    loc = client.post("/settings/devices/vm2/create-asset",
                      follow_redirects=False).headers.get("location", "")
    check("the single-device route refuses", "is+ignored" in loc, True)
    check("and made nothing", db.q1("SELECT COUNT(*) c FROM assets")["c"], before)
    check("the device is still unlinked",
          db.q1("SELECT asset_id FROM devices WHERE id='vm2'")["asset_id"], None)
    def links(where):
        return {r["id"]: r["asset_id"] for r in
                db.q(f"SELECT id, asset_id FROM devices WHERE {where}")}

    ignored_before = links("ignored_reason IS NOT NULL")
    visible_unlinked = sorted(links("ignored_reason IS NULL AND asset_id IS NULL"))
    client.post("/settings/devices/create-assets", data={}, follow_redirects=False)
    check("no ignored device's link changed",
          links("ignored_reason IS NOT NULL"), ignored_before)
    check("while every visible unlinked one was served",
          sorted(k for k, v in links("ignored_reason IS NULL").items() if v),
          visible_unlinked)

print("\n--- the summary line counts what is on the page, not what is hidden ---")
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "IgnoreTest!2345"},
                follow_redirects=False)
    page = client.get("/settings/devices").text
    tally = page.split('class="tally"', 1)[1].split("</p>", 1)[0]
    check("the custom-attribute widget is gone", "Custom attributes</span>" in page, False)
    check("the OS breakdown has left this page", "By OS" in page, False)
    visible = db.q1("SELECT COUNT(*) c FROM devices WHERE ignored_reason IS NULL")["c"]
    check("the managed count excludes ignored devices",
          f"Managed devices <strong>{visible}</strong>" in tally, True)

print("\n--- and an ignored device is not counted on the Assets overview ---")
# The OS counts moved to Assets, where they are counts of kit rather than of
# whatever happens to have enrolled. An ignored device must not appear there
# either, or hiding a VM would still leave it in the Windows figure.
from app import devices as dev                     # noqa: E402
laptops = next(w for w in dev.overview() if w["label"] == "Laptops")
families = {b["name"]: b["count"] for b in laptops["breakdown"]}
for family, os_names in (("Windows", ("windows",)), ("macOS", ("macos", "mac os"))):
    expected = db.q1(
        """SELECT COUNT(*) c FROM devices d JOIN assets a ON a.id = d.asset_id
           WHERE d.ignored_reason IS NULL AND a.category = 'Laptop'
             AND LOWER(COALESCE(d.os,'')) IN (%s)"""
        % ",".join("?" * len(os_names)), os_names)["c"]
    check(f"{family} counts only what is not ignored",
          families.get(family, 0), expected)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
