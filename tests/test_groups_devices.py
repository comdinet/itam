import os, tempfile, sys, tempfile, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(ENTRA_TENANT_ID="t", ENTRA_CLIENT_ID="c", ENTRA_CLIENT_SECRET="s")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, entra, rules
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

# --- users the groups will reference -----------------------------------
for upn, name in [("ada@x.com","Ada"), ("grace@x.com","Grace"), ("hedy@x.com","Hedy")]:
    db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))

PAGES = {}
requested = []

class FakeResp:
    status_code = 200          # the app now inspects this instead of raising
    def __init__(self, body): self._b = body
    def raise_for_status(self): pass
    def json(self): return self._b

class FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, headers=None, params=None):
        requested.append(url)
        key = url
        if key not in PAGES:
            raise AssertionError(f"unexpected URL: {url}")
        return FakeResp(PAGES[key])

entra._token = lambda: "tok"
entra.httpx.Client = FakeClient
G = "https://graph.microsoft.com/v1.0"
B = "https://graph.microsoft.com/beta"

# --- groups, with paging on the member list ----------------------------
PAGES[f"{G}/groups"] = {"value": [
    {"id": "g-design", "displayName": "Design", "description": "Design team"},
    {"id": "g-eng", "displayName": "Engineering", "description": None},
]}
PAGES[f"{G}/groups/g-design/members"] = {
    "value": [{"id":"1","userPrincipalName":"Hedy@X.com"},
              {"id":"2","userPrincipalName":"ada@x.com"},
              {"id":"3"}],                                  # nested group: no UPN
    "@odata.nextLink": f"{G}/groups/g-design/members?$skip=3"}
PAGES[f"{G}/groups/g-design/members?$skip=3"] = {
    "value": [{"id":"4","userPrincipalName":"ghost@x.com"}]}  # not synced as a user
PAGES[f"{G}/groups/g-eng/members"] = {
    "value": [{"id":"5","userPrincipalName":"grace@x.com"}]}

r = entra.sync_groups()
print("\n--- groups ---")
check("groups fetched", r["groups"], 2)
check("created", r["created"], 2)
check("members linked", r["members_linked"], 3)      # hedy, ada, grace
check("members unknown", r["members_unknown"], 1)    # ghost@x.com
check("member paging followed", f"{G}/groups/g-design/members?$skip=3" in requested, True)
check("UPN lowercased in membership",
      bool(db.q1("SELECT 1 FROM group_members WHERE upn='hedy@x.com'")), True)
check("nested group without UPN ignored",
      db.q1("SELECT COUNT(*) c FROM group_members WHERE group_id='g-design'")["c"], 2)
check("member_count records Entra's number, not ours",
      db.q1("SELECT member_count FROM groups WHERE id='g-design'")["member_count"], 3)

# re-sync with Hedy removed: membership must not be stale
PAGES[f"{G}/groups/g-design/members"] = {"value": [{"id":"2","userPrincipalName":"ada@x.com"}]}
del PAGES[f"{G}/groups/g-design/members?$skip=3"]
r2 = entra.sync_groups()
check("re-sync updates not duplicates", r2["created"], 0)
check("removed member is dropped",
      bool(db.q1("SELECT 1 FROM group_members WHERE group_id='g-design' AND upn='hedy@x.com'")), False)
# The reserved "Everyone" pseudo-group also lives in this table, so count
# only groups that actually came from Entra.
check("no duplicate groups",
      db.q1("SELECT COUNT(*) c FROM groups WHERE id != ?", (db.ALL_USERS_GROUP,))["c"], 2)
check("reserved Everyone group exists",
      bool(db.q1("SELECT 1 FROM groups WHERE id = ?", (db.ALL_USERS_GROUP,))), True)
check("group sync does not clobber it",
      db.q1("SELECT display_name FROM groups WHERE id = ?", (db.ALL_USERS_GROUP,))["display_name"],
      db.ALL_USERS_LABEL)

