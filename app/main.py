"""ITAM - simple IT asset & subscription tracking, keyed on Entra ID UPN."""
import csv
import datetime
import io
import os
import secrets
from contextlib import asynccontextmanager

from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, auth, db, entra, fx, pricing, rules, saml, settings, stock

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    fx.ensure_base()
    auth.purge_expired()
    auth.purge_pending()
    saml.purge()
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
templates.env.globals["categories"] = db.categories
app.mount("/static", StaticFiles(directory="app/static"), name="static")


PUBLIC_PATHS = ("/login", "/static", "/favicon.ico", "/healthz", "/api/", "/saml/")
COOKIE_2FA = "itam_2fa"


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

    # When two-factor is mandatory, nothing else is reachable until it is on.
    if (auth.require_2fa() and not user["totp_enabled"] and not user["sso"]
            and not path.startswith("/account") and not path.startswith("/logout")):
        return RedirectResponse(
            "/account?msg=Two-factor+authentication+is+required+-+set+it+up+to+continue",
            status_code=303)

    request.state.user = user
    return await call_next(request)


def today() -> str:
    return datetime.date.today().isoformat()


def pick_currency(code: str) -> tuple[str | None, int, str | None]:
    """Validate a submitted currency. Returns (code, frozen_rate, complaint).

    There is deliberately no fallback: an amount without a stated currency is
    ambiguous, and guessing one is how a shekel purchase silently becomes
    dollars.
    """
    code = (code or "").strip().upper()
    if not code:
        return None, 0, "Choose the currency this was paid in"
    row = fx.get(code)
    if not row:
        return None, 0, f"{code} is not set up. Add it under Settings > Currencies."
    if not row["active"]:
        return None, 0, f"{code} is switched off. Switch it on under Settings > Currencies."
    return code, int(row["rate_micro"]), None


def qr_svg(data: str) -> str:
    """Inline SVG QR code. SVG keeps it dependency-light - no image library."""
    import io
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]


def render(request: Request, name: str, **ctx):
    ctx.setdefault("flash", request.query_params.get("msg"))
    # Resolved per request: a settings change must show up without a restart,
    # and a Jinja global holding the function would render the function itself.
    ctx.setdefault("currency", settings.currency())
    ctx.setdefault("me", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, ctx)


def why(exc: Exception) -> str:
    """Message for the user. A GraphError already reads plainly; anything else
    needs its type to be identifiable at all."""
    return str(exc) if isinstance(exc, entra.GraphError) else f"{type(exc).__name__}: {exc}"


def back(url: str, msg: str | None = None):
    if msg:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}msg={msg.replace(' ', '+')}"
    return RedirectResponse(url, status_code=303)


# --- cost queries --------------------------------------------------------

