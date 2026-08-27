import os, tempfile, sys, tempfile, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ENTRA_TENANT_ID"] = "tid"
os.environ["ENTRA_CLIENT_ID"] = "cid"
os.environ["ENTRA_CLIENT_SECRET"] = "secret"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from app import db, entra
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

check("is_configured", entra.is_configured(), True)

# Two-page Graph response, exercising @odata.nextLink paging.
PAGES = {
    "https://graph.microsoft.com/v1.0/users": {
        "value": [
            {"id": "g1", "userPrincipalName": "Ada.Lovelace@Contoso.com", "displayName": "Ada Lovelace",
             "jobTitle": "Head of Eng", "department": "Engineering", "accountEnabled": True},
            {"id": "g2", "userPrincipalName": "bob@contoso.com", "displayName": "Bob",
             "jobTitle": None, "department": None, "accountEnabled": False},
            {"id": "g3", "userPrincipalName": None, "displayName": "Service Principal Thing"},
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?$skiptoken=X",
    },
    "https://graph.microsoft.com/v1.0/users?$skiptoken=X": {
        "value": [
            {"id": "g4", "userPrincipalName": "carol@contoso.com", "displayName": "Carol",
             "jobTitle": "CFO", "department": "Finance", "accountEnabled": True},
        ],
    },
}
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
        requested.append((url, params))
        return FakeResp(PAGES[url])

entra._token = lambda: "faketoken"
entra.httpx.Client = FakeClient

r = entra.sync()
check("fetched", r["fetched"], 4)
check("created", r["created"], 3)
check("skipped (no UPN)", r["skipped"], 1)
check("pages requested", len(requested), 2)
check("2nd page sends no params", requested[1][1], None)
check("1st page has $select", "$select" in (requested[0][1] or {}), True)

rows = {row["upn"]: row for row in db.q("SELECT * FROM users")}
check("users stored", sorted(rows), ["ada.lovelace@contoso.com", "bob@contoso.com", "carol@contoso.com"])
check("UPN lowercased", "ada.lovelace@contoso.com" in rows, True)
check("display name", rows["ada.lovelace@contoso.com"]["display_name"], "Ada Lovelace")
check("department", rows["carol@contoso.com"]["department"], "Finance")
check("disabled flag", rows["bob@contoso.com"]["account_enabled"], 0)
check("source", rows["carol@contoso.com"]["source"], "entra")
check("synced_at set", bool(rows["carol@contoso.com"]["synced_at"]), True)

# Assign an asset, then re-sync with a renamed + re-enabled Bob: upsert must
# update in place and must NOT drop the assignment.
db.execute("INSERT INTO assets (name, category, cost_cents, assigned_upn) VALUES ('Laptop','Laptop',100000,'bob@contoso.com')")
PAGES["https://graph.microsoft.com/v1.0/users"]["value"][1] = {
    "id": "g2", "userPrincipalName": "bob@contoso.com", "displayName": "Bob Newname",
    "jobTitle": "Analyst", "department": "Finance", "accountEnabled": True}
r2 = entra.sync()
check("resync created", r2["created"], 0)
check("resync updated", r2["updated"], 3)
bob = db.q1("SELECT * FROM users WHERE upn='bob@contoso.com'")
check("rename applied", bob["display_name"], "Bob Newname")
check("re-enabled", bob["account_enabled"], 1)
check("assignment survived sync",
      db.q1("SELECT COUNT(*) c FROM assets WHERE assigned_upn='bob@contoso.com'")["c"], 1)
check("no duplicate rows", db.q1("SELECT COUNT(*) c FROM users")["c"], 3)

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
