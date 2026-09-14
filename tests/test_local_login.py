"""Turning off username-and-password sign-in, without locking anybody out.

The point of the switch is that an internet-facing app stops accepting
passwords at all. The point of the safety net is that it can never be the
reason nobody can get in.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "LocalTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient           # noqa: E402
from app import auth, db, main, saml, settings      # noqa: E402

db.init_db()
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


CERT = "MIIC8DCCAdigAwIBAgIQFAKECERTFAKECERTFAKECERT"


def configure_sso():
    settings.set_value("ITAM_SAML_SP_BASE_URL", "https://itam.example.com", "test")
    settings.set_value("ITAM_SAML_IDP_ENTITY_ID", "https://sts.windows.net/x/", "test")
    settings.set_value("ITAM_SAML_IDP_SSO_URL",
                       "https://login.microsoftonline.com/x/saml2", "test")
    settings.set_value("ITAM_SAML_IDP_CERT", CERT, "test")


def unconfigure_sso():
    for key in ("ITAM_SAML_IDP_ENTITY_ID", "ITAM_SAML_IDP_SSO_URL",
                "ITAM_SAML_IDP_CERT"):
        settings.set_value(key, "", "test")


def sign_in(client):
    return client.post("/login", data={"username": "admin",
                                       "password": "LocalTest!2345"},
                       follow_redirects=False)


def signed_out(client):
    """The sign-in page as a stranger sees it - a session redirects away."""
    client.cookies.clear()
    return client.get("/login").text


with TestClient(main.app) as client:
    print("--- with it off, passwords work as before ---")
    check("SSO is not set up yet", saml.is_configured(), False)
    check("so local sign-in is allowed", auth.local_login_allowed(), True)
    check("and it works", sign_in(client).headers["location"], "/")
    check("the form is on the page", 'name="password"' in signed_out(client), True)

    print("\n--- the switch does nothing until SSO is finished ---")
    settings.set_value("ITAM_LOCAL_LOGIN_DISABLED", "1", "test")
    check("still allowed, because there would be no other way in",
          auth.local_login_allowed(), True)
    check("and signing in still works", sign_in(client).headers["location"], "/")

    print("\n--- once SSO works, passwords stop ---")
    configure_sso()
    check("SSO is configured", saml.is_configured(), True)
    check("local sign-in is refused", auth.local_login_allowed(), False)
    page = signed_out(client)
    check("the form is gone", 'name="password"' in page, False)
    check("Microsoft is still offered", "Sign in with Microsoft" in page, True)

    print("\n--- and the endpoint refuses, not just the page ---")
    # A form that is not drawn is still an endpoint anybody can post to.
    r = sign_in(client)
    check("posting the right password is refused",
          "turned+off" in r.headers["location"], True)
    check("no session was issued", "itam_session" in r.headers.get("set-cookie", ""),
          False)

    print("\n--- clearing the SAML settings brings passwords back by itself ---")
    unconfigure_sso()
    check("allowed again", auth.local_login_allowed(), True)
    check("and it works", sign_in(client).headers["location"], "/")
    check("the switch is still set, waiting",
          settings.get_bool("ITAM_LOCAL_LOGIN_DISABLED"), True)

    print("\n--- the way back in when Entra is down ---")
    configure_sso()
    check("locked to SSO", auth.local_login_allowed(), False)
    # What the documented recovery command does, run on the box.
    settings.set_value("ITAM_LOCAL_LOGIN_DISABLED", "0", "recovery")
    check("passwords work again, with no restart", auth.local_login_allowed(), True)
    check("and signing in works", sign_in(client).headers["location"], "/")

    print("\n--- the settings page says which of the three states it is in ---")
    settings.set_value("ITAM_LOCAL_LOGIN_DISABLED", "1", "test")
    page = client.get("/settings/sso").text
    check("in effect", "Username and password are turned off" in page, True)
    unconfigure_sso()
    page = client.get("/settings/sso").text
    check("set but not in effect", "Not in effect yet" in page, True)
    check("and the recovery command is on the page",
          "ITAM_LOCAL_LOGIN_DISABLED&#39;,&#39;0&#39;" in page
          or "ITAM_LOCAL_LOGIN_DISABLED','0'" in page, True)

    print("\n--- the sign-in page does not say what it is guarding ---")
    settings.set_value("ITAM_LOCAL_LOGIN_DISABLED", "0", "test")
    unconfigure_sso()
    page = signed_out(client)
    for word in ("ITAM", "asset", "subscription", "tracking"):
        check(f"no {word!r} on the page", word.lower() in page.lower(), False)
    check("but it is still a sign-in page", "<h1>Sign in</h1>" in page, True)
    check("and the title gives nothing away either",
          "<title>Sign in</title>" in page, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