# --- Intune devices ----------------------------------------------------
db.execute("INSERT INTO assets (name, category, cost_cents, serial) VALUES ('MacBook Pro','Laptop',249900,'C02ADA1')")
PAGES[f"{G}/deviceManagement/managedDevices"] = {"value": [
    {"id":"d1","deviceName":"ADA-MBP","serialNumber":"C02ADA1","manufacturer":"Apple",
     "model":"MacBook Pro 14","operatingSystem":"macOS","osVersion":"15.3",
     "userPrincipalName":"Ada@X.com","complianceState":"compliant",
     "enrolledDateTime":"2026-01-05T10:00:00Z","lastSyncDateTime":"2026-08-24T06:00:00Z",
     "totalStorageSpaceInBytes":994662584320,"freeStorageSpaceInBytes":412316860416},
    {"id":"d2","deviceName":"GRACE-PC","serialNumber":"PF-GRACE","manufacturer":"Lenovo",
     "model":"ThinkPad X1","operatingSystem":"Windows","osVersion":"11",
     "userPrincipalName":"grace@x.com","complianceState":"noncompliant",
     "totalStorageSpaceInBytes":None,"freeStorageSpaceInBytes":"not-a-number"},
]}
d = entra.sync_devices()
print("\n--- devices ---")
check("devices fetched", d["devices"], 2)
check("linked to asset by serial", d["linked_to_assets"], 1)
check("device UPN lowercased",
      db.q1("SELECT primary_upn FROM devices WHERE id='d1'")["primary_upn"], "ada@x.com")
check("asset link resolved",
      bool(db.q1("SELECT asset_id FROM devices WHERE id='d1'")["asset_id"]), True)
check("unmatched serial leaves asset_id null",
      db.q1("SELECT asset_id FROM devices WHERE id='d2'")["asset_id"], None)
check("bad storage number becomes NULL, not a crash",
      db.q1("SELECT storage_free FROM devices WHERE id='d2'")["storage_free"], None)
check("storage parsed when valid",
      db.q1("SELECT storage_total FROM devices WHERE id='d1'")["storage_total"], 994662584320)

# a hand-made link must survive a re-sync that finds no serial match
db.execute("INSERT INTO assets (name, category, cost_cents) VALUES ('Grace laptop','Laptop',150000)")
aid = db.q1("SELECT id FROM assets WHERE name='Grace laptop'")["id"]
db.execute("UPDATE devices SET asset_id=? WHERE id='d2'", (aid,))
entra.sync_devices()
check("manual asset link preserved on re-sync",
      db.q1("SELECT asset_id FROM devices WHERE id='d2'")["asset_id"], aid)

# --- macOS custom attributes ------------------------------------------
PAGES[f"{B}/deviceManagement/deviceCustomAttributeShellScripts"] = {"value": [
    {"id":"s1","displayName":"CPU and RAM"},
    {"id":"s2","displayName":"Warranty"},
]}
PAGES[f"{B}/deviceManagement/deviceCustomAttributeShellScripts/s1/deviceRunStates"] = {"value": [
    {"managedDevice":{"id":"d1","deviceName":"ADA-MBP"},
     "resultMessage":"Apple M4 Pro / 36 GB","lastStateUpdateDateTime":"2026-08-24T05:00:00Z"},
    {"managedDevice":{"id":"unknown-dev"},"resultMessage":"whatever"},   # device we don't have
    {"managedDevice":{"id":"d1"},"resultMessage":"   "},                  # empty result
]}
PAGES[f"{B}/deviceManagement/deviceCustomAttributeShellScripts/s2/deviceRunStates"] = {"value": [
    {"managedDeviceId":"d1","resultMessage":"Expires 2027-04-01"},        # no expand
]}
a = entra.sync_custom_attributes()
print("\n--- custom attributes ---")
check("scripts read", a["scripts"], 2)
check("values stored", a["attributes_stored"], 2)
check("skipped (unknown device + empty)", a["skipped"], 2)
check("beta endpoint used",
      any("graph.microsoft.com/beta" in u for u in requested), True)
rows = {r["name"]: r["value"] for r in db.q("SELECT name, value FROM device_attributes WHERE device_id='d1'")}
check("CPU/RAM attribute merged onto device", rows.get("CPU and RAM"), "Apple M4 Pro / 36 GB")
check("second attribute via managedDeviceId", rows.get("Warranty"), "Expires 2027-04-01")

# re-running updates in place
PAGES[f"{B}/deviceManagement/deviceCustomAttributeShellScripts/s1/deviceRunStates"]["value"][0]["resultMessage"] = "Apple M4 Max / 64 GB"
entra.sync_custom_attributes()
check("attribute updated not duplicated",
      db.q1("SELECT value FROM device_attributes WHERE device_id='d1' AND name='CPU and RAM'")["value"],
      "Apple M4 Max / 64 GB")
check("no duplicate attribute rows",
      db.q1("SELECT COUNT(*) c FROM device_attributes WHERE device_id='d1'")["c"], 2)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
