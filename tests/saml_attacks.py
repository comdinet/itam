"""Craft SAML responses like an IdP would, then try to break the SP."""
import base64, copy, datetime, sys, uuid
import httpx, lxml.etree as ET, xmlsec

BASE = "http://127.0.0.1:8000"
ACS = "https://itam.remedio.io/saml/acs"
SP = "https://itam.remedio.io/saml/metadata"
IDP = "https://sts.windows.net/test-tenant-id/"
NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
      "saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
for p, u in NS.items():
    ET.register_namespace(p, u)

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

def t(offset=0):
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")

def build(in_response_to, *, user="sso.user@remedio.io", audience=SP, dest=ACS,
          issuer=IDP, not_before=-60, not_after=300, aid=None, rid=None):
    # Unique ids per assertion, exactly as a real IdP issues them.
    aid = aid or "_a" + uuid.uuid4().hex
    rid = rid or "_r" + uuid.uuid4().hex
    xml = f'''<samlp:Response xmlns:samlp="{NS['samlp']}" xmlns:saml="{NS['saml']}"
   ID="{rid}" Version="2.0" IssueInstant="{t()}" Destination="{dest}"
   {'InResponseTo="%s"' % in_response_to if in_response_to else ''}>
 <saml:Issuer>{issuer}</saml:Issuer>
 <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
 <saml:Assertion ID="{aid}" Version="2.0" IssueInstant="{t()}">
  <saml:Issuer>{issuer}</saml:Issuer>
  <saml:Subject>
   <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{user}</saml:NameID>
   <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
    <saml:SubjectConfirmationData NotOnOrAfter="{t(not_after)}" Recipient="{dest}"
      {'InResponseTo="%s"' % in_response_to if in_response_to else ''}/>
   </saml:SubjectConfirmation>
  </saml:Subject>
  <saml:Conditions NotBefore="{t(not_before)}" NotOnOrAfter="{t(not_after)}">
   <saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AuthnStatement AuthnInstant="{t()}" SessionIndex="_s1">
   <saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport</saml:AuthnContextClassRef></saml:AuthnContext>
  </saml:AuthnStatement>
  <saml:AttributeStatement>
   <saml:Attribute Name="http://schemas.microsoft.com/ws/2008/06/identity/claims/groups">
    <saml:AttributeValue>ITAM-Admins</saml:AttributeValue></saml:Attribute>
  </saml:AttributeStatement>
 </saml:Assertion>
</samlp:Response>'''
    return ET.fromstring(xml.encode())

def sign(doc, key="/tmp/testidp.key", cert="/tmp/testidp.crt", aid=None):
    assertion = doc.find("saml:Assertion", NS)
    if aid is None:
        aid = assertion.get("ID")
    sig = xmlsec.template.create(assertion, xmlsec.Transform.EXCL_C14N,
                                 xmlsec.Transform.RSA_SHA256)
    assertion.insert(1, sig)          # right after Issuer, as Entra does
    ref = xmlsec.template.add_reference(sig, xmlsec.Transform.SHA256, uri="#" + aid)
    xmlsec.template.add_transform(ref, xmlsec.Transform.ENVELOPED)
    xmlsec.template.add_transform(ref, xmlsec.Transform.EXCL_C14N)
    ki = xmlsec.template.ensure_key_info(sig); xmlsec.template.add_x509_data(ki)
    ctx = xmlsec.SignatureContext()
    ctx.key = xmlsec.Key.from_file(key, xmlsec.KeyFormat.PEM)
    ctx.key.load_cert_from_file(cert, xmlsec.KeyFormat.PEM)
    ctx.register_id(assertion, "ID", None)
    ctx.sign(sig)
    return doc

def post(doc, request_id=None, cookie_id=None):
    body = base64.b64encode(ET.tostring(doc)).decode()
    cookies = {}
    if cookie_id: cookies["itam_saml"] = cookie_id
    with httpx.Client(follow_redirects=False, timeout=20) as c:
        r = c.post(f"{BASE}/saml/acs", data={"SAMLResponse": body}, cookies=cookies)
    loc = r.headers.get("location", "")
    signed_in = "itam_session" in r.headers.get("set-cookie", "")
    return signed_in, loc

