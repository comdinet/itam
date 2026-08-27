import os, tempfile, sys, tempfile, tempfile
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

calls = []
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
        calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        if len(calls) == 1:
            return FakeResp({"value": [
                {"id":"1","userPrincipalName":"member@x.com","displayName":"Member User",
                 "accountEnabled":True}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=Z"})
        return FakeResp({"value": [
            {"id":"2","userPrincipalName":"member2@x.com","displayName":"Second","accountEnabled":True}]})
entra._token = lambda: "tok"
entra.httpx.Client = FakeClient

THE_FILTER = "accountEnabled eq true and userType eq 'Member'"
os.environ["ENTRA_USER_FILTER"] = THE_FILTER

r = entra.sync()
first = calls[0]
print("--- with the member-only filter ---")
check("filter passed through verbatim", first["params"].get("$filter"), THE_FILTER)
check("inner single quotes preserved", "'Member'" in first["params"]["$filter"], True)
check("ConsistencyLevel header sent", first["headers"].get("ConsistencyLevel"), "eventual")
check("$count=true sent", first["params"].get("$count"), "true")
check("header kept on the nextLink page", calls[1]["headers"].get("ConsistencyLevel"), "eventual")
check("paging still followed", len(calls), 2)
check("users synced", r["fetched"], 2)

# no filter -> no advanced query
calls.clear()
os.environ["ENTRA_USER_FILTER"] = ""
entra.sync()
print("\n--- with no filter ---")
check("no $filter", "$filter" in calls[0]["params"], False)
check("no ConsistencyLevel", "ConsistencyLevel" in calls[0]["headers"], False)
check("no $count", "$count" in calls[0]["params"], False)

# whitespace-only filter is treated as unset
calls.clear()
os.environ["ENTRA_USER_FILTER"] = "   "
entra.sync()
print("\n--- with a whitespace-only filter ---")
check("treated as unset", "$filter" in calls[0]["params"], False)
check("no advanced query", "ConsistencyLevel" in calls[0]["headers"], False)

# config status surfaces it for the UI
os.environ["ENTRA_USER_FILTER"] = THE_FILTER
print("\n--- config status ---")
check("filter reported to the UI", entra.config_status()["filter"], THE_FILTER)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
