"""ITAM - simple IT asset & subscription tracking, keyed on Entra ID UPN."""
import csv
import datetime
import io
import os
from contextlib import asynccontextmanager

from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, auth, db, entra

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    auth.purge_expired()
    creds = auth.bootstrap()
    if creds:
        bar = "=" * 66
        print(f"\n{bar}\n  ITAM first run - sign in with these credentials:\n"
              f"      {creds}\n"
              "  You will be asked to set a new password immediately.\n"
              "  (Set ITAM_ADMIN_PASSWORD in .env to choose your own instead.)\n"
              f"{bar}\n", flush=True)
    yield


app = FastAPI(title="ITAM", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["money"] = db.money
templates.env.globals["currency"] = db.CURRENCY
templates.env.globals["categories"] = db.categories
app.mount("/static", StaticFiles(directory="app/static"), name="static")


PUBLIC_PATHS = ("/login", "/static", "/favicon.ico", "/healthz", "/api/")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Everything is behind sign-in except the login page and static assets."""
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        return await call_next(request)

    user = auth.current_user(request.cookies.get(auth.COOKIE))
    if not user:
        if request.method == "GET":
            nxt = request.url.path
            if request.url.query:
                nxt += "?" + request.url.query
            resp = RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=303)
        else:
            resp = RedirectResponse("/login?msg=Your+session+expired", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # A freshly provisioned account must set its own password before doing anything.
    if user["must_change"] and not path.startswith("/account"):
        return RedirectResponse("/account?msg=Please+set+a+new+password+to+continue",
                                status_code=303)

    request.state.user = user
    return await call_next(request)


def today() -> str:
    return datetime.date.today().isoformat()


def render(request: Request, name: str, **ctx):
    ctx.setdefault("flash", request.query_params.get("msg"))
    ctx.setdefault("me", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, msg: str | None = None):
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={msg.replace(' ', '+')}"
    return RedirectResponse(url, status_code=303)


# --- cost queries --------------------------------------------------------

USER_COSTS = """
SELECT u.upn, u.display_name, u.job_title, u.department, u.account_enabled, u.source,
       COALESCE(a.asset_total, 0)   AS asset_total,
       COALESCE(a.asset_count, 0)   AS asset_count,
       COALESCE(s.monthly_total, 0) AS monthly_total,
       COALESCE(s.sub_count, 0)     AS sub_count
FROM users u
LEFT JOIN (SELECT assigned_upn, SUM(cost_cents) asset_total, COUNT(*) asset_count
             FROM assets WHERE assigned_upn IS NOT NULL GROUP BY assigned_upn) a
       ON a.assigned_upn = u.upn
LEFT JOIN (SELECT ss.upn, SUM(sub.monthly_cost_cents) monthly_total, COUNT(*) sub_count
             FROM subscription_seats ss JOIN subscriptions sub ON sub.id = ss.subscription_id
            GROUP BY ss.upn) s
       ON s.upn = u.upn
"""


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe: confirms the DB is readable."""
    db.q1("SELECT 1")
    return {"status": "ok"}


# --- sign in / account ---------------------------------------------------

def safe_next(raw: str) -> str:
    """Only allow same-site relative redirects after login."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if urlparse(raw).netloc:
        return "/"
    return raw


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if auth.current_user(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "flash": request.query_params.get("msg"), "next": safe_next(next), "me": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 next: str = Form("/")):
    target = safe_next(next)
    locked = auth.is_locked((username or "").strip().lower())
    if locked:
        return back(f"/login?next={quote(target, safe='')}",
                    f"Too many attempts - try again in {locked} minute(s)")
    token, user = auth.login(username, password)
    if not token:
        return back(f"/login?next={quote(target, safe='')}", "Incorrect username or password")
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax",
                    secure=auth.COOKIE_SECURE, max_age=auth.SESSION_HOURS * 3600, path="/")
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/login?msg=Signed+out", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    return render(request, "account.html")


@app.post("/account/password")
def change_password(request: Request, current: str = Form(""), new: str = Form(""),
                    confirm: str = Form("")):
    me = request.state.user
    # A forced first-time change has no meaningful "current" password to prove.
    if not me["must_change"] and not auth.verify_password(current, me["password_hash"]):
        return back("/account", "Current password is incorrect")
    if new != confirm:
        return back("/account", "The two new passwords do not match")
    problem = auth.password_problem(new)
    if problem:
        return back("/account", problem)
    auth.set_password(me["username"], new)
    auth.revoke_all(me["username"])          # sign out every other session
    resp = RedirectResponse("/login?msg=Password+changed+-+please+sign+in+again",
                            status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


# --- shared authorisation helper -----------------------------------------

def require_admin(request: Request) -> bool:
    return bool(request.state.user["is_admin"])


@app.post("/admin/accounts/new")
def account_new(request: Request, username: str = Form(...), password: str = Form(""),
                is_admin: str = Form("")):
    if not require_admin(request):
        return back("/admin/accounts", "Admin accounts only")
    username = username.strip().lower()
    if not username.isascii() or not username.replace(".", "").replace("-", "").replace("_", "").isalnum():
        return back("/admin/accounts", "Username may only contain letters, digits, dot, dash, underscore")
    if auth.get_user(username):
        return back("/admin/accounts", "That username already exists")
    problem = auth.password_problem(password)
    if problem:
        return back("/admin/accounts", problem)
    auth.create_user(username, password, is_admin=bool(is_admin), must_change=True)
    return back("/admin/accounts", f"Account '{username}' created - it must set a new password at first sign-in")


@app.post("/admin/accounts/delete")
def account_delete(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return back("/admin/accounts", "Admin accounts only")
    username = username.strip().lower()
    if username == request.state.user["username"]:
        return back("/admin/accounts", "You cannot delete the account you are signed in with")
    admins = [u for u in auth.list_users() if u["is_admin"]]
    target = auth.get_user(username)
    if target and target["is_admin"] and len(admins) <= 1:
        return back("/admin/accounts", "Cannot delete the last admin account")
    auth.delete_user(username)
    return back("/admin/accounts", f"Account '{username}' deleted")


@app.post("/admin/accounts/reset")
def account_reset(request: Request, username: str = Form(...), password: str = Form("")):
    if not require_admin(request):
        return back("/admin/accounts", "Admin accounts only")
    problem = auth.password_problem(password)
    if problem:
        return back("/admin/accounts", problem)
    username = username.strip().lower()
    if not auth.get_user(username):
        return back("/admin/accounts", "No such account")
    auth.set_password(username, password)
    db.execute("UPDATE auth_users SET must_change = 1 WHERE username = ?", (username,))
    auth.revoke_all(username)
    return back("/admin/accounts", f"Password reset for '{username}'; their sessions were signed out")


# --- machine API (bearer token, no session) -------------------------------

def _api_key(request: Request):
    return api.authenticate(
        request.headers.get("authorization") or request.headers.get("x-api-key"))


@app.get("/api/v1/ping")
async def api_ping(request: Request):
    """Lets an integrator confirm the token works before wiring up a webhook."""
    key = _api_key(request)
    if not key:
        api.log(None, "GET /api/v1/ping", 401, "Missing or invalid token")
        return JSONResponse({"error": "Invalid or missing API token."}, status_code=401)
    return {"status": "ok", "key": key["name"],
            "can_create_assets": bool(key["can_create_assets"]),
            "can_assign": bool(key["can_assign"])}


@app.post("/api/v1/assets")
async def api_create_asset(request: Request):
    """Create an asset from a webhook payload. Idempotent on external_id."""
    endpoint = "POST /api/v1/assets"
    key = _api_key(request)
    if not key:
        api.log(None, endpoint, 401, "Missing or invalid token")
        return JSONResponse({"error": "Invalid or missing API token."}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        api.log(key["name"], endpoint, 400, "Body was not valid JSON")
        return JSONResponse({"error": "Request body must be JSON."}, status_code=400)
    if not isinstance(payload, dict):
        api.log(key["name"], endpoint, 400, "Body was not a JSON object")
        return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)

    try:
        result = api.create_asset(key, payload)
    except api.ApiError as exc:
        api.log(key["name"], endpoint, exc.status, exc.message, payload)
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    except Exception as exc:
        api.log(key["name"], endpoint, 500, f"{type(exc).__name__}: {exc}", payload)
        return JSONResponse({"error": "Internal error creating the asset."}, status_code=500)

    status = 200 if result["status"] == "already_exists" else 201
    api.log(key["name"], endpoint, status,
            f"{result['status']}: {result['name']} (asset {result['asset_id']})", payload)
    return JSONResponse(result, status_code=status)


# --- dashboard -----------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    totals = db.q1(
        """SELECT (SELECT COUNT(*) FROM users)                                       AS users,
                  (SELECT COUNT(*) FROM users WHERE account_enabled = 0)             AS users_disabled,
                  (SELECT COUNT(*) FROM assets)                                      AS assets,
                  (SELECT COALESCE(SUM(cost_cents),0) FROM assets)                   AS asset_value,
                  (SELECT COUNT(*) FROM assets WHERE assigned_upn IS NULL)           AS spare,
                  (SELECT COALESCE(SUM(cost_cents),0) FROM assets
                     WHERE assigned_upn IS NULL)                                     AS spare_value,
                  (SELECT COUNT(*) FROM subscriptions)                               AS subs,
                  (SELECT COALESCE(SUM(sub.monthly_cost_cents),0)
                     FROM subscription_seats ss
                     JOIN subscriptions sub ON sub.id = ss.subscription_id)          AS monthly"""
    )
    by_dept = db.q(
        """SELECT COALESCE(NULLIF(TRIM(u.department),''),'(no department)') AS department,
                  COUNT(DISTINCT u.upn) AS people,
                  COALESCE(SUM(x.asset_total),0)   AS asset_total,
                  COALESCE(SUM(x.monthly_total),0) AS monthly_total
           FROM users u
           LEFT JOIN (SELECT u2.upn,
                             (SELECT COALESCE(SUM(cost_cents),0) FROM assets WHERE assigned_upn=u2.upn) asset_total,
                             (SELECT COALESCE(SUM(sub.monthly_cost_cents),0)
                                FROM subscription_seats ss JOIN subscriptions sub ON sub.id=ss.subscription_id
                               WHERE ss.upn=u2.upn) monthly_total
                        FROM users u2) x ON x.upn = u.upn
           GROUP BY department ORDER BY monthly_total DESC, asset_total DESC"""
    )
    top_subs = db.q(
        """SELECT sub.id, sub.name, sub.vendor, sub.monthly_cost_cents,
                  COUNT(ss.upn) AS seats,
                  COUNT(ss.upn) * sub.monthly_cost_cents AS monthly
           FROM subscriptions sub
           LEFT JOIN subscription_seats ss ON ss.subscription_id = sub.id
           GROUP BY sub.id ORDER BY monthly DESC"""
    )
    orphans = db.q(
        """SELECT sub.name, ss.upn FROM subscription_seats ss
           JOIN subscriptions sub ON sub.id = ss.subscription_id
           JOIN users u ON u.upn = ss.upn
           WHERE u.account_enabled = 0 ORDER BY sub.name"""
    )
    return render(request, "dashboard.html", t=totals, by_dept=by_dept,
                  top_subs=top_subs, orphans=orphans)


# --- users ---------------------------------------------------------------

@app.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", dept: str = ""):
    sql = USER_COSTS
    where, params = [], []
    if q:
        where.append("(u.upn LIKE ? OR u.display_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if dept:
        where.append("COALESCE(u.department,'') = ?")
        params.append(dept)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY u.display_name"
    rows = db.q(sql, params)
    depts = db.q("SELECT DISTINCT COALESCE(department,'') d FROM users ORDER BY d")
    return render(request, "users.html", users=rows, q=q, dept=dept, depts=depts)


@app.get("/users/{upn}", response_class=HTMLResponse)
def user_detail(request: Request, upn: str):
    upn = upn.strip().lower()          # Entra UPNs are stored lowercased
    user = db.q1(USER_COSTS + " WHERE u.upn = ?", (upn,))
    if not user:
        return HTMLResponse("<h1>404</h1><p>No such user.</p>", status_code=404)
    assets = db.q("SELECT * FROM assets WHERE assigned_upn = ? ORDER BY category, name", (upn,))
    subs = db.q(
        """SELECT sub.*, ss.assigned_on FROM subscription_seats ss
           JOIN subscriptions sub ON sub.id = ss.subscription_id
           WHERE ss.upn = ? ORDER BY sub.monthly_cost_cents DESC""", (upn,))
    spare = db.q("SELECT * FROM assets WHERE assigned_upn IS NULL ORDER BY category, name")
    avail_subs = db.q(
        """SELECT * FROM subscriptions WHERE id NOT IN
             (SELECT subscription_id FROM subscription_seats WHERE upn = ?)
           ORDER BY name""", (upn,))
    return render(request, "user_detail.html", u=user, assets=assets, subs=subs,
                  spare=spare, avail_subs=avail_subs)


@app.post("/users/{upn}/assign-asset")
def assign_asset(upn: str, asset_id: int = Form(...)):
    db.execute("UPDATE assets SET assigned_upn = ?, assigned_on = ? WHERE id = ?", (upn, today(), asset_id))
    return back(f"/users/{upn}", "Asset assigned")


@app.post("/users/{upn}/assign-sub")
def assign_sub(upn: str, subscription_id: int = Form(...)):
    db.execute(
        "INSERT OR IGNORE INTO subscription_seats (subscription_id, upn, assigned_on) VALUES (?,?,?)",
        (subscription_id, upn, today()))
    return back(f"/users/{upn}", "Licence assigned")


# --- assets --------------------------------------------------------------

@app.get("/assets", response_class=HTMLResponse)
def assets_list(request: Request, q: str = "", category: str = "", state: str = ""):
    sql = """SELECT a.*, u.display_name FROM assets a
             LEFT JOIN users u ON u.upn = a.assigned_upn"""
    where, params = [], []
    if q:
        where.append("(a.name LIKE ? OR COALESCE(a.serial,'') LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("a.category = ?")
        params.append(category)
    if state == "spare":
        where.append("a.assigned_upn IS NULL")
    elif state == "assigned":
        where.append("a.assigned_upn IS NOT NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.category, a.name"
    rows = db.q(sql, params)
    total = sum(r["cost_cents"] for r in rows)
    users = db.q("SELECT upn, display_name FROM users ORDER BY display_name")
    return render(request, "assets.html", assets=rows, users=users, q=q,
                  category=category, state=state, total=total)


@app.post("/assets/new")
def asset_new(name: str = Form(...), category: str = Form("Other"), cost: str = Form("0"),
              serial: str = Form(""), purchased_on: str = Form(""), notes: str = Form(""),
              assigned_upn: str = Form("")):
    db.execute(
        """INSERT INTO assets (name, category, cost_cents, serial, purchased_on, notes,
                               assigned_upn, assigned_on)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name.strip(), category, db.to_cents(cost), serial.strip() or None,
         purchased_on or None, notes.strip() or None, assigned_upn or None,
         today() if assigned_upn else None))
    return back("/assets", "Asset added")