USER_COSTS = """
SELECT u.upn, u.display_name, u.job_title, u.department, u.country,
       u.usage_location, u.account_enabled, u.source,
       COALESCE(a.asset_total, 0)   AS asset_total,
       COALESCE(a.asset_count, 0)   AS asset_count,
       COALESCE(k.stock_total, 0)   AS stock_total,
       COALESCE(k.stock_units, 0)   AS stock_units,
       COALESCE(a.asset_total, 0) + COALESCE(k.stock_total, 0) AS onetime_total,
       COALESCE(s.monthly_total, 0) AS monthly_total,
       COALESCE(s.sub_count, 0)     AS sub_count,
       COALESCE(a.currencies, '') || CASE WHEN a.currencies IS NOT NULL
            AND k.currencies IS NOT NULL THEN ',' ELSE '' END
            || COALESCE(k.currencies, '') AS onetime_currencies
FROM users u
LEFT JOIN (SELECT assigned_upn,
                  SUM(""" + db.conv("cost_cents", "rate_micro") + """) asset_total,
                  COUNT(*) asset_count,
                  GROUP_CONCAT(DISTINCT currency) currencies
             FROM assets WHERE assigned_upn IS NOT NULL GROUP BY assigned_upn) a
       ON a.assigned_upn = u.upn
LEFT JOIN (SELECT al.upn,
                  SUM(""" + db.conv("al.quantity * si.unit_cost_cents", "si.rate_micro") + """) stock_total,
                  SUM(al.quantity) stock_units,
                  GROUP_CONCAT(DISTINCT si.currency) currencies
             FROM stock_allocations al JOIN stock_items si ON si.id = al.item_id
            GROUP BY al.upn) k
       ON k.upn = u.upn
LEFT JOIN (SELECT ss.upn,
                  SUM(""" + db.conv("sub.monthly_cost_cents", "sub.rate_micro") + """) monthly_total,
                  COUNT(*) sub_count
             FROM subscription_seats ss JOIN subscriptions sub ON sub.id = ss.subscription_id
            GROUP BY ss.upn) s
       ON s.upn = u.upn
"""


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe: confirms the DB is readable."""
    db.q1("SELECT 1")
    return {"status": "ok"}


# --- SAML single sign-on -------------------------------------------------

COOKIE_SAML = "itam_saml"


def _saml_request(request: Request, post_data: dict | None = None) -> dict:
    """The request shape python3-saml expects, built from the real request.

    http_host comes from the configured base URL rather than the Host header:
    a forwarded header must never decide what audience an assertion is checked
    against.
    """
    from urllib.parse import urlparse
    parsed = urlparse(saml.base_url())
    return {
        "https": "on" if parsed.scheme == "https" else "off",
        "http_host": parsed.netloc,
        "server_port": None,
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": post_data or {},
    }


def _saml_auth(request: Request, post_data: dict | None = None):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    return OneLogin_Saml2_Auth(_saml_request(request, post_data), saml.sp_settings())


@app.get("/saml/metadata")
def saml_metadata():
    """SP metadata, for uploading into the Entra enterprise application."""
    if not saml.is_configured():
        return JSONResponse({"error": "SAML is not configured."}, status_code=404)
    from onelogin.saml2.settings import OneLogin_Saml2_Settings
    # Not named `settings`: that is the app's own settings module.
    sp = OneLogin_Saml2_Settings(saml.sp_settings(), sp_validation_only=True)
    metadata = sp.get_sp_metadata()
    errors = sp.validate_metadata(metadata)
    if errors:
        return JSONResponse({"error": "Invalid SP metadata", "detail": errors},
                            status_code=500)
    return Response(content=metadata, media_type="application/xml")


@app.get("/saml/login")
def saml_login(request: Request, next: str = "/"):
    if not saml.is_configured():
        return back("/login", "Single sign-on is not configured")
    target = safe_next(next)
    a = _saml_auth(request)
    url = a.login(return_to=target)
    request_id = a.get_last_request_id()
    saml.remember_request(request_id, target)

    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(COOKIE_SAML, request_id, httponly=True, samesite="lax",
                    secure=settings.cookie_secure(),
                    max_age=saml.REQUEST_MINUTES * 60, path="/")
    return resp


@app.post("/saml/acs")
async def saml_acs(request: Request):
    """Where Entra posts the assertion."""
    if not saml.is_configured():
        return back("/login", "Single sign-on is not configured")

    form = await request.form()
    post_data = {k: v for k, v in form.items() if isinstance(v, str)}

    outstanding = saml.take_request(request.cookies.get(COOKIE_SAML))
    request_id = outstanding["request_id"] if outstanding else None
    if not request_id and not saml.allow_unsolicited():
        return back("/login", "That sign-in did not start here - please try again")

    a = _saml_auth(request, post_data)
    a.process_response(request_id=request_id)
    errors = a.get_errors()
    if errors:
        detail = a.get_last_error_reason() or ", ".join(errors)
        return back("/login", f"Single sign-on failed: {detail}"[:200])
    if not a.is_authenticated():
        return back("/login", "Single sign-on did not authenticate")

    # Replay: a correctly signed assertion is still only good once.
    assertion_id = a.get_last_assertion_id()
    if assertion_id:
        if saml.already_seen(assertion_id):
            return back("/login", "That sign-in has already been used")
        saml.mark_seen(assertion_id)

    attributes = a.get_attributes() or {}
    username = saml.pick_username(a.get_nameid(), attributes)
    if not username:
        return back("/login", "The sign-in carried no username claim")

    user = auth.get_user(username)
    if not user:
        if not saml.auto_provision():
            return back("/login",
                        f"No ITAM account for '{username}' - an admin must create it first")
        auth.create_user(username, secrets.token_urlsafe(32),
                         is_admin=saml.is_admin_by_group(attributes))
        db.execute("UPDATE auth_users SET sso = 1, must_change = 0 WHERE username = ?",
                   (username,))
        user = auth.get_user(username)
    else:
        db.execute("UPDATE auth_users SET sso = 1 WHERE username = ?", (username,))
        if saml.admin_group() and saml.is_admin_by_group(attributes) and not user["is_admin"]:
            db.execute("UPDATE auth_users SET is_admin = 1 WHERE username = ?", (username,))

    target = (outstanding["next_url"] if outstanding else "/") or "/"
    resp = RedirectResponse(safe_next(target), status_code=303)
    resp.set_cookie(auth.COOKIE, auth.issue_session(username), httponly=True,
                    samesite="lax", secure=settings.cookie_secure(),
                    max_age=settings.session_hours() * 3600, path="/")
    resp.delete_cookie(COOKIE_SAML, path="/")
    return resp


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
        "flash": request.query_params.get("msg"), "next": safe_next(next), "me": None,
        "sso": saml.is_configured()})


@app.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                 next: str = Form("/")):
    target = safe_next(next)
    locked = auth.is_locked((username or "").strip().lower())
    if locked:
        return back(f"/login?next={quote(target, safe='')}",
                    f"Too many attempts - try again in {locked} minute(s)")
    user = auth.check_credentials(username, password)
    if not user:
        return back(f"/login?next={quote(target, safe='')}", "Incorrect username or password")

    if user["totp_enabled"]:
        pending = auth.start_pending(user["username"])
        resp = RedirectResponse(f"/login/2fa?next={quote(target, safe='')}", status_code=303)
        resp.set_cookie(COOKIE_2FA, pending, httponly=True, samesite="lax",
                        secure=settings.cookie_secure(),
                        max_age=auth.PENDING_MINUTES * 60, path="/")
        return resp

    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.issue_session(user["username"]), httponly=True,
                    samesite="lax", secure=settings.cookie_secure(),
                    max_age=settings.session_hours() * 3600, path="/")
    return resp


@app.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, next: str = "/"):
    user = auth.pending_user(request.cookies.get(COOKIE_2FA))
    if not user:
        return RedirectResponse("/login?msg=That+sign-in+expired+-+please+start+again",
                                status_code=303)
    return templates.TemplateResponse(request, "login_2fa.html", {
        "flash": request.query_params.get("msg"), "next": safe_next(next),
        "username": user["username"], "me": None,
        "recovery_left": auth.recovery_codes_left(user["username"])})


@app.post("/login/2fa")
def login_2fa_submit(request: Request, code: str = Form(""), recovery: str = Form(""),
                     next: str = Form("/")):
    pending_token = request.cookies.get(COOKIE_2FA)
    user = auth.pending_user(pending_token)
    target = safe_next(next)
    if not user:
        return back("/login", "That sign-in expired - please start again")

    username = user["username"]
    locked = auth.is_locked(username)
    if locked:
        return back("/login", f"Too many attempts - try again in {locked} minute(s)")

    step = auth.verify_totp(user["totp_secret"], code, user["totp_last_step"])
    used_recovery = False
    if step is None:
        if recovery.strip() and auth.use_recovery_code(username, recovery):
            used_recovery = True
        elif auth.totp_already_used(user["totp_secret"], code, user["totp_last_step"]):
            # Right code, spent window. Not a failed attempt, so it does not
            # count toward the lockout.
            return back(f"/login/2fa?next={quote(target, safe='')}",
                        "That code was already used - wait for the next one")
        else:
            auth.record_failure(username)
            return back(f"/login/2fa?next={quote(target, safe='')}",
                        "That code is not valid")

    if step is not None:
        # Remember the step so the same code cannot be replayed.
        db.execute("UPDATE auth_users SET totp_last_step = ? WHERE username = ?",
                   (step, username))
    auth.clear_failures(username)
    auth.clear_pending(pending_token)

    msg = target
    if used_recovery:
        left = auth.recovery_codes_left(username)
        msg = f"/account?msg=Recovery+code+used+-+{left}+left"

    resp = RedirectResponse(msg, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.issue_session(username), httponly=True,
                    samesite="lax", secure=settings.cookie_secure(),
                    max_age=settings.session_hours() * 3600, path="/")
    resp.delete_cookie(COOKIE_2FA, path="/")
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE))
    auth.clear_pending(request.cookies.get(COOKIE_2FA))
    resp = RedirectResponse("/login?msg=Signed+out", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    resp.delete_cookie(COOKIE_2FA, path="/")
    return resp


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    me = request.state.user
    return render(request, "account.html",
                  recovery_left=auth.recovery_codes_left(me["username"]),
                  require_2fa=auth.require_2fa())


@app.post("/account/2fa/start")
def totp_start(request: Request):
    """Generate a secret and show the QR. Not enabled until a code confirms it."""
    me = request.state.user
    auth.begin_totp_setup(me["username"])
    return back("/account/2fa/setup")


@app.get("/account/2fa/setup", response_class=HTMLResponse)
def totp_setup(request: Request):
    me = request.state.user
    user = auth.get_user(me["username"])
    if not user["totp_secret"] or user["totp_enabled"]:
        return back("/account", "Nothing to set up")
    secret = user["totp_secret"]
    return render(request, "account_2fa.html", secret=secret,
                  uri=auth.totp_uri(user["username"], secret),
                  qr_svg=qr_svg(auth.totp_uri(user["username"], secret)))


@app.post("/account/2fa/confirm")
def totp_confirm(request: Request, code: str = Form("")):
    me = request.state.user
    if not auth.confirm_totp(me["username"], code):
        user = auth.get_user(me["username"])
        if auth.totp_already_used(user["totp_secret"], code, user["totp_last_step"]):
            return back("/account/2fa/setup", "That code was already used - wait for the next one")
        return back("/account/2fa/setup",
                    "That code did not match - check the clock on your phone and try the next one")
    codes = auth.issue_recovery_codes(me["username"])
    return render(request, "account_2fa_codes.html", codes=codes,
                  flash="Two-factor authentication is on")


@app.post("/account/2fa/test")
def totp_test(request: Request, code: str = Form("")):
    """Check a code without consuming it.

    Deliberately does not advance totp_last_step: this is for confirming the
    authenticator is in sync, and burning the window would make the next real
    sign-in fail for no reason.
    """
    me = request.state.user
    user = auth.get_user(me["username"])
    if not user["totp_enabled"] or not user["totp_secret"]:
        return back("/account", "Two-factor is not set up on this account")
    if auth.verify_totp(user["totp_secret"], code, None) is not None:
        return back("/account", "That code is correct - two-factor is working")
    return back("/account",
                "That code did not match. Check your phone's clock is accurate, "
                "and that you are reading the entry for this account.")


@app.post("/account/2fa/disable")
def totp_disable(request: Request, current: str = Form("")):
    me = request.state.user
    if auth.require_2fa():
        return back("/account", "Two-factor authentication is required and cannot be turned off")
    if not auth.verify_password(current, me["password_hash"]):
        return back("/account", "Current password is incorrect")
    auth.disable_totp(me["username"])
    return back("/account", "Two-factor authentication turned off")


@app.post("/account/2fa/recovery")
def totp_recovery(request: Request, current: str = Form("")):
    me = request.state.user
    if not auth.verify_password(current, me["password_hash"]):
        return back("/account", "Current password is incorrect")
    if not me["totp_enabled"]:
        return back("/account", "Two-factor authentication is not on")
    codes = auth.issue_recovery_codes(me["username"])
    return render(request, "account_2fa_codes.html", codes=codes,
                  flash="New recovery codes - the old ones no longer work")


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


@app.post("/settings/accounts/new")
def account_new(request: Request, username: str = Form(...), password: str = Form(""),
                is_admin: str = Form("")):
    if not require_admin(request):
        return back("/settings/accounts", "Admin accounts only")
    username = username.strip().lower()
    if not username.isascii() or not username.replace(".", "").replace("-", "").replace("_", "").isalnum():
        return back("/settings/accounts", "Username may only contain letters, digits, dot, dash, underscore")
    if auth.get_user(username):
        return back("/settings/accounts", "That username already exists")
    problem = auth.password_problem(password)
    if problem:
        return back("/settings/accounts", problem)
    auth.create_user(username, password, is_admin=bool(is_admin), must_change=True)
    return back("/settings/accounts", f"Account '{username}' created - it must set a new password at first sign-in")


@app.post("/settings/accounts/delete")
def account_delete(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return back("/settings/accounts", "Admin accounts only")
    username = username.strip().lower()
    if username == request.state.user["username"]:
        return back("/settings/accounts", "You cannot delete the account you are signed in with")
    admins = [u for u in auth.list_users() if u["is_admin"]]
    target = auth.get_user(username)
    if target and target["is_admin"] and len(admins) <= 1:
        return back("/settings/accounts", "Cannot delete the last admin account")
    auth.delete_user(username)
    return back("/settings/accounts", f"Account '{username}' deleted")


@app.post("/settings/accounts/2fa-reset")
def account_2fa_reset(request: Request, username: str = Form(...)):
    if not require_admin(request):
        return back("/settings/accounts", "Admin accounts only")
    username = username.strip().lower()
    if not auth.get_user(username):
        return back("/settings/accounts", "No such account")
    auth.disable_totp(username)
    auth.revoke_all(username)
    return back("/settings/accounts",
                f"Two-factor turned off for '{username}'; they must set it up again")


@app.post("/settings/accounts/reset")
def account_reset(request: Request, username: str = Form(...), password: str = Form("")):
    if not require_admin(request):
        return back("/settings/accounts", "Admin accounts only")
    problem = auth.password_problem(password)
    if problem:
        return back("/settings/accounts", problem)
    username = username.strip().lower()
    if not auth.get_user(username):
        return back("/settings/accounts", "No such account")
    auth.set_password(username, password)
    db.execute("UPDATE auth_users SET must_change = 1 WHERE username = ?", (username,))
    auth.revoke_all(username)
    return back("/settings/accounts", f"Password reset for '{username}'; their sessions were signed out")


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
                  (SELECT COALESCE(SUM(((cost_cents * COALESCE(rate_micro,1000000) + 500000) / 1000000)),0)
                     FROM assets)                                                   AS asset_value,
                  (SELECT COUNT(*) FROM assets WHERE assigned_upn IS NULL)           AS spare,
                  (SELECT COALESCE(SUM(((cost_cents * COALESCE(rate_micro,1000000) + 500000) / 1000000)),0)
                     FROM assets WHERE assigned_upn IS NULL)                        AS spare_value,
                  (SELECT COUNT(*) FROM subscriptions)                               AS subs,
                  (SELECT COALESCE(SUM(((sub.monthly_cost_cents * COALESCE(sub.rate_micro,1000000) + 500000) / 1000000)),0)
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
        """SELECT sub.id, sub.name, sub.vendor, sub.monthly_cost_cents, sub.currency,
                  COUNT(ss.upn) AS seats,
                  """ + db.conv("COUNT(ss.upn) * sub.monthly_cost_cents", "sub.rate_micro") + """ AS monthly
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
    by_currency = db.q(
        """SELECT c.code, c.symbol, c.rate_micro, c.rate_source, c.rate_set_on,
                  COALESCE(a.raw, 0) AS asset_raw, COALESCE(a.rep, 0) AS asset_rep,
                  COALESCE(k.raw, 0) AS stock_raw, COALESCE(k.rep, 0) AS stock_rep,
                  COALESCE(s.raw, 0) AS monthly_raw, COALESCE(s.rep, 0) AS monthly_rep
           FROM currencies c
           LEFT JOIN (SELECT currency, SUM(cost_cents) raw,
                             SUM(""" + db.conv("cost_cents", "rate_micro") + """) rep
                        FROM assets GROUP BY currency) a ON a.currency = c.code
           LEFT JOIN (SELECT currency, SUM(quantity * unit_cost_cents) raw,
                             SUM(""" + db.conv("quantity * unit_cost_cents", "rate_micro") + """) rep
                        FROM stock_items GROUP BY currency) k ON k.currency = c.code
           LEFT JOIN (SELECT sub.currency, SUM(sub.monthly_cost_cents) raw,
                             SUM(""" + db.conv("sub.monthly_cost_cents", "sub.rate_micro") + """) rep
                        FROM subscription_seats ss
                        JOIN subscriptions sub ON sub.id = ss.subscription_id
                       GROUP BY sub.currency) s ON s.currency = c.code
           WHERE c.active = 1
           ORDER BY (COALESCE(a.rep,0) + COALESCE(k.rep,0)) DESC, c.code""")
    rate_asof = db.q1(
        "SELECT MAX(rate_set_on) AS d FROM currencies WHERE rate_source != 'base'")
    return render(request, "dashboard.html", t=totals, by_dept=by_dept,
                  top_subs=top_subs, orphans=orphans, stock=stock.totals(),
                  by_currency=by_currency, reporting=fx.reporting_code(),
                  rate_asof=rate_asof["d"] if rate_asof else None)


