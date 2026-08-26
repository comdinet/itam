"""SAML 2.0 single sign-on against Entra ID.

Service-provider initiated: the browser asks us, we redirect to Entra with a
signed-in-response-to id, and Entra posts the assertion back to /saml/acs.

The heavy lifting - XML signature verification, canonicalisation, condition
checking - is python3-saml with strict mode on. What this module adds around
it is the state a correct flow needs: remembering the AuthnRequest we issued,
refusing an assertion that does not answer one, and refusing an assertion id
we have already consumed.
"""
import datetime
import os
import re

from . import db

REQUEST_MINUTES = 10          # an AuthnRequest is good for one sign-in attempt
SEEN_HOURS = 24               # how long a consumed assertion id blocks a replay


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# --- configuration -------------------------------------------------------

def base_url() -> str:
    """Public origin, used to build the ACS and entity URLs Entra must match."""
    explicit = _env("ITAM_SAML_SP_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    first = _env("ITAM_SITE_ADDRESS").split(",")[0].strip()
    return f"https://{first}" if first else ""


def sp_entity_id() -> str:
    return _env("ITAM_SAML_SP_ENTITY_ID") or f"{base_url()}/saml/metadata"


def acs_url() -> str:
    return f"{base_url()}/saml/acs"


def normalise_cert(raw: str) -> str:
    """Accept the certificate however it was pasted: with or without PEM
    headers, on one line or many."""
    body = re.sub(r"-----(BEGIN|END) CERTIFICATE-----", "", raw or "")
    body = re.sub(r"\s+", "", body)
    return body


def is_configured() -> bool:
    return all([_env("ITAM_SAML_IDP_ENTITY_ID"), _env("ITAM_SAML_IDP_SSO_URL"),
                normalise_cert(_env("ITAM_SAML_IDP_CERT")), base_url()])


def allow_unsolicited() -> bool:
    """IdP-initiated sign-in (the Microsoft My Apps tile). Off by default: an
    unsolicited assertion is the usual way SSO gets abused, so it has to be
    turned on deliberately."""
    return _env("ITAM_SAML_ALLOW_IDP_INITIATED", "0").lower() in ("1", "true", "yes")


def auto_provision() -> bool:
    return _env("ITAM_SAML_AUTO_PROVISION", "0").lower() in ("1", "true", "yes")


def admin_group() -> str:
    return _env("ITAM_SAML_ADMIN_GROUP")


def username_attribute() -> str:
    return _env("ITAM_SAML_ATTR_USERNAME")


def config_status() -> dict:
    return {
        "configured": is_configured(),
        "idp_entity_id": _env("ITAM_SAML_IDP_ENTITY_ID"),
        "idp_sso_url": _env("ITAM_SAML_IDP_SSO_URL"),
        "cert_set": bool(normalise_cert(_env("ITAM_SAML_IDP_CERT"))),
        "sp_entity_id": sp_entity_id(),
        "acs_url": acs_url(),
        "metadata_url": f"{base_url()}/saml/metadata" if base_url() else "",
        "auto_provision": auto_provision(),
        "admin_group": admin_group(),
        "allow_idp_initiated": allow_unsolicited(),
        "username_attribute": username_attribute() or "NameID",
    }


def settings() -> dict:
    """python3-saml settings. Strict, and assertions must be signed."""
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sp_entity_id(),
            "assertionConsumerService": {
                "url": acs_url(),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": _env("ITAM_SAML_IDP_ENTITY_ID"),
            "singleSignOnService": {
                "url": _env("ITAM_SAML_IDP_SSO_URL"),
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": normalise_cert(_env("ITAM_SAML_IDP_CERT")),
        },
        "security": {
            # Entra signs the assertion; that is what must be verified.
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameId": True,
            "wantAssertionsEncrypted": False,
            "requestedAuthnContext": False,
            "rejectUnsolicitedResponsesWithInResponseTo": not allow_unsolicited(),
            "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
        },
    }


# --- request / replay state ---------------------------------------------

def remember_request(request_id: str, next_url: str) -> None:
    db.execute(
        """INSERT OR REPLACE INTO saml_requests (request_id, next_url, created_at, expires_at)
           VALUES (?,?,?,?)""",
        (request_id, next_url, _iso(_now()),
         _iso(_now() + datetime.timedelta(minutes=REQUEST_MINUTES))))


def take_request(request_id: str | None):
    """Consume an outstanding AuthnRequest. One use only."""
    if not request_id:
        return None
    row = db.q1("SELECT * FROM saml_requests WHERE request_id = ? AND expires_at > ?",
                (request_id, _iso(_now())))
    db.execute("DELETE FROM saml_requests WHERE request_id = ?", (request_id,))
    return row


def already_seen(assertion_id: str) -> bool:
    return bool(db.q1("SELECT 1 FROM saml_seen WHERE assertion_id = ?", (assertion_id,)))


def mark_seen(assertion_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO saml_seen (assertion_id, seen_at, expires_at) VALUES (?,?,?)",
        (assertion_id, _iso(_now()), _iso(_now() + datetime.timedelta(hours=SEEN_HOURS))))


def purge() -> None:
    now = _iso(_now())
    db.execute("DELETE FROM saml_requests WHERE expires_at <= ?", (now,))
    db.execute("DELETE FROM saml_seen WHERE expires_at <= ?", (now,))


# --- mapping the assertion to an account --------------------------------

def pick_username(nameid: str | None, attributes: dict) -> str:
    """Which claim identifies the person. NameID unless told otherwise."""
    attr = username_attribute()
    if attr:
        values = attributes.get(attr) or []
        if values:
            return str(values[0]).strip().lower()
        return ""
    return (nameid or "").strip().lower()


def is_admin_by_group(attributes: dict) -> bool:
    group = admin_group()
    if not group:
        return False
    for values in attributes.values():
        for v in values or []:
            if str(v).strip() == group:
                return True
    return False
