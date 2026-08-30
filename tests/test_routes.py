"""Every page must actually render.

The template check catches a name a route stops passing. It cannot catch a
stale SQL column, which is the other half of the same mistake: renaming
pooled_items.quantity left three queries referring to it, and nothing failed
until a browser asked for the page. So: seed a small tenant, sign in, and GET
every page there is.
"""
import os, sys, tempfile, warnings
warnings.filterwarnings("ignore")   # starlette's httpx2 deprecation notice
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "RouteTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import db, pooled, fx, main

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

with TestClient(main.app) as client:          # lifespan runs: init_db, bootstrap
    fx.ensure_base()
    db.execute("INSERT INTO groups (id, display_name, member_count) VALUES ('g-il','Israel',2)")
    for upn, name in [("ada@x.com", "Ada"), ("yael@x.com", "Yael")]:
        db.execute("INSERT INTO users (upn, display_name, source) VALUES (?,?,'entra')", (upn, name))
        db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-il',?)", (upn,))
    db.execute("INSERT INTO assets (name,category,cost_cents,currency,rate_micro,serial,assigned_upn) "
               "VALUES ('MacBook Air 13 M4','Laptop',129900,'USD',1000000,'SN-1','ada@x.com')")
    mon = pooled.create("Dell U2723QE", "Monitor", 59900, currency="USD", rate_micro=1000000)
    pooled.assign(mon, "ada@x.com", 2)
    pooled.take_back(mon, "ada@x.com", 1)                    # leaves one on the shelf
    sub = db.execute("INSERT INTO subscriptions (name,monthly_cost_cents,currency,rate_micro) "
                     "VALUES ('Figma',1500,'USD',1000000)")
    from app import pricing, rules
    grp = pricing.create("Latitude 5450", 59900, currency="USD", rate_micro=1000000)
    rule = rules.create("Israel gets two monitors", "g-il", "asset", 2,
                        category="Monitor", asset_name="Dell U2723QE")

    r = client.post("/login", data={"username": "admin", "password": "RouteTest!2345"},
                    follow_redirects=False)
    check("sign-in redirects rather than erroring", r.status_code, 303)

    # Path params that cannot be guessed from the route itself.
    values = {"upn": "ada@x.com", "asset_id": 1, "item_id": mon, "rule_id": rule,
              "subscription_id": sub, "sub_id": sub, "group_id": "g-il",
              "sku_id": "none", "device_id": "none", "code": "USD", "key_id": 1,
              "user_id": 1, "category": "Monitor", "job": "users", "id": 1,
              "rest": "x", "template_id": "subscription-seats"}
    # Same parameter name, different thing, depending on the page it is on.
    per_path = {"/settings/pricing/{group_id}": {"group_id": grp}}
    skip = {"/logout", "/healthz"}          # side effect, and already covered

    checked, missing_param, bad = 0, [], []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if "GET" not in getattr(route, "methods", set()) or path in skip:
            continue
        if path.startswith(("/static", "/saml", "/api/")):
            continue
        url, ok = path, True
        here = {**values, **per_path.get(path, {})}
        for name in getattr(route, "param_convertors", {}):
            if name not in here:
                ok = False
                missing_param.append(f"{path} needs a value for {{{name}}}")
                break
            url = url.replace("{" + name + "}", str(here[name]))
        url = url.replace("{rest:path}", "x")
        if not ok:
            continue
        resp = client.get(url, follow_redirects=False)
        checked += 1
        if resp.status_code not in (200, 303, 307, 308, 404):
            bad.append(f"{url} -> {resp.status_code}")

    check("every route had a value to test with", missing_param, [])
    check("no page errored", bad, [])
    check("something was actually checked", checked > 15, True)
    print(f"       ({checked} routes exercised)")

print("\nFAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