# --- users ---------------------------------------------------------------

@app.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", dept: str = "", country: str = ""):
    sql = USER_COSTS
    where, params = [], []
    if q:
        where.append("(u.upn LIKE ? OR u.display_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if dept:
        where.append("COALESCE(u.department,'') = ?")
        params.append(dept)
    if country:
        where.append("COALESCE(u.country,'') = ?")
        params.append(country)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY u.display_name"
    rows = db.q(sql, params)
    depts = db.q("SELECT DISTINCT COALESCE(department,'') d FROM users ORDER BY d")
    countries = db.q("SELECT DISTINCT COALESCE(country,'') c FROM users ORDER BY c")
    return render(request, "users.html", users=rows, q=q, dept=dept, depts=depts,
                  country=country, countries=countries)


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
    entra_licences = db.q(
        """SELECT l.* FROM user_licenses ul JOIN licenses l ON l.sku_id = ul.sku_id
           WHERE ul.upn = ? ORDER BY l.display_name""", (upn,))
    stock_items = stock.for_user(upn)
    stock_available = db.q(
        """SELECT s.*, (s.quantity - COALESCE((SELECT SUM(quantity)
             FROM stock_allocations a WHERE a.item_id = s.id), 0)) AS available
           FROM stock_items s
           WHERE available > 0 ORDER BY s.category, s.name""")
    return render(request, "user_detail.html", u=user, assets=assets, subs=subs,
                  spare=spare, avail_subs=avail_subs, entra_licences=entra_licences,
                  stock_items=stock_items, stock_available=stock_available)


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
    # Mixed currencies cannot be added raw, so the total is in reporting currency.
    total = sum(fx.to_reporting(r["cost_cents"], r["rate_micro"]) for r in rows)
    users = db.q("SELECT upn, display_name FROM users ORDER BY display_name")
    return render(request, "assets.html", assets=rows, users=users, q=q,
                  category=category, state=state, total=total,
                  currencies=fx.listing(active_only=True))


@app.post("/assets/new")
def asset_new(name: str = Form(...), category: str = Form("Other"), cost: str = Form("0"),
              currency: str = Form(""), serial: str = Form(""),
              purchased_on: str = Form(""), notes: str = Form(""),
              assigned_upn: str = Form("")):
    code, rate, problem = pick_currency(currency)
    if problem:
        return back("/assets", problem)
    db.execute(
        """INSERT INTO assets (name, category, cost_cents, currency, rate_micro, serial,
                               purchased_on, notes, assigned_upn, assigned_on)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (name.strip(), category, db.to_cents(cost), code, rate, serial.strip() or None,
         purchased_on or None, notes.strip() or None, assigned_upn or None,
         today() if assigned_upn else None))
    return back("/assets", "Asset added")


@app.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_page(request: Request, asset_id: int):
    a = db.q1("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not a:
        return HTMLResponse("<h1>404</h1><p>No such asset.</p>", status_code=404)
    users = db.q("SELECT upn, display_name FROM users ORDER BY display_name")
    return render(request, "asset_edit.html", a=a, users=users,
                  currencies=fx.listing(active_only=True))


@app.post("/assets/{asset_id}/edit")
def asset_edit(asset_id: int, name: str = Form(...), category: str = Form("Other"),
               cost: str = Form("0"), currency: str = Form(""), serial: str = Form(""),
               purchased_on: str = Form(""), notes: str = Form(""),
               assigned_upn: str = Form("")):
    prev = db.q1("SELECT assigned_upn, currency, rate_micro FROM assets WHERE id = ?",
                 (asset_id,))
    if not prev:
        return back("/assets", "No such asset")
    code, rate, problem = pick_currency(currency)
    if problem:
        return back(f"/assets/{asset_id}", problem)
    # Keep the frozen rate while the currency is unchanged: editing a typo in
    # the name must not silently revalue the purchase.
    if code == (prev["currency"] or "") and prev["rate_micro"]:
        rate = int(prev["rate_micro"])
    changed = (prev["assigned_upn"] or "") != (assigned_upn or "")
    db.execute(
        """UPDATE assets SET name=?, category=?, cost_cents=?, currency=?, rate_micro=?,
                             serial=?, purchased_on=?, notes=?, assigned_upn=?,
                             assigned_on = CASE WHEN ? THEN ? ELSE assigned_on END
           WHERE id=?""",
        (name.strip(), category, db.to_cents(cost), code, rate, serial.strip() or None,
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


# --- stock (pooled items) ------------------------------------------------

@app.get("/stock", response_class=HTMLResponse)
def stock_list(request: Request):
    return render(request, "stock.html", items=stock.listing(),
                  totals=stock.totals(), currencies=fx.listing(active_only=True))


@app.post("/stock/new")
def stock_new(name: str = Form(...), category: str = Form("Peripheral"),
              unit_cost: str = Form("0"), currency: str = Form(""),
              quantity: str = Form("0"), vendor: str = Form(""), notes: str = Form("")):
    if not name.strip():
        return back("/stock", "Give the item a name")
    code, rate, problem = pick_currency(currency)
    if problem:
        return back("/stock", problem)
    try:
        qty = max(0, int(quantity or 0))
    except ValueError:
        return back("/stock", "Quantity must be a whole number")
    item_id = stock.create(name, category, db.to_cents(unit_cost), qty, vendor, notes,
                           currency=code, rate_micro=rate)
    return back(f"/stock/{item_id}", "Item added")


@app.get("/stock/{item_id}", response_class=HTMLResponse)
def stock_detail(request: Request, item_id: int):
    item = stock.get(item_id)
    if not item:
        return HTMLResponse("<h1>404</h1><p>No such item.</p>", status_code=404)
    people = db.q("SELECT upn, display_name FROM users ORDER BY display_name")
    return render(request, "stock_detail.html", i=item, s=stock.summary(item),
                  people=people, currencies=fx.listing(active_only=True))


@app.post("/stock/{item_id}/edit")
def stock_edit(item_id: int, name: str = Form(...), category: str = Form("Peripheral"),
               unit_cost: str = Form("0"), currency: str = Form(""),
               quantity: str = Form("0"), vendor: str = Form(""), notes: str = Form("")):
    existing = stock.get(item_id)
    if not existing:
        return back("/stock", "No such item")
    code, rate, problem = pick_currency(currency)
    if problem:
        return back(f"/stock/{item_id}", problem)
    if code == (existing["currency"] or "") and existing["rate_micro"]:
        rate = int(existing["rate_micro"])
    try:
        qty = max(0, int(quantity or 0))
    except ValueError:
        return back(f"/stock/{item_id}", "Quantity must be a whole number")
    problem = stock.update(item_id, name, category, db.to_cents(unit_cost), qty,
                           vendor, notes, currency=code, rate_micro=rate)
    return back(f"/stock/{item_id}", problem or "Item updated")


@app.post("/stock/{item_id}/delete")
def stock_delete(item_id: int):
    stock.delete(item_id)
    return back("/stock", "Item deleted")


@app.post("/stock/{item_id}/assign")
def stock_assign(item_id: int, upn: str = Form(...), quantity: str = Form("1"),
                 redirect: str = Form("")):
    try:
        qty = int(quantity or 1)
    except ValueError:
        qty = 1
    problem = stock.assign(item_id, upn.strip().lower(), qty)
    target = redirect or f"/stock/{item_id}"
    return back(target, problem or f"Handed out {qty} unit(s)")


@app.post("/stock/{item_id}/take-back")
def stock_take_back(item_id: int, upn: str = Form(...), quantity: str = Form(""),
                    redirect: str = Form("")):
    qty = None
    if quantity.strip():
        try:
            qty = int(quantity)
        except ValueError:
            qty = None
    problem = stock.take_back(item_id, upn.strip().lower(), qty)
    target = redirect or f"/stock/{item_id}"
    return back(target, problem or "Returned to stock")


# --- subscriptions -------------------------------------------------------

@app.get("/subscriptions", response_class=HTMLResponse)
def subs_list(request: Request):
    rows = db.q(
        """SELECT sub.*, COUNT(ss.upn) AS seats,
                  COUNT(ss.upn) * sub.monthly_cost_cents AS monthly,
                  """ + db.conv("COUNT(ss.upn) * sub.monthly_cost_cents", "sub.rate_micro") + """ AS monthly_rep
           FROM subscriptions sub
           LEFT JOIN subscription_seats ss ON ss.subscription_id = sub.id
           GROUP BY sub.id ORDER BY monthly_rep DESC, sub.name""")
    monthly = sum(r["monthly_rep"] for r in rows)
    return render(request, "subscriptions.html", subs=rows, monthly=monthly,
                  currencies=fx.listing(active_only=True))


@app.post("/subscriptions/new")
def sub_new(name: str = Form(...), vendor: str = Form(""), monthly_cost: str = Form("0"),
            currency: str = Form(""), notes: str = Form("")):
    code, rate, problem = pick_currency(currency)
    if problem:
        return back("/subscriptions", problem)
    db.execute(
        """INSERT INTO subscriptions (name, vendor, monthly_cost_cents, currency,
                                      rate_micro, notes)
           VALUES (?,?,?,?,?,?)""",
        (name.strip(), vendor.strip() or None, db.to_cents(monthly_cost), code, rate,
         notes.strip() or None))
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
                  monthly=len(seats) * sub["monthly_cost_cents"],
                  monthly_rep=fx.to_reporting(len(seats) * sub["monthly_cost_cents"],
                                              sub["rate_micro"]),
                  currencies=fx.listing(active_only=True))


@app.post("/subscriptions/{sub_id}/edit")
def sub_edit(sub_id: int, name: str = Form(...), vendor: str = Form(""),
             monthly_cost: str = Form("0"), currency: str = Form(""),
             notes: str = Form("")):
    prev = db.q1("SELECT currency, rate_micro FROM subscriptions WHERE id = ?", (sub_id,))
    if not prev:
        return back("/subscriptions", "No such subscription")
    code, rate, problem = pick_currency(currency)
    if problem:
        return back(f"/subscriptions/{sub_id}", problem)
    if code == (prev["currency"] or "") and prev["rate_micro"]:
        rate = int(prev["rate_micro"])
    db.execute(
        """UPDATE subscriptions SET name=?, vendor=?, monthly_cost_cents=?, currency=?,
                                    rate_micro=?, notes=? WHERE id=?""",
        (name.strip(), vendor.strip() or None, db.to_cents(monthly_cost), code, rate,
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

# --- settings: groups ----------------------------------------------------

@app.get("/settings/groups", response_class=HTMLResponse)
def settings_groups(request: Request):
    groups = db.q(
        """SELECT g.*,
                  (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS known,
                  (SELECT COUNT(*) FROM group_members_unlinked u WHERE u.group_id = g.id)
                      AS unlinked
           FROM groups g WHERE g.id != ? ORDER BY g.display_name""",
        (db.ALL_USERS_GROUP,))
    last = db.q1("SELECT MAX(synced_at) AS last FROM groups")
    return render(request, "settings_groups.html", groups=groups, last=last,
                  cfg=entra.config_status(), section="groups")


@app.post("/settings/groups/sync")
def settings_groups_sync():
    if not entra.is_configured():
        return back("/settings/groups", "Entra ID is not configured yet")
    try:
        r = entra.sync_groups()
    except Exception as exc:
        return back("/settings/groups", f"Sync failed: {why(exc)}"[:300])
    msg = (f"Synced {r['groups']} group(s); {r['members_linked']} membership(s) linked")
    if r["members_unknown"]:
        msg += f", {r['members_unknown']} member(s) not known here - sync users first"
    return back("/settings/groups", msg)


@app.get("/settings/groups/{group_id}", response_class=HTMLResponse)
def group_detail(request: Request, group_id: str):
    group = db.q1("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not group:
        return HTMLResponse("<h1>404</h1><p>No such group.</p>", status_code=404)
    members = db.q(
        """SELECT u.* FROM group_members gm JOIN users u ON u.upn = gm.upn
           WHERE gm.group_id = ? ORDER BY u.display_name""", (group_id,))
    group_rules = db.q("SELECT * FROM rules WHERE group_id = ?", (group_id,))
    unlinked = db.q(
        "SELECT upn FROM group_members_unlinked WHERE group_id = ? ORDER BY upn",
        (group_id,))
    return render(request, "settings_group_detail.html", g=group, members=members,
                  group_rules=group_rules, unlinked=unlinked,
                  unlinked_reason=entra.unlinked_reason(), section="groups")


# --- settings: devices (Intune) -----------------------------------------

def _device_query(q: str, os_filter: str, linked: str) -> tuple[str, list]:
    sql = """SELECT d.*, a.name AS asset_name FROM devices d
             LEFT JOIN assets a ON a.id = d.asset_id"""
    where, params = [], []
    if q:
        where.append("(COALESCE(d.device_name,'') LIKE ? OR COALESCE(d.serial_number,'') LIKE ?"
                     " OR COALESCE(d.primary_upn,'') LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if os_filter:
        where.append("COALESCE(d.os,'') = ?")
        params.append(os_filter)
    if linked == "unlinked":
        where.append("d.asset_id IS NULL")
    elif linked == "linked":
        where.append("d.asset_id IS NOT NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql + " ORDER BY d.device_name", params


@app.get("/settings/devices", response_class=HTMLResponse)
def settings_devices(request: Request, q: str = "", os_filter: str = "",
                     linked: str = ""):
    sql, params = _device_query(q, os_filter, linked)
    devices = db.q(sql, params)
    oses = db.q("SELECT DISTINCT COALESCE(os,'') o FROM devices ORDER BY o")
    attrs = {}
    for row in db.q("SELECT device_id, name, value FROM device_attributes ORDER BY name"):
        attrs.setdefault(row["device_id"], []).append(row)
    last = db.q1("SELECT MAX(synced_at) AS last FROM devices")
    counts = db.q1(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN asset_id IS NULL THEN 1 ELSE 0 END) AS unlinked,
                  (SELECT COUNT(*) FROM device_attributes) AS attributes
           FROM devices""")
    unlinked_here = sum(1 for d in devices if not d["asset_id"])
    return render(request, "settings_devices.html", devices=devices, attrs=attrs,
                  oses=oses, q=q, os_filter=os_filter, linked=linked,
                  unlinked_here=unlinked_here, last=last, counts=counts,
                  cfg=entra.config_status(),
                  attr_filter=settings.get("INTUNE_ATTRIBUTE_FILTER"),
                  attr_names=db.q("SELECT DISTINCT name FROM device_attributes ORDER BY name"),
                  section="devices")


@app.post("/settings/devices/sync")
def settings_devices_sync():
    if not entra.is_configured():
        return back("/settings/devices", "Entra ID is not configured yet")
    try:
        r = entra.sync_devices()
    except Exception as exc:
        return back("/settings/devices", f"Device sync failed: {why(exc)}"[:300])
    return back("/settings/devices",
                f"Synced {r['devices']} device(s); {r['linked_to_assets']} matched an asset by serial")


@app.post("/settings/devices/sync-attributes")
def settings_devices_sync_attrs():
    if not entra.is_configured():
        return back("/settings/devices", "Entra ID is not configured yet")
    try:
        r = entra.sync_custom_attributes()
    except Exception as exc:
        return back("/settings/devices", f"Attribute sync failed: {why(exc)}"[:300])
    msg = (f"Synced {r['scripts_synced']} of {r['scripts_found']} attribute(s); "
           f"stored {r['attributes_stored']} value(s)")
    if r["filtered_out"]:
        msg += f"; {r['filtered_out']} left out by the filter"
    if r["stale_attributes_removed"]:
        msg += f"; cleared {r['stale_attributes_removed']} no longer covered"
    if r["skipped_values"]:
        msg += f"; {r['skipped_values']} had no value or an unknown device"
    if r["available"]:
        msg += ". Available: " + ", ".join(r["available"][:8])
    return back("/settings/devices", msg[:400])


def _asset_from_device(d) -> tuple[int, int | None]:
    """Create an asset for an Intune device and link them.

    Returns (asset_id, price_applied). Named after the model, since an asset
    record is about the kit; the hostname goes in the notes.
    """
    category = "Laptop" if (d["os"] or "").lower() in ("macos", "windows") else "Other"
    upn = d["primary_upn"] if d["primary_upn"] and db.q1(
        "SELECT 1 FROM users WHERE upn = ?", (d["primary_upn"],)) else None
    name = (d["model"] or d["device_name"] or "Device").strip()
    hostname = (d["device_name"] or "").strip()
    notes = f"Created from Intune device {hostname}".strip() if hostname \
        else "Created from Intune"
    asset_id = db.execute(
        """INSERT INTO assets (name, category, cost_cents, currency, rate_micro, serial,
                               notes, assigned_upn, assigned_on)
           VALUES (?,?,0,?,?,?,?,?,?)""",
        (name, category, fx.reporting_code(), fx.MICRO, d["serial_number"], notes,
         upn, today() if upn else None))
    db.execute("UPDATE devices SET asset_id = ? WHERE id = ?", (asset_id, d["id"]))

    # A pricing group covering this specification prices it straight away, in
    # the currency that group was priced in.
    hit = pricing.price_for_asset(asset_id)
    if hit:
        db.execute(
            "UPDATE assets SET cost_cents = ?, currency = ?, rate_micro = ? WHERE id = ?",
            (hit["price_cents"], hit["currency"], hit["rate_micro"], asset_id))
    return asset_id, hit


@app.post("/settings/devices/{device_id}/create-asset")
def device_create_asset(device_id: str):
    """Turn one Intune device into a tracked asset, keeping them linked."""
    d = db.q1("SELECT * FROM devices WHERE id = ?", (device_id,))
    if not d:
        return back("/settings/devices", "No such device")
    if d["asset_id"]:
        return back("/settings/devices", "That device is already linked to an asset")
    _, hit = _asset_from_device(d)
    if hit:
        return back("/settings/devices",
                    f"Asset created, linked and priced at "
                    f"{fx.money(hit['price_cents'], hit['currency'])} from a pricing group")
    return back("/settings/devices",
                "Asset created and linked - set its cost on the Assets page")


@app.post("/settings/devices/create-assets")
def devices_create_assets(request: Request, q: str = Form(""), os_filter: str = Form(""),
                          linked: str = Form("")):
    """Create assets for every unlinked device matching the current filters.

    Scoped to what the page was showing, so a filtered view creates assets for
    that subset rather than the whole estate.
    """
    sql, params = _device_query(q, os_filter, "unlinked")
    devices = db.q(sql, params)
    if not devices:
        return back("/settings/devices", "No unlinked devices match those filters")
    created = priced = 0
    for d in devices:
        _, hit = _asset_from_device(d)
        created += 1
        if hit:
            priced += 1
    msg = f"Created {created} asset(s) from Intune"
    if priced:
        msg += f", {priced} priced from a pricing group"
    if created - priced:
        msg += f"; {created - priced} need a cost setting"
    return back("/settings/devices", msg)


# --- settings: licences (from Entra) ------------------------------------

@app.get("/settings/licences", response_class=HTMLResponse)
def settings_licences(request: Request):
    rows = db.q(
        """SELECT l.*,
                  (SELECT COUNT(*) FROM user_licenses ul WHERE ul.sku_id = l.sku_id) AS held_here,
                  s.id AS sub_id, s.monthly_cost_cents
           FROM licenses l
           LEFT JOIN subscriptions s ON s.sku_id = l.sku_id
           ORDER BY l.display_name""")
    last = db.q1("SELECT MAX(synced_at) AS last FROM licenses")
    totals = db.q1(
        """SELECT COALESCE(SUM(prepaid),0) AS prepaid,
                  COALESCE(SUM(consumed),0) AS consumed FROM licenses""")
    # Licences held by accounts that are disabled in Entra: money being spent
    # on people who cannot sign in.
    reclaimable = db.q(
        """SELECT l.display_name, u.upn, u.display_name AS person
           FROM user_licenses ul
           JOIN licenses l ON l.sku_id = ul.sku_id
           JOIN users u ON u.upn = ul.upn
           WHERE u.account_enabled = 0
           ORDER BY l.display_name, u.display_name""")
    return render(request, "settings_licences.html", licences=rows, last=last,
                  totals=totals, reclaimable=reclaimable,
                  cfg=entra.config_status(), section="licences")


@app.post("/settings/licences/sync")
def settings_licences_sync():
    if not entra.is_configured():
        return back("/settings/licences", "Entra ID is not configured yet")
    try:
        r = entra.sync_licenses()
    except Exception as exc:
        return back("/settings/licences", f"Licence sync failed: {why(exc)}"[:300])
    msg = f"Synced {r['skus']} SKU(s) and {r['assignments']} assignment(s)"
    if r["licensed_not_synced"]:
        msg += f"; {r['licensed_not_synced']} licensed account(s) are not synced here"
    if r["unknown_skus"]:
        msg += f"; {r['unknown_skus']} assignment(s) referenced an unknown SKU"
    return back("/settings/licences", msg)


@app.post("/settings/licences/{sku_id}/create-subscription")
def licence_create_subscription(sku_id: str):
    """Turn an Entra licence into a tracked subscription, and grant its seats.

    Cost is left at zero: Entra knows who holds a licence, not what you pay for
    it. Set the per-seat price on the Subscriptions page.
    """
    lic = db.q1("SELECT * FROM licenses WHERE sku_id = ?", (sku_id,))
    if not lic:
        return back("/settings/licences", "No such licence")
    existing = db.q1("SELECT id FROM subscriptions WHERE sku_id = ?", (sku_id,))
    if existing:
        return back(f"/subscriptions/{existing['id']}",
                    "That licence already has a subscription")

    sub_id = db.execute(
        """INSERT INTO subscriptions (name, vendor, monthly_cost_cents, notes, sku_id)
           VALUES (?,?,0,?,?)""",
        (lic["display_name"], "Microsoft",
         f"Created from Entra licence {lic['sku_part_number']}", sku_id))

    # Mirror the current holders, so the seat count matches Entra straight away.
    seats = 0
    for row in db.q("SELECT upn FROM user_licenses WHERE sku_id = ?", (sku_id,)):
        db.execute(
            """INSERT OR IGNORE INTO subscription_seats (subscription_id, upn, assigned_on)
               VALUES (?,?,?)""", (sub_id, row["upn"], today()))
        seats += 1
    return back(f"/subscriptions/{sub_id}",
                f"Subscription created with {seats} seat(s) - set the per-seat cost")


@app.get("/settings/licences/{sku_id}", response_class=HTMLResponse)
def licence_detail(request: Request, sku_id: str):
    lic = db.q1("SELECT * FROM licenses WHERE sku_id = ?", (sku_id,))
    if not lic:
        return HTMLResponse("<h1>404</h1><p>No such licence.</p>", status_code=404)
    holders = db.q(
        """SELECT u.upn, u.display_name, u.department, u.country, u.account_enabled
           FROM user_licenses ul JOIN users u ON u.upn = ul.upn
           WHERE ul.sku_id = ? ORDER BY u.display_name""", (sku_id,))
    sub = db.q1("SELECT * FROM subscriptions WHERE sku_id = ?", (sku_id,))
    return render(request, "settings_licence_detail.html", l=lic, holders=holders,
                  sub=sub, section="licences")


# --- settings: currencies ------------------------------------------------

@app.get("/settings/currencies", response_class=HTMLResponse)
def settings_currencies(request: Request):
    fx.ensure_base()
    rows = []
    for c in fx.listing():
        rows.append({"c": c, "usage": fx.usage(c["code"]),
                     "rate": fx.format_rate(c["rate_micro"])})
    return render(request, "settings_currencies.html", rows=rows,
                  reporting=fx.reporting_code(), proposal=None,
                  fetch_error=None, section="currencies")


@app.post("/settings/currencies/check", response_class=HTMLResponse)
def currencies_check(request: Request):
    """Ask Bank of Israel for today's rates. Nothing is stored until approved."""
    if not require_admin(request):
        return back("/settings/currencies", "Admin accounts only")
    fx.ensure_base()
    proposal, error = None, None
    try:
        proposal = fx.fetch_boi()
    except fx.RateFetchError as exc:
        error = str(exc)
    rows = [{"c": c, "usage": fx.usage(c["code"]),
             "rate": fx.format_rate(c["rate_micro"])} for c in fx.listing()]
    return render(request, "settings_currencies.html", rows=rows,
                  reporting=fx.reporting_code(), proposal=proposal,
                  fetch_error=error, section="currencies")


@app.post("/settings/currencies/approve")
async def currencies_approve(request: Request):
    """Apply only the rates that were ticked."""
    if not require_admin(request):
        return back("/settings/currencies", "Admin accounts only")
    form = await request.form()
    picked = [k[len("rate_"):] for k in form.keys() if k.startswith("rate_")]
    if not picked:
        return back("/settings/currencies", "Nothing was ticked, so nothing changed")
    as_of = str(form.get("as_of") or "")
    by = request.state.user["username"]
    applied = []
    for code in picked:
        micro = fx.parse_rate(str(form.get(f"value_{code}") or ""))
        if not micro:
            continue
        fx.add(code, str(form.get(f"symbol_{code}") or code),
               str(form.get(f"name_{code}") or code), micro, "boi", by, as_of)
        applied.append(code)
    if not applied:
        return back("/settings/currencies", "None of the ticked rates were readable")
    return back("/settings/currencies",
                f"Approved {len(applied)} rate(s) from Bank of Israel: "
                + ", ".join(sorted(applied)))


@app.post("/settings/currencies/manual")
def currencies_manual(request: Request, code: str = Form(...), symbol: str = Form(""),
                      name: str = Form(""), rate: str = Form("")):
    if not require_admin(request):
        return back("/settings/currencies", "Admin accounts only")
    code = code.strip().upper()
    if not (2 <= len(code) <= 4) or not code.isalpha():
        return back("/settings/currencies", "A currency code is 3 letters, such as ILS")
    if code == fx.reporting_code():
        return back("/settings/currencies",
                    f"{code} is the reporting currency and is always 1.0")
    micro = fx.parse_rate(rate)
    if not micro:
        return back("/settings/currencies",
                    "Enter the rate as a number, such as 0.3357 for shekels to dollars")
    fx.add(code, symbol or fx.KNOWN_SYMBOLS.get(code, code),
           name or fx.KNOWN_NAMES.get(code, code), micro, "manual",
           request.state.user["username"])
    return back("/settings/currencies", f"{code} set to {fx.format_rate(micro)}")


@app.post("/settings/currencies/{code}/delete")
def currencies_delete(request: Request, code: str):
    if not require_admin(request):
        return back("/settings/currencies", "Admin accounts only")
    problem = fx.delete(code)
    return back("/settings/currencies", problem or f"{code.upper()} removed")


@app.post("/settings/currencies/{code}/toggle")
def currencies_toggle(request: Request, code: str, active: str = Form("")):
    if not require_admin(request):
        return back("/settings/currencies", "Admin accounts only")
    problem = fx.set_active(code, bool(active))
    return back("/settings/currencies", problem or f"{code.upper()} updated")


# --- settings: pricing groups -------------------------------------------

@app.get("/settings/pricing", response_class=HTMLResponse)
def settings_pricing(request: Request):
    groups = []
    for g in pricing.listing():
        s = pricing.summary(g)
        groups.append({"g": g, "criteria": s["criteria"], "matched": s["matched"],
                       "to_change": s["to_change"],
                       "describe": [pricing.describe(c) for c in s["criteria"]]})
    return render(request, "settings_pricing.html", groups=groups,
                  models=pricing.known_models(),
                  currencies=fx.listing(active_only=True), section="pricing")


@app.post("/settings/pricing/new")
def pricing_new(name: str = Form(...), price: str = Form("0"), currency: str = Form(""),
                notes: str = Form(""), model: str = Form("")):
    if not name.strip():
        return back("/settings/pricing", "Give the group a name")
    code, rate, problem = pick_currency(currency)
    if problem:
        return back("/settings/pricing", problem)
    gid = pricing.create(name, db.to_cents(price), notes, currency=code, rate_micro=rate)
    # A model is the usual starting point, so offer it on creation.
    if model.strip():
        pricing.add_criterion(gid, "model", "eq", model)
    return back(f"/settings/pricing/{gid}", "Group created - add criteria to narrow it")


@app.get("/settings/pricing/{group_id}", response_class=HTMLResponse)
def pricing_detail(request: Request, group_id: int):
    group = pricing.get(group_id)
    if not group:
        return HTMLResponse("<h1>404</h1><p>No such pricing group.</p>", status_code=404)
    s = pricing.summary(group)
    return render(request, "settings_pricing_detail.html", g=group, s=s,
                  describe=pricing.describe, fields=pricing.FIELDS, ops=pricing.OPS,
                  attribute_names=pricing.attribute_names(),
                  models=pricing.known_models(),
                  currencies=fx.listing(active_only=True), section="pricing")


@app.post("/settings/pricing/{group_id}/edit")
def pricing_edit(group_id: int, name: str = Form(...), price: str = Form("0"),
                 currency: str = Form(""), notes: str = Form("")):
    existing = pricing.get(group_id)
    if not existing:
        return back("/settings/pricing", "No such group")
    code, rate, problem = pick_currency(currency)
    if problem:
        return back(f"/settings/pricing/{group_id}", problem)
    if code == (existing["currency"] or "") and existing["rate_micro"]:
        rate = int(existing["rate_micro"])
    pricing.update(group_id, name, db.to_cents(price), notes,
                   currency=code, rate_micro=rate)
    return back(f"/settings/pricing/{group_id}", "Group updated")


@app.post("/settings/pricing/{group_id}/delete")
def pricing_delete(group_id: int):
    pricing.delete(group_id)
    return back("/settings/pricing", "Group deleted - asset prices are left as they are")


@app.post("/settings/pricing/{group_id}/criteria/add")
def pricing_criterion_add(group_id: int, field: str = Form(...), op: str = Form(...),
                          value: str = Form(""), attr_name: str = Form("")):
    if not pricing.get(group_id):
        return back("/settings/pricing", "No such group")
    if not value.strip():
        return back(f"/settings/pricing/{group_id}", "Give the criterion a value")
    try:
        pricing.add_criterion(group_id, field, op, value, attr_name)
    except ValueError as exc:
        return back(f"/settings/pricing/{group_id}", str(exc))
    return back(f"/settings/pricing/{group_id}", "Criterion added")


@app.post("/settings/pricing/{group_id}/criteria/{criterion_id}/delete")
def pricing_criterion_delete(group_id: int, criterion_id: int):
    pricing.delete_criterion(criterion_id)
    return back(f"/settings/pricing/{group_id}", "Criterion removed")


@app.post("/settings/pricing/{group_id}/apply")
def pricing_apply(group_id: int):
    group = pricing.get(group_id)
    if not group:
        return back("/settings/pricing", "No such group")
    if not pricing.criteria(group_id):
        return back(f"/settings/pricing/{group_id}",
                    "Add at least one criterion first - an empty group would match nothing")
    r = pricing.apply(group)
    return back(f"/settings/pricing/{group_id}",
                f"Priced {r['changed']} asset(s) at {settings.currency()} "
                f"{db.money(group['price_cents'])}")


# --- settings: rules -----------------------------------------------------

@app.get("/settings/rules", response_class=HTMLResponse)
def settings_rules(request: Request):
    return render(request, "settings_rules.html",
                  overview=rules.compliance_overview(),
                  items_by_category=rules.assets_by_category(),
                  grants_label=rules.grants_label,
                  groups=db.q(
                      """SELECT * FROM groups
                         ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END, display_name""",
                      (db.ALL_USERS_GROUP,)),
                  all_users_group=db.ALL_USERS_GROUP,
                  people=db.q1("SELECT COUNT(*) c FROM users")["c"],
                  subs=db.q("SELECT * FROM subscriptions ORDER BY name"),
                  section="rules")


@app.post("/settings/rules/new")
async def rule_new(request: Request):
    """Create a rule, with its exceptions, in one step."""
    form = await request.form()
    name = str(form.get("name") or "").strip()
    group_id = str(form.get("group_id") or "")
    kind = str(form.get("kind") or "")
    category = str(form.get("category") or "").strip()
    subscription_id = str(form.get("subscription_id") or "").strip()
    quantity = str(form.get("quantity") or "1")
    excludes = [g for g in form.getlist("exclude") if g]
    includes = [g for g in form.getlist("include") if g]

    if not name:
        return back("/settings/rules", "Give the rule a name")
    if kind not in ("asset", "subscription"):
        return back("/settings/rules",
                    "Choose whether the rule grants an asset or a licence")
    if not db.q1("SELECT 1 FROM groups WHERE id = ?", (group_id,)):
        return back("/settings/rules", "Choose the group the rule applies to")
    try:
        qty = max(1, int(quantity))
    except ValueError:
        return back("/settings/rules", "Quantity must be a whole number")

    if kind == "asset":
        if not category:
            return back("/settings/rules", "Choose what the rule grants")
        rule_id = rules.create(name, group_id, "asset", qty, category=category,
                               asset_name=str(form.get("asset_name") or "").strip())
    else:
        if not subscription_id.isdigit():
            return back("/settings/rules", "Choose which subscription the rule grants")
        # One seat per person, regardless of what was typed.
        rule_id = rules.create(name, group_id, "subscription", 1,
                               subscription_id=int(subscription_id))

    refused = 0
    for gid in excludes:
        if rules.add_group(rule_id, gid, "exclude"):
            refused += 1
    for gid in includes:
        if rules.add_group(rule_id, gid, "include"):
            refused += 1

    msg = "Rule created"
    if excludes:
        msg += f" with {len(excludes) - refused} exception(s)"
    return back(f"/settings/rules/{rule_id}", msg)


@app.post("/settings/rules/{rule_id}/delete")
def rule_delete(rule_id: int):
    rules.delete(rule_id)
    return back("/settings/rules", "Rule deleted")


@app.post("/settings/rules/{rule_id}/toggle")
def rule_toggle(rule_id: int, active: str = Form("")):
    rules.set_active(rule_id, bool(active))
    return back("/settings/rules", "Rule updated")


@app.post("/settings/rules/{rule_id}/groups/add")
def rule_group_add(rule_id: int, group_id: str = Form(...), mode: str = Form(...)):
    if not rules.get(rule_id):
        return back("/settings/rules", "No such rule")
    problem = rules.add_group(rule_id, group_id, mode)
    return back(f"/settings/rules/{rule_id}",
                problem or ("Group added" if mode == "include" else "Group excluded"))


@app.post("/settings/rules/{rule_id}/groups/remove")
def rule_group_remove(rule_id: int, group_id: str = Form(...), mode: str = Form(...)):
    rules.remove_group(rule_id, group_id, mode)
    return back(f"/settings/rules/{rule_id}", "Condition removed")


@app.post("/settings/rules/{rule_id}/fulfilment/clear")
def rule_fulfilment_clear(rule_id: int, upn: str = Form("")):
    if not rules.get(rule_id):
        return back("/settings/rules", "No such rule")
    if upn.strip():
        rules.clear_fulfilment(rule_id, upn.strip().lower())
        return back(f"/settings/rules/{rule_id}",
                    f"{upn.strip().lower()} can be served by this rule again")
    n = rules.clear_all_fulfilments(rule_id)
    return back(f"/settings/rules/{rule_id}",
                f"Cleared {n} record(s); the rule can serve everyone again")


@app.post("/settings/rules/{rule_id}/apply")
def rule_apply(rule_id: int):
    rule = rules.get(rule_id)
    if not rule:
        return back("/settings/rules", "No such rule")
    r = rules.apply(rule)
    msg = f"Assigned {r['granted']} item(s)"
    if r["shortfall"]:
        names = ", ".join(u["display_name"] for u in r["shortfall"][:3])
        more = "" if len(r["shortfall"]) <= 3 else f" and {len(r['shortfall']) - 3} more"
        msg += f"; no spares left for {names}{more}"
    return back("/settings/rules", msg)


@app.get("/settings/rules/{rule_id}", response_class=HTMLResponse)
def rule_detail(request: Request, rule_id: int):
    rule = rules.get(rule_id)
    if not rule:
        return HTMLResponse("<h1>404</h1><p>No such rule.</p>", status_code=404)
    return render(request, "settings_rule_detail.html", r=rule,
                  s=rules.summarise(rule), all_users_group=db.ALL_USERS_GROUP,
                  groups=db.q("SELECT * FROM groups ORDER BY display_name"),
                  grants_label=rules.grants_label, section="rules")


# --- settings: general ---------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_general(request: Request):
    counts = db.q1(
        """SELECT (SELECT COUNT(*) FROM users)         AS people,
                  (SELECT COUNT(*) FROM assets)        AS assets,
                  (SELECT COUNT(*) FROM subscriptions) AS subs,
                  (SELECT COUNT(*) FROM auth_users)    AS logins,
                  (SELECT COUNT(*) FROM api_keys WHERE active = 1) AS api_keys,
                  (SELECT COUNT(*) FROM groups)  AS groups,
                  (SELECT COUNT(*) FROM devices) AS devices,
                  (SELECT COUNT(*) FROM rules WHERE active = 1) AS rules"""
    )
    local_accounts = db.q(
        """SELECT username, is_admin, totp_enabled, sso FROM auth_users
           ORDER BY username""")
    tfa_ready = [a for a in local_accounts if a["totp_enabled"] or a["sso"]]
    env_only = [(k, os.environ.get(k) or "(not set)", why)
                for k, why in settings.ENV_ONLY.items()
                if not k.startswith("ITAM_ADMIN_")]
    return render(request, "settings_general.html", counts=counts,
                  fields=settings.group("general"), env_only=env_only,
                  db_path=db.DB_PATH, local_accounts=local_accounts,
                  tfa_ready=len(tfa_ready), require_2fa=auth.require_2fa(),
                  me_has_2fa=bool(request.state.user["totp_enabled"]),
                  section="general")


async def _read_form(request: Request) -> dict:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


@app.post("/settings/general/save")
async def settings_general_save(request: Request):
    if not require_admin(request):
        return back("/settings", "Admin accounts only")
    data = await _read_form(request)
    return _apply(data, "general", request.state.user["username"], "/settings")


@app.post("/settings/entra/save")
async def settings_entra_save(request: Request):
    if not require_admin(request):
        return back("/settings/entra", "Admin accounts only")
    data = await _read_form(request)
    return _apply(data, "entra", request.state.user["username"], "/settings/entra")


@app.post("/settings/sso/save")
async def settings_sso_save(request: Request):
    if not require_admin(request):
        return back("/settings/sso", "Admin accounts only")
    data = await _read_form(request)
    return _apply(data, "saml", request.state.user["username"], "/settings/sso")


@app.post("/settings/reset")
async def settings_reset(request: Request):
    """Drop an override so the .env value (or the default) applies again."""
    if not require_admin(request):
        return back("/settings", "Admin accounts only")
    data = await _read_form(request)
    key = data.get("key", "")
    redirect = data.get("redirect", "/settings")
    if key not in settings.SPEC:
        return back(redirect, "Unknown setting")
    settings.clear(key, request.state.user["username"])
    return back(redirect, f"'{key}' reset to the .env value")


def _apply(data: dict, group: str, by: str, redirect: str):
    changed, errors = 0, []
    for field in settings.group(group):
        key, kind = field["key"], field["kind"]
        if kind == "bool":
            # An unticked checkbox sends nothing, which is a real value here.
            value = "1" if data.get(key) else "0"
        else:
            if key not in data:
                continue
            value = data[key].strip()
            # A blank secret means "leave it alone", not "erase it".
            if field["secret"] and value == "":
                continue
        if kind == "int":
            try:
                int(value)
            except ValueError:
                errors.append(f"{field['label']} must be a whole number")
                continue
        if settings.raw(key) == value and settings.stored(key) is not None:
            continue
        settings.set_value(key, value, by)
        changed += 1
    if errors:
        return back(redirect, "; ".join(errors))
    return back(redirect, f"Saved {changed} setting(s)" if changed else "Nothing changed")


# --- settings: Entra ID -------------------------------------------------

@app.get("/settings/entra", response_class=HTMLResponse)
def admin_entra(request: Request):
    last = db.q1("SELECT MAX(synced_at) AS last, COUNT(*) AS n FROM users WHERE source='entra'")
    people = db.q1("SELECT COUNT(*) c FROM users")["c"]
    return render(request, "settings_entra.html", cfg=entra.config_status(), last=last,
                  people=people, fields=settings.group("entra"), probe=None,
                  section="entra")


@app.post("/settings/entra/test", response_class=HTMLResponse)
def admin_entra_test(request: Request):
    """Check the credentials and each permission separately."""
    if not require_admin(request):
        return back("/settings/entra", "Admin accounts only")
    if not entra.is_configured():
        return back("/settings/entra", "Fill in the tenant, client and secret first")
    last = db.q1("SELECT MAX(synced_at) AS last, COUNT(*) AS n FROM users WHERE source='entra'")
    people = db.q1("SELECT COUNT(*) c FROM users")["c"]
    return render(request, "settings_entra.html", cfg=entra.config_status(), last=last,
                  people=people, fields=settings.group("entra"),
                  probe=entra.test_connection(), section="entra")


@app.post("/settings/entra/sync")
def admin_sync():
    if not entra.is_configured():
        return back("/settings/entra", "Entra ID is not configured - set the environment variables first")
    try:
        r = entra.sync()
    except Exception as exc:  # surface the Graph error rather than a 500 page
        return back("/settings/entra", f"Sync failed: {why(exc)}"[:300])
    return back("/settings/entra", f"Synced {r['fetched']} users ({r['created']} new, {r['updated']} updated)")


# --- settings: local accounts -------------------------------------------

@app.get("/accounts")
def accounts_moved():
    return RedirectResponse("/settings/accounts", status_code=308)


@app.get("/admin")
@app.get("/admin/{rest:path}")
def admin_renamed(rest: str = ""):
    """Admin was renamed to Settings; keep old links and bookmarks working."""
    return RedirectResponse(f"/settings{'/' + rest if rest else ''}", status_code=308)


@app.get("/settings/accounts", response_class=HTMLResponse)
def accounts_page(request: Request):
    if not require_admin(request):
        return HTMLResponse("<h1>403</h1><p>Admin accounts only.</p>", status_code=403)
    return render(request, "settings_accounts.html", accounts=auth.list_users(),
                  section="accounts")


@app.get("/settings/sso", response_class=HTMLResponse)
def settings_sso(request: Request):
    if not require_admin(request):
        return HTMLResponse("<h1>403</h1><p>Admin accounts only.</p>", status_code=403)
    return render(request, "settings_sso.html", cfg=saml.config_status(),
                  fields=settings.group("saml"), section="sso")


# --- settings: API keys and field mapping -------------------------------

@app.get("/settings/api", response_class=HTMLResponse)
def admin_api(request: Request):
    if not require_admin(request):
        return HTMLResponse("<h1>403</h1><p>Admin accounts only.</p>", status_code=403)
    new_token = request.query_params.get("token")
    return render(request, "settings_api.html", keys=api.list_keys(),
                  mapping=db.q("SELECT * FROM api_field_map ORDER BY source_field"),
                  asset_fields=db.ASSET_FIELDS, log=api.recent_log(),
                  new_token=new_token, site=os.environ.get("ITAM_SITE_ADDRESS") or "your-itam-host",
                  section="api")


@app.post("/settings/api/keys/new")
def api_key_new(request: Request, name: str = Form(...), can_create_assets: str = Form(""),
                can_assign: str = Form("")):
    if not require_admin(request):
        return back("/settings/api", "Admin accounts only")
    if not name.strip():
        return back("/settings/api", "Give the key a name")
    token = api.create_key(name, bool(can_create_assets), bool(can_assign))
    # Shown once, via the redirect, then never again.
    return RedirectResponse(f"/settings/api?token={quote(token, safe='')}", status_code=303)


@app.post("/settings/api/keys/{key_id}/delete")
def api_key_delete(request: Request, key_id: int):
    if not require_admin(request):
        return back("/settings/api", "Admin accounts only")
    api.delete_key(key_id)
    return back("/settings/api", "API key deleted")


@app.post("/settings/api/keys/{key_id}/toggle")
def api_key_toggle(request: Request, key_id: int, active: str = Form("")):
    if not require_admin(request):
        return back("/settings/api", "Admin accounts only")
    api.set_key_active(key_id, bool(active))
    return back("/settings/api", "API key updated")


@app.post("/settings/api/mapping")
def api_mapping_set(request: Request, source_field: str = Form(...), target_field: str = Form(...)):
    if not require_admin(request):
        return back("/settings/api", "Admin accounts only")
    if target_field not in db.ASSET_FIELDS:
        return back("/settings/api", "Unknown target field")
    if not source_field.strip():
        return back("/settings/api", "Give the incoming field a name")
    api.set_mapping(source_field, target_field)
    return back("/settings/api", f"Mapped '{source_field.strip()}' to '{target_field}'")


@app.post("/settings/api/mapping/delete")
def api_mapping_delete(request: Request, source_field: str = Form(...)):
    if not require_admin(request):
        return back("/settings/api", "Admin accounts only")
    api.delete_mapping(source_field)
    return back("/settings/api", "Mapping removed")


@app.get("/export/costs.csv")
def export_costs():
    rows = db.q(USER_COSTS + " ORDER BY u.display_name")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["upn", "display_name", "department", "country", "account_enabled",
                "assets", f"asset_value_{settings.currency()}",
                "stock_units", f"stock_value_{settings.currency()}",
                f"onetime_total_{settings.currency()}",
                "subscriptions", f"monthly_{settings.currency()}",
                f"annual_{settings.currency()}"])
    for r in rows:
        w.writerow([r["upn"], r["display_name"], r["department"] or "",
                    r["country"] or "", r["account_enabled"],
                    r["asset_count"], db.money(r["asset_total"]).replace(",", ""),
                    r["stock_units"], db.money(r["stock_total"]).replace(",", ""),
                    db.money(r["onetime_total"]).replace(",", ""),
                    r["sub_count"], db.money(r["monthly_total"]).replace(",", ""),
                    db.money(r["monthly_total"] * 12).replace(",", "")])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="itam-costs.csv"'})