@app.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_page(request: Request, asset_id: int):
    a = db.q1("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not a:
        return HTMLResponse("<h1>404</h1><p>No such asset.</p>", status_code=404)
    users = db.q("SELECT upn, display_name FROM users ORDER BY display_name")
    return render(request, "asset_edit.html", a=a, users=users)


@app.post("/assets/{asset_id}/edit")
def asset_edit(asset_id: int, name: str = Form(...), category: str = Form("Other"),
               cost: str = Form("0"), serial: str = Form(""), purchased_on: str = Form(""),
               notes: str = Form(""), assigned_upn: str = Form("")):
    prev = db.q1("SELECT assigned_upn FROM assets WHERE id = ?", (asset_id,))
    changed = prev and (prev["assigned_upn"] or "") != (assigned_upn or "")
    db.execute(
        """UPDATE assets SET name=?, category=?, cost_cents=?, serial=?, purchased_on=?,
                             notes=?, assigned_upn=?,
                             assigned_on = CASE WHEN ? THEN ? ELSE assigned_on END
           WHERE id=?""",
        (name.strip(), category, db.to_cents(cost), serial.strip() or None,
         purchased_on or None, notes.strip() or None, assigned_upn or None,
         1 if changed else 0, today() if assigned_upn else None, asset_id))
    return back("/assets", "Asset updated")


@app.post("/assets/{asset_id}/unassign")
def asset_unassign(asset_id: int, redirect: str = Form("/assets")):
    db.execute("UPDATE assets SET assigned_upn = NULL, assigned_on = NULL WHERE id = ?", (asset_id,))
    return back(redirect, "Asset returned to spares")


@app.post("/assets/{asset_id}/delete")
def asset_delete(asset_id: int, redirect: str = Form("/assets")):
    db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    return back(redirect, "Asset deleted")


# --- subscriptions -------------------------------------------------------

@app.get("/subscriptions", response_class=HTMLResponse)
def subs_list(request: Request):
    rows = db.q(
        """SELECT sub.*, COUNT(ss.upn) AS seats,
                  COUNT(ss.upn) * sub.monthly_cost_cents AS monthly
           FROM subscriptions sub
           LEFT JOIN subscription_seats ss ON ss.subscription_id = sub.id
           GROUP BY sub.id ORDER BY monthly DESC, sub.name""")
    monthly = sum(r["monthly"] for r in rows)
    return render(request, "subscriptions.html", subs=rows, monthly=monthly)


@app.post("/subscriptions/new")
def sub_new(name: str = Form(...), vendor: str = Form(""), monthly_cost: str = Form("0"),
            notes: str = Form("")):
    db.execute(
        "INSERT INTO subscriptions (name, vendor, monthly_cost_cents, notes) VALUES (?,?,?,?)",
        (name.strip(), vendor.strip() or None, db.to_cents(monthly_cost), notes.strip() or None))
    return back("/subscriptions", "Subscription added")


@app.get("/subscriptions/{sub_id}", response_class=HTMLResponse)
def sub_detail(request: Request, sub_id: int):
    sub = db.q1("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
    if not sub:
        return HTMLResponse("<h1>404</h1><p>No such subscription.</p>", status_code=404)
    seats = db.q(
        """SELECT u.upn, u.display_name, u.department, u.account_enabled, ss.assigned_on
           FROM subscription_seats ss JOIN users u ON u.upn = ss.upn
           WHERE ss.subscription_id = ? ORDER BY u.display_name""", (sub_id,))
    avail = db.q(
        """SELECT upn, display_name FROM users
           WHERE upn NOT IN (SELECT upn FROM subscription_seats WHERE subscription_id = ?)
           ORDER BY display_name""", (sub_id,))
    return render(request, "subscription_detail.html", s=sub, seats=seats, avail=avail,
                  monthly=len(seats) * sub["monthly_cost_cents"])


@app.post("/subscriptions/{sub_id}/edit")
def sub_edit(sub_id: int, name: str = Form(...), vendor: str = Form(""),
             monthly_cost: str = Form("0"), notes: str = Form("")):
    db.execute(
        "UPDATE subscriptions SET name=?, vendor=?, monthly_cost_cents=?, notes=? WHERE id=?",
        (name.strip(), vendor.strip() or None, db.to_cents(monthly_cost),
         notes.strip() or None, sub_id))
    return back(f"/subscriptions/{sub_id}", "Subscription updated")


@app.post("/subscriptions/{sub_id}/delete")
def sub_delete(sub_id: int):
    db.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    return back("/subscriptions", "Subscription deleted")


@app.post("/subscriptions/{sub_id}/seats/add")
def seat_add(sub_id: int, upn: str = Form(...)):
    db.execute(
        "INSERT OR IGNORE INTO subscription_seats (subscription_id, upn, assigned_on) VALUES (?,?,?)",
        (sub_id, upn, today()))
    return back(f"/subscriptions/{sub_id}", "Seat assigned")


@app.post("/subscriptions/{sub_id}/seats/remove")
def seat_remove(sub_id: int, upn: str = Form(...), redirect: str = Form("")):
    db.execute("DELETE FROM subscription_seats WHERE subscription_id = ? AND upn = ?", (sub_id, upn))
    return back(redirect or f"/subscriptions/{sub_id}", "Seat removed")


# --- admin / Entra sync --------------------------------------------------

# --- admin: general ------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_general(request: Request):
    counts = db.q1(
        """SELECT (SELECT COUNT(*) FROM users)         AS people,
                  (SELECT COUNT(*) FROM assets)        AS assets,
                  (SELECT COUNT(*) FROM subscriptions) AS subs,
                  (SELECT COUNT(*) FROM auth_users)    AS logins,
                  (SELECT COUNT(*) FROM api_keys WHERE active = 1) AS api_keys"""
    )
    settings = [
        ("Currency", db.CURRENCY, "ITAM_CURRENCY", "Display label only; no conversion is done."),
        ("Site address", os.environ.get("ITAM_SITE_ADDRESS") or "(not set)", "ITAM_SITE_ADDRESS",
         "Hostname the HTTPS certificate is issued for."),
        ("TLS mode", os.environ.get("ITAM_TLS") or "internal", "ITAM_TLS",
         "'internal' uses Caddy's local CA; an email address uses Let's Encrypt."),
        ("Secure cookies", "on" if auth.COOKIE_SECURE else "off", "ITAM_COOKIE_SECURE",
         "Must be on when served over HTTPS, off on plain HTTP."),
        ("Session length", f"{auth.SESSION_HOURS} hours", "ITAM_SESSION_HOURS",
         "How long a sign-in lasts before it expires."),
        ("Database", db.DB_PATH, "ITAM_DB", "SQLite file. Back it up with ./backup.sh."),
    ]
    return render(request, "admin_general.html", counts=counts, settings=settings,
                  section="general")


# --- admin: Entra ID ----------------------------------------------------

@app.get("/admin/entra", response_class=HTMLResponse)
def admin_entra(request: Request):
    last = db.q1("SELECT MAX(synced_at) AS last, COUNT(*) AS n FROM users WHERE source='entra'")
    people = db.q1("SELECT COUNT(*) c FROM users")["c"]
    return render(request, "admin_entra.html", cfg=entra.config_status(), last=last,
                  people=people, section="entra")


@app.post("/admin/entra/sync")
def admin_sync():
    if not entra.is_configured():
        return back("/admin/entra", "Entra ID is not configured - set the environment variables first")
    try:
        r = entra.sync()
    except Exception as exc:  # surface the Graph error rather than a 500 page
        return back("/admin/entra", f"Sync failed: {type(exc).__name__}: {exc}"[:300])
    return back("/admin/entra", f"Synced {r['fetched']} users ({r['created']} new, {r['updated']} updated)")


# --- admin: local accounts ----------------------------------------------

@app.get("/accounts")
def accounts_moved():
    return RedirectResponse("/admin/accounts", status_code=308)


@app.get("/admin/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    if not require_admin(request):
        return HTMLResponse("<h1>403</h1><p>Admin accounts only.</p>", status_code=403)
    return render(request, "admin_accounts.html", accounts=auth.list_users(),
                  section="accounts")


# --- admin: API keys and field mapping ----------------------------------

@app.get("/admin/api", response_class=HTMLResponse)
def admin_api(request: Request):
    if not require_admin(request):
        return HTMLResponse("<h1>403</h1><p>Admin accounts only.</p>", status_code=403)
    new_token = request.query_params.get("token")
    return render(request, "admin_api.html", keys=api.list_keys(),
                  mapping=db.q("SELECT * FROM api_field_map ORDER BY source_field"),
                  asset_fields=db.ASSET_FIELDS, log=api.recent_log(),
                  new_token=new_token, site=os.environ.get("ITAM_SITE_ADDRESS") or "your-itam-host",
                  section="api")


@app.post("/admin/api/keys/new")
def api_key_new(request: Request, name: str = Form(...), can_create_assets: str = Form(""),
                can_assign: str = Form("")):
    if not require_admin(request):
        return back("/admin/api", "Admin accounts only")
    if not name.strip():
        return back("/admin/api", "Give the key a name")
    token = api.create_key(name, bool(can_create_assets), bool(can_assign))
    # Shown once, via the redirect, then never again.
    return RedirectResponse(f"/admin/api?token={quote(token, safe='')}", status_code=303)


@app.post("/admin/api/keys/{key_id}/delete")
def api_key_delete(request: Request, key_id: int):
    if not require_admin(request):
        return back("/admin/api", "Admin accounts only")
    api.delete_key(key_id)
    return back("/admin/api", "API key deleted")


@app.post("/admin/api/keys/{key_id}/toggle")
def api_key_toggle(request: Request, key_id: int, active: str = Form("")):
    if not require_admin(request):
        return back("/admin/api", "Admin accounts only")
    api.set_key_active(key_id, bool(active))
    return back("/admin/api", "API key updated")


@app.post("/admin/api/mapping")
def api_mapping_set(request: Request, source_field: str = Form(...), target_field: str = Form(...)):
    if not require_admin(request):
        return back("/admin/api", "Admin accounts only")
    if target_field not in db.ASSET_FIELDS:
        return back("/admin/api", "Unknown target field")
    if not source_field.strip():
        return back("/admin/api", "Give the incoming field a name")
    api.set_mapping(source_field, target_field)
    return back("/admin/api", f"Mapped '{source_field.strip()}' to '{target_field}'")


@app.post("/admin/api/mapping/delete")
def api_mapping_delete(request: Request, source_field: str = Form(...)):
    if not require_admin(request):
        return back("/admin/api", "Admin accounts only")
    api.delete_mapping(source_field)
    return back("/admin/api", "Mapping removed")


@app.get("/export/costs.csv")
def export_costs():
    rows = db.q(USER_COSTS + " ORDER BY u.display_name")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["upn", "display_name", "department", "account_enabled", "assets",
                f"asset_value_{db.CURRENCY}", "subscriptions",
                f"monthly_{db.CURRENCY}", f"annual_{db.CURRENCY}"])
    for r in rows:
        w.writerow([r["upn"], r["display_name"], r["department"] or "", r["account_enabled"],
                    r["asset_count"], db.money(r["asset_total"]).replace(",", ""),
                    r["sub_count"], db.money(r["monthly_total"]).replace(",", ""),
                    db.money(r["monthly_total"] * 12).replace(",", "")])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="itam-costs.csv"'})
