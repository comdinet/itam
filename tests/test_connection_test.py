import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(ENTRA_TENANT_ID="t", ENTRA_CLIENT_ID="c", ENTRA_CLIENT_SECRET="s")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, entra
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

# Endpoints that reject a page-size argument. Graph answers 400
# Request_UnsupportedQuery, which is what broke the licence probe.
NO_TOP = {"/subscribedSkus",
          "/deviceManagement/deviceCustomAttributeShellScripts"}

seen = []
class FakeResp:
    def __init__(self, code=200, body=None):
        self.status_code = code; self._b = body or {"value": []}; self.text = ""
    def json(self): return self._b
class FakeClient:
    def __init__(self,*a,**k): pass
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def get(self, url, headers=None, params=None):
        seen.append((url, dict(params or {})))
        # behave like Graph: reject $top where the real endpoint would
        for path in NO_TOP:
            if url.endswith(path) and "$top" in (params or {}):
                return FakeResp(400, {"error": {
                    "code": "Request_UnsupportedQuery",
                    "message": "This resource does not support custom page sizes. "
                               "Please retry without a page size argument."}})
        return FakeResp()
entra._token = lambda: "tok"
entra.httpx.Client = FakeClient

print("--- every probe must succeed against an endpoint that mimics Graph ---")
r = entra.test_connection()
check("token accepted", r["token"], True)
for p in r["probes"]:
    check(f"probe {p['label']!r} ok", p["ok"], True)
    if not p["ok"]:
        print("      detail:", p["detail"])

print("\n--- no probe sends $top to an endpoint that refuses it ---")
offenders = [u for u, params in seen
             if any(u.endswith(path) for path in NO_TOP) and "$top" in params]
check("no offending probe", offenders, [])

print("\n--- each probe declares its own query, no guessing ---")
for probe in entra.PROBES:
    check(f"{probe['label']!r} has explicit params", isinstance(probe["params"], dict), True)
    check(f"{probe['label']!r} declares a base URL", probe["base"].startswith("https://"), True)

print("\n--- and the real syncs do not send $top there either ---")
seen.clear()
db.execute("INSERT INTO users (upn, display_name, source) VALUES ('a@x.com','A','entra')")
entra.sync_licenses()
sku_calls = [(u, p) for u, p in seen if "subscribedSkus" in u]
check("licence sync called subscribedSkus", len(sku_calls) >= 1, True)
check("without $top", all("$top" not in p for _, p in sku_calls), True)

seen.clear()
db.execute("""INSERT INTO devices (id, device_name, synced_at)
              VALUES ('d1','X','2026-01-01T00:00:00+00:00')""")
entra.sync_custom_attributes()
script_calls = [(u, p) for u, p in seen if "deviceCustomAttributeShellScripts" in u
                and "deviceRunStates" not in u]
check("attribute sync listed the scripts", len(script_calls) >= 1, True)
check("without $top", all("$top" not in p for _, p in script_calls), True)

print("\n--- a 400 about page size is not reported as a filter problem ---")
class R:
    status_code = 400; text = ""
    def json(self): return {"error": {"code": "Request_UnsupportedQuery",
        "message": "This resource does not support custom page sizes."}}
msg = entra._explain(R(), "/subscribedSkus")
check("does not blame the filters", "check the OData filters" in msg, False)
check("says what it really is", "page-size" in msg, True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
