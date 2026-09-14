"""Reading Entra's federation metadata, instead of copying three fields.

The identifier, the login URL and a base64 certificate were being copied
across a screen by hand, one at a time. Entra publishes all three at one URL.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")
os.environ["ITAM_ADMIN_USER"] = "admin"
os.environ["ITAM_ADMIN_PASSWORD"] = "SamlTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient           # noqa: E402
from app import db, main, saml, settings            # noqa: E402

db.init_db()
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


CERT = ("MIIC8DCCAdigAwIBAgIQY3kL2yQAAAAAAAAAAAAAADANBgkqhkiG9w0BAQsFADA0"
        "MTIwMAYDVQQDEylNaWNyb3NvZnQgQXp1cmUgRmVkZXJhdGVkIFNTTyBDZXJ0aWZp"
        "Y2F0ZTAeFw0yNjA4MDUxMDAyNDJa")

ENTRA = f"""<?xml version="1.0" encoding="utf-8"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata"
    entityID="https://sts.windows.net/4e0842e5-8500-0000-0000-000000000000/">
  <IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <KeyDescriptor use="signing">
      <KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">
        <X509Data><X509Certificate>{CERT}</X509Certificate></X509Data>
      </KeyInfo>
    </KeyDescriptor>
    <SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        Location="https://login.microsoftonline.com/tenant/saml2"/>
    <SingleSignOnService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
        Location="https://login.microsoftonline.com/tenant/saml2"/>
  </IDPSSODescriptor>
</EntityDescriptor>
"""

print("--- one paste gives all three ---")
found = saml.read_metadata(ENTRA)
check("the Entra identifier", found["entity_id"],
      "https://sts.windows.net/4e0842e5-8500-0000-0000-000000000000/")
check("the login URL", found["sso_url"],
      "https://login.microsoftonline.com/tenant/saml2")
check("the certificate, stripped of its wrapping",
      found["cert"].startswith("MIIC8DCC") and "\n" not in found["cert"], True)

print("\n--- a certificate marked for encryption is not the signing one ---")
two_keys = ENTRA.replace('<KeyDescriptor use="signing">',
                         '<KeyDescriptor use="encryption">'
                         '<KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">'
                         '<X509Data><X509Certificate>WRONGWRONGWRONG</X509Certificate>'
                         '</X509Data></KeyInfo></KeyDescriptor>'
                         '<KeyDescriptor use="signing">', 1)
check("the signing key is chosen, not whichever came first",
      saml.read_metadata(two_keys)["cert"].startswith("MIIC8DCC"), True)

print("\n--- and the things that should be refused ---")


def refusal(source):
    try:
        saml.read_metadata(source)
        return None
    except saml.MetadataError as exc:
        return str(exc)


check("nothing pasted", refusal("").startswith("Paste the metadata"), True)
check("neither URL nor XML", "neither a URL nor XML" in refusal("tenant-id-maybe"), True)
# A DOCTYPE is where entity-expansion attacks live; metadata has no use for one.
check("a DOCTYPE is refused rather than parsed",
      "DOCTYPE" in refusal('<!DOCTYPE a [<!ENTITY x "y">]><EntityDescriptor/>'), True)
check("XML that is not metadata", "not SAML metadata" in refusal("<html><body/></html>"),
      True)
check("broken XML", "not valid XML" in refusal("<EntityDescriptor"), True)
check("a service provider's own metadata, pasted by mistake",
      "describes no identity provider" in refusal(
          '<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" '
          'entityID="https://itam.remedio.io/saml/metadata">'
          '<SPSSODescriptor protocolSupportEnumeration="x"/></EntityDescriptor>'), True)

print("\n--- half of it is not written, and says which half is missing ---")
no_cert = ENTRA.replace(f"<X509Certificate>{CERT}</X509Certificate>", "")
check("the missing part is named",
      "signing certificate" in refusal(no_cert), True)
no_sso = ENTRA.replace('Location="https://login.microsoftonline.com/tenant/saml2"',
                       'Location=""')
check("and so is this one", "login URL" in refusal(no_sso), True)

print("\n--- the page does it in one submission ---")
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "SamlTest!2345"},
                follow_redirects=False)
    r = client.post("/settings/sso/import", data={"metadata": ENTRA},
                    follow_redirects=False)
    check("it says what it read", "identifier" in r.headers["location"], True)
    for name, key in saml.METADATA_KEYS.items():
        check(f"{key} stored", bool(settings.get(key)), True)
    check("the identifier is the one from the metadata",
          settings.get("ITAM_SAML_IDP_ENTITY_ID"), found["entity_id"])

    print("\n--- a bad paste changes nothing ---")
    before = settings.get("ITAM_SAML_IDP_ENTITY_ID")
    r = client.post("/settings/sso/import", data={"metadata": "<html/>"},
                    follow_redirects=False)
    check("it complains", "not+SAML+metadata" in r.headers["location"], True)
    check("and the working config is untouched",
          settings.get("ITAM_SAML_IDP_ENTITY_ID"), before)

    print("\n--- the page leads with the paste, not with five empty boxes ---")
    page = client.get("/settings/sso").text
    check("the metadata box is there", 'action="/settings/sso/import"' in page, True)
    check("what Entra needs is selectable", 'class="copyable mono"' in page, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
