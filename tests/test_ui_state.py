"""Filters survive a form, and collapsed panels stay collapsed.

Adding a device from a filtered view used to hand back the whole unfiltered
list with every panel wide open again. Both are the same complaint: the page
forgot what you had set up.
"""
import os, re, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "UiTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
from app import db, fx, pooled
db.init_db(); fx.ensure_base()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

db.execute("INSERT INTO users (upn, display_name, source) VALUES ('yael@x.com','Yael','entra')")
db.execute("""INSERT INTO assets (name, category, cost_cents, currency, rate_micro, serial)
              VALUES ('MacBook Air 13 M4','Laptop',0,'USD',1000000,'SN-1')""")
pooled.create("Keychron K3", "Peripheral", 0, currency="USD", rate_micro=1000000)

from fastapi.testclient import TestClient          # noqa: E402
from app import main                               # noqa: E402

with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "UiTest!2345"},
                follow_redirects=False)

    print("--- the form sends you back to the page you were on ---")
    def redirect_field(url):
        # Unescaped: "&" is written "&amp;" in an attribute, which is correct
        # HTML and is what the browser hands back as a plain "&".
        import html
        page = client.get(url).text
        return [html.unescape(v) for v in
                re.findall(r'name="redirect" value="([^"]*)"', page)]

    check("unfiltered", redirect_field("/assets")[0], "/assets")
    check("a category page", redirect_field("/assets/c/Laptop")[0], "/assets/c/Laptop")
    check("with a filter on", redirect_field("/assets?priced=unpriced")[0],
          "/assets?priced=unpriced")
    check("several filters, all kept",
          redirect_field("/assets/c/Laptop?q=MacBook&state=spare&priced=unpriced")[0],
          "/assets/c/Laptop?q=MacBook&state=spare&priced=unpriced")
    check("both forms on the page agree",
          len(set(redirect_field("/assets?priced=unpriced"))), 1)

    print("\n--- and the flash from last time is not carried into the next one ---")
    check("msg dropped", redirect_field("/assets?priced=unpriced&msg=Asset+added")[0],
          "/assets?priced=unpriced")

    print("\n--- adding from a filtered view lands back on it ---")
    r = client.post("/assets/new", follow_redirects=False, data={
        "name": "Dell U2723QE", "category": "Monitor", "cost": "599.00",
        "currency": "USD", "redirect": "/assets/c/Laptop?state=spare&priced=unpriced"})
    check("redirects", r.status_code, 303)
    location = r.headers["location"]
    check("back to the same filtered page",
          location.startswith("/assets/c/Laptop?state=spare&priced=unpriced&msg="), True)
    check("and the filters still work when followed",
          client.get(location.split("&msg=")[0]).status_code, 200)

    print("\n--- a person's page keeps its place too ---")
    check("user detail", redirect_field("/users/yael@x.com")[0], "/users/yael@x.com")

    print("\n--- every panel has a stable key to be remembered by ---")
    import pathlib                                  # noqa: E402
    TPL = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
    untagged, keys = [], []
    for path in sorted(TPL.glob("*.html")):
        text = path.read_text()
        for match in re.finditer(r'<details class="panel"([^>]*)>', text):
            attrs = match.group(1)
            found = re.search(r'data-panel="([^"]+)"', attrs)
            if not found:
                untagged.append(f"{path.name}: {match.group(0)[:60]}")
            else:
                keys.append(found.group(1))
    check("none left without one", untagged, [])
    check("and the keys are unique", sorted(k for k in keys if keys.count(k) > 1), [])
    check("something was actually found", len(keys) > 10, True)

    print("\n--- the key is not derived from text that moves ---")
    # settings_devices carries "Ignored devices 7 of 14" in its summary; keying
    # on that would lose the setting the moment a device was added.
    devices_html = (TPL / "settings_devices.html").read_text()
    check("that panel has an explicit key",
          'data-panel="ignored-devices"' in devices_html, True)

    print("\n--- the page ships the script that does the remembering ---")
    page = client.get("/assets").text
    check("keyed storage", "itam.panels" in page, True)
    check("reads data-panel", "details[data-panel]" in page, True)
    check("a stored choice beats the server default", "hasOwnProperty" in page, True)
    check("private-mode writes cannot throw the page over",
          page.count("catch (e)") >= 2, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