def start_login():
    """Ask the SP for an AuthnRequest, returning its id."""
    with httpx.Client(follow_redirects=False, timeout=20) as c:
        r = c.get(f"{BASE}/saml/login", params={"next": "/settings"})
    for part in r.headers.get("set-cookie", "").split(";"):
        if part.strip().startswith("itam_saml="):
            return part.strip().split("=", 1)[1]
    return None

xmlsec.init()
print("--- the happy path (account must exist; auto-provision is off) ---")
rid = start_login()
ok, loc = post(sign(build(rid, user="nobody.here@remedio.io")), cookie_id=rid)
check("unknown user refused when auto-provision is off", ok, False)
check("  and says why", "No+ITAM+account" in loc, True)

# create the account, then retry
sys.path.insert(0, "/srv/itam")
from app import auth as _auth
if not _auth.get_user("sso.user@remedio.io"):
    _auth.create_user("sso.user@remedio.io", "Placeholder-Pass-12345")
rid = start_login()
ok, loc = post(sign(build(rid)), cookie_id=rid)
if not ok: print("      reason:", loc)
check("valid signed assertion signs in", ok, True)
check("  lands on the requested page", loc.endswith("/settings"), True)

print("\n--- forgery and tampering ---")
rid = start_login()
ok, _ = post(build(rid), cookie_id=rid)                       # never signed
check("unsigned assertion refused", ok, False)

rid = start_login()
ok, _ = post(sign(build(rid), key="/tmp/attacker.key", cert="/tmp/attacker.crt"),
             cookie_id=rid)
check("assertion signed by another key refused", ok, False)

rid = start_login()
doc = sign(build(rid))
doc.find("saml:Assertion/saml:Subject/saml:NameID", NS).text = "attacker@evil.example"
ok, _ = post(doc, cookie_id=rid)
check("tampered NameID after signing refused", ok, False)

print("\n--- audience, destination, issuer ---")
rid = start_login()
ok, _ = post(sign(build(rid, audience="https://someone-else.example")), cookie_id=rid)
check("wrong audience refused", ok, False)
rid = start_login()
ok, _ = post(sign(build(rid, dest="https://evil.example/saml/acs")), cookie_id=rid)
check("wrong destination refused", ok, False)
rid = start_login()
ok, _ = post(sign(build(rid, issuer="https://sts.windows.net/other-tenant/")), cookie_id=rid)
check("wrong issuer refused", ok, False)

print("\n--- time conditions ---")
rid = start_login()
ok, _ = post(sign(build(rid, not_before=-7200, not_after=-3600)), cookie_id=rid)
check("expired assertion refused", ok, False)
rid = start_login()
ok, _ = post(sign(build(rid, not_before=3600, not_after=7200)), cookie_id=rid)
check("not-yet-valid assertion refused", ok, False)

print("\n--- InResponseTo / unsolicited ---")
ok, loc = post(sign(build(None)))
check("unsolicited assertion refused (no request started here)", ok, False)
check("  says so", "did+not+start+here" in loc, True)
rid = start_login()
ok, _ = post(sign(build("_someone-elses-request-id")), cookie_id=rid)
check("InResponseTo not matching our request refused", ok, False)
rid = start_login()
ok, _ = post(sign(build(rid)), cookie_id="_a-different-cookie")
check("cookie not matching any outstanding request refused", ok, False)

print("\n--- replay ---")
rid = start_login()
doc = sign(build(rid))
body = base64.b64encode(ET.tostring(doc)).decode()
with httpx.Client(follow_redirects=False, timeout=20) as c:
    r1 = c.post(f"{BASE}/saml/acs", data={"SAMLResponse": body}, cookies={"itam_saml": rid})
first = "itam_session" in r1.headers.get("set-cookie", "")
rid2 = start_login()
with httpx.Client(follow_redirects=False, timeout=20) as c:
    r2 = c.post(f"{BASE}/saml/acs", data={"SAMLResponse": body}, cookies={"itam_saml": rid2})
second = "itam_session" in r2.headers.get("set-cookie", "")
check("first use of the assertion works", first, True)
check("replaying the very same assertion refused", second, False)

print("\n--- the AuthnRequest is single use ---")
rid = start_login()
ok, _ = post(sign(build(rid)), cookie_id=rid)
check("first response consumes it", ok, True)
ok, _ = post(sign(build(rid)), cookie_id=rid)
check("the same request id cannot be reused", ok, False)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
