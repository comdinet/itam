import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(ENTRA_TENANT_ID="t", ENTRA_CLIENT_ID="c", ENTRA_CLIENT_SECRET="s")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, entra, settings
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

print("--- matching ---")
check("no filter matches everything", entra.attribute_wanted("Anything", []), True)
pats = ["CPU and RAM", "Warranty"]
check("exact name matches", entra.attribute_wanted("CPU and RAM", pats), True)
check("case does not matter", entra.attribute_wanted("cpu AND ram", pats), True)
check("surrounding space tolerated", entra.attribute_wanted("  Warranty ", pats), True)
check("unlisted name refused", entra.attribute_wanted("Disk Encryption", pats), False)
check("substring alone is not a match", entra.attribute_wanted("CPU", pats), False)
wild = ["CPU*", "*Encryption"]
check("prefix wildcard", entra.attribute_wanted("CPU and RAM", wild), True)
check("suffix wildcard", entra.attribute_wanted("Disk Encryption", wild), True)
check("wildcard still refuses others", entra.attribute_wanted("Warranty", wild), False)
check("empty name with a filter is refused", entra.attribute_wanted("", pats), False)

# --- sync honours the filter -------------------------------------------
PAGES, calls = {}, []
class FakeResp:
    status_code = 200
    def __init__(self, b): self._b = b
    def raise_for_status(self): pass
    def json(self): return self._b
class FakeClient:
    def __init__(self,*a,**k): pass
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def get(self, url, headers=None, params=None):
        calls.append(url)
        return FakeResp(PAGES[url])
entra._token = lambda: "tok"
entra.httpx.Client = FakeClient
B = "https://graph.microsoft.com/beta"

PAGES[f"{B}/deviceManagement/deviceCustomAttributeShellScripts"] = {"value": [
    {"id": "s1", "displayName": "CPU script", "customAttributeName": "CPU and RAM"},
    {"id": "s2", "displayName": "Warranty script", "customAttributeName": "Warranty"},
    {"id": "s3", "displayName": "Noise", "customAttributeName": "Something Irrelevant"},
]}
for sid, val in (("s1", "Apple M4 Pro / 36 GB"), ("s2", "Expires 2027"), ("s3", "junk")):
    PAGES[f"{B}/deviceManagement/deviceCustomAttributeShellScripts/{sid}/deviceRunStates"] = {
        "value": [{"managedDevice": {"id": "d1"}, "resultMessage": val,
                   "lastStateUpdateDateTime": "2026-08-27T05:00:00Z"}]}
db.execute("""INSERT INTO devices (id, device_name, synced_at)
              VALUES ('d1','MAC-1','2026-08-27T00:00:00+00:00')""")

print("\n--- with no filter, everything syncs ---")
r = entra.sync_custom_attributes()
check("all three scripts synced", r["scripts_synced"], 3)
check("nothing filtered out", r["filtered_out"], 0)
check("values stored", r["attributes_stored"], 3)
check("names held", sorted(x["name"] for x in db.q("SELECT name FROM device_attributes")),
      ["CPU and RAM", "Something Irrelevant", "Warranty"])

print("\n--- narrow the filter ---")
settings.set_value("INTUNE_ATTRIBUTE_FILTER", "CPU and RAM, Warranty", "test")
calls.clear()
r = entra.sync_custom_attributes()
check("only two synced", r["scripts_synced"], 2)
check("one filtered out", r["filtered_out"], 1)
check("the excluded script's device states were never fetched",
      any("s3/deviceRunStates" in u for u in calls), False)
check("the wanted ones were fetched",
      all(any(f"{s}/deviceRunStates" in u for u in calls) for s in ("s1", "s2")), True)
check("stale value removed", r["stale_attributes_removed"], 1)
check("names held now",
      sorted(x["name"] for x in db.q("SELECT name FROM device_attributes")),
      ["CPU and RAM", "Warranty"])
check("everything available is still reported", r["available"],
      ["CPU and RAM", "Something Irrelevant", "Warranty"])

print("\n--- wildcard ---")
settings.set_value("INTUNE_ATTRIBUTE_FILTER", "CPU*", "test")
r = entra.sync_custom_attributes()
check("only the CPU attribute synced", r["scripts_synced"], 1)
check("the other stale value was cleared",
      [x["name"] for x in db.q("SELECT name FROM device_attributes")], ["CPU and RAM"])

print("\n--- a filter matching nothing keeps nothing ---")
settings.set_value("INTUNE_ATTRIBUTE_FILTER", "Nothing Matches This", "test")
r = entra.sync_custom_attributes()
check("no scripts synced", r["scripts_synced"], 0)
check("no attributes left", db.q1("SELECT COUNT(*) c FROM device_attributes")["c"], 0)
check("but they are still listed as available", len(r["available"]), 3)

print("\n--- clearing the filter brings them all back ---")
settings.clear("INTUNE_ATTRIBUTE_FILTER", "test")
r = entra.sync_custom_attributes()
check("all synced again", r["scripts_synced"], 3)
check("all values restored", db.q1("SELECT COUNT(*) c FROM device_attributes")["c"], 3)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
