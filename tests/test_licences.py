import os, tempfile, sys, tempfile, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(ENTRA_TENANT_ID="t", ENTRA_CLIENT_ID="c", ENTRA_CLIENT_SECRET="s")
os.environ["ENTRA_USER_FILTER"] = "accountEnabled eq true and userType eq 'Member'"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, entra, jobs
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

PAGES, calls = {}, []
class FakeResp:
    status_code = 200          # the app now inspects this instead of raising
    def __init__(self, b): self._b = b
    def raise_for_status(self): pass
    def json(self): return self._b
class FakeClient:
    def __init__(self,*a,**k): pass
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def get(self, url, headers=None, params=None):
        calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        for key, body in PAGES.items():
            if url.startswith(key) and (params or {}).get("$select") == PAGES_SELECT.get(key, (params or {}).get("$select")):
                return FakeResp(body)
        if url in PAGES: return FakeResp(PAGES[url])
        raise AssertionError(f"unexpected {url} {params}")
entra._token = lambda: "tok"
entra.httpx.Client = FakeClient
G = "https://graph.microsoft.com/v1.0"
PAGES_SELECT = {}

# Two Business Premium variants, as the tenant reports them. GUIDs here are
# arbitrary on purpose: nothing may depend on a hardcoded GUID.
SPB_ID   = "11111111-2222-3333-4444-555555555555"
NOTEAMS  = "66666666-7777-8888-9999-000000000000"
UNKNOWN  = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

PAGES[f"{G}/subscribedSkus"] = {"value": [
    {"skuId": SPB_ID,  "skuPartNumber": "SPB",         "prepaidUnits": {"enabled": 25}, "consumedUnits": 21},
    {"skuId": NOTEAMS, "skuPartNumber": "SPB_NOTEAMS", "prepaidUnits": {"enabled": 10}, "consumedUnits": 3},
    {"skuId": "x", "skuPartNumber": "WEIRD_SKU_NOT_IN_MAP", "prepaidUnits": {"enabled": 1}, "consumedUnits": 0},
]}
PAGES[f"{G}/users"] = {"value": [
    {"userPrincipalName": "Ada@X.com",   "assignedLicenses": [{"skuId": SPB_ID}]},
    {"userPrincipalName": "grace@x.com", "assignedLicenses": [{"skuId": NOTEAMS}, {"skuId": SPB_ID}]},
    {"userPrincipalName": "gone@x.com",  "assignedLicenses": [{"skuId": SPB_ID}]},   # disabled here
    {"userPrincipalName": "guest@x.com", "assignedLicenses": [{"skuId": SPB_ID}]},   # not synced here
    {"userPrincipalName": "ada@x.com",   "assignedLicenses": [{"skuId": UNKNOWN}]},  # SKU not owned
]}

for upn, name, enabled in [("ada@x.com","Ada",1), ("grace@x.com","Grace",1), ("gone@x.com","Departed",0)]:
    db.execute("INSERT INTO users (upn,display_name,account_enabled,source) VALUES (?,?,?,'entra')",
               (upn, name, enabled))

r = entra.sync_licenses()
print("--- licence sync ---")
check("SKUs stored", r["skus"], 3)
check("assignments for synced people", r["assignments"], 4)   # ada, grace x2, gone
check("licensed but not synced counted", r["licensed_not_synced"], 1)  # guest
check("assignment to an unowned SKU counted", r["unknown_skus"], 1)

print("\n--- the two products asked about ---")
spb = db.q1("SELECT * FROM licenses WHERE sku_part_number='SPB'")
nt  = db.q1("SELECT * FROM licenses WHERE sku_part_number='SPB_NOTEAMS'")
check("SPB friendly name", spb["display_name"], "Microsoft 365 Business Premium")
check("SPB_NOTEAMS friendly name", nt["display_name"], "Microsoft 365 Business Premium (no Teams)")
check("SPB purchased", spb["prepaid"], 25)
check("SPB consumed (tenant)", spb["consumed"], 21)
check("no-Teams purchased", nt["prepaid"], 10)
check("unmapped SKU falls back to its string id",
      db.q1("SELECT display_name FROM licenses WHERE sku_part_number='WEIRD_SKU_NOT_IN_MAP'")["display_name"],
      "WEIRD_SKU_NOT_IN_MAP")

print("\n--- assignments ---")
check("UPN lowercased", bool(db.q1("SELECT 1 FROM user_licenses WHERE upn='ada@x.com'")), True)
check("grace holds both", db.q1("SELECT COUNT(*) c FROM user_licenses WHERE upn='grace@x.com'")["c"], 2)
check("unowned SKU not linked",
      db.q1("SELECT COUNT(*) c FROM user_licenses WHERE sku_id=?", (UNKNOWN,))["c"], 0)
check("unsynced guest not linked",
      db.q1("SELECT COUNT(*) c FROM user_licenses WHERE upn='guest@x.com'")["c"], 0)

print("\n--- reclaimable: licences on disabled accounts ---")
recl = db.q("""SELECT l.sku_part_number, u.upn FROM user_licenses ul
               JOIN licenses l ON l.sku_id=ul.sku_id JOIN users u ON u.upn=ul.upn
               WHERE u.account_enabled=0""")
check("one reclaimable seat found", [(r0["sku_part_number"], r0["upn"]) for r0 in recl],
      [("SPB", "gone@x.com")])

print("\n--- the user filter is applied to the licence query too ---")
user_call = [c for c in calls if c["url"].endswith("/users")][0]
check("same filter sent", user_call["params"].get("$filter"),
      "accountEnabled eq true and userType eq 'Member'")
check("advanced query header sent", user_call["headers"].get("ConsistencyLevel"), "eventual")
check("assignedLicenses selected", "assignedLicenses" in user_call["params"]["$select"], True)

print("\n--- re-sync replaces, does not accumulate ---")
before = db.q1("SELECT COUNT(*) c FROM user_licenses")["c"]
entra.sync_licenses()
check("assignment count stable", db.q1("SELECT COUNT(*) c FROM user_licenses")["c"], before)
PAGES[f"{G}/users"]["value"][0]["assignedLicenses"] = []      # Ada loses hers
entra.sync_licenses()
check("revoked licence disappears",
      db.q1("SELECT COUNT(*) c FROM user_licenses WHERE upn='ada@x.com'")["c"], 0)

print("\n--- job runner ---")
check("licences job registered", "licences" in jobs.JOBS, True)
check("users run before licences", jobs.ORDER.index("users") < jobs.ORDER.index("licences"), True)
rc = jobs.run(["licences"])
check("job exit code 0 on success", rc, 0)
def boom(): raise RuntimeError("Graph said no")
jobs.JOBS["licences"] = ("Entra ID licences", boom)
check("job exit code 1 on failure", jobs.run(["licences"]), 1)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
