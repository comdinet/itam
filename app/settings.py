"""Settings, layered: a row in the database beats the environment.

Anything editable in the UI lives here. Reading always goes database ->
environment -> built-in default, so an existing .env keeps working untouched
and a UI change takes effect without a restart.

Some settings genuinely cannot work this way and are declared ENV_ONLY: the
database path (needed to open the database), the published ports and hostname
(used by Compose and the certificate before the app runs), and the bootstrap
admin credentials (consumed once at first start).
"""
import datetime

from . import db

# key -> (kind, default, label, group, secret)
#   kind: str | int | bool | text
#   group: which Settings page it belongs to
SPEC: dict[str, tuple] = {
    # --- general ---
    "ITAM_CURRENCY":        ("str",  "USD",  "Currency label", "general", False),
    "ITAM_SESSION_HOURS":   ("int",  "12",   "Sign-in lasts (hours)", "general", False),
    "ITAM_REQUIRE_2FA":     ("bool", "0",    "Require two-factor for every local account",
                             "general", False),
    "ITAM_TOTP_ISSUER":     ("str",  "ITAM", "Name shown in authenticator apps",
                             "general", False),
    # --- Entra ID ---
    "ENTRA_TENANT_ID":      ("str",  "", "Tenant ID", "entra", False),
    "ENTRA_CLIENT_ID":      ("str",  "", "Client ID", "entra", False),
    "ENTRA_CLIENT_SECRET":  ("str",  "", "Client secret", "entra", True),
    "ENTRA_USER_FILTER":    ("str",  "", "User filter (OData)", "entra", False),
    "ENTRA_GROUP_FILTER":   ("str",  "", "Group filter (OData)", "entra", False),
    "ENTRA_DEVICE_GROUP_FILTER": ("str", "", "Device group filter (OData) - narrows "
                                 "which groups are scanned for device members",
                                 "entra", False),
    "INTUNE_DEVICE_FILTER": ("str",  "", "Device filter (OData)", "entra", False),
    "INTUNE_ATTRIBUTE_FILTER": ("str", "", "macOS custom attributes to sync "
                                "(comma separated, blank = all, * allowed)",
                                "entra", False),
    # --- SAML ---
    "ITAM_SAML_SP_BASE_URL":        ("str",  "", "This app's public base URL", "saml", False),
    "ITAM_SAML_SP_ENTITY_ID":       ("str",  "", "SP entity ID (blank = derived)", "saml", False),
    "ITAM_SAML_IDP_ENTITY_ID":      ("str",  "", "Microsoft Entra Identifier", "saml", False),
    "ITAM_SAML_IDP_SSO_URL":        ("str",  "", "Login URL", "saml", False),
    "ITAM_SAML_IDP_CERT":           ("text", "", "Base64 certificate", "saml", False),
    "ITAM_SAML_AUTO_PROVISION":     ("bool", "0", "Create accounts on first sign-in",
                                     "saml", False),
    "ITAM_SAML_ADMIN_GROUP":        ("str",  "", "Group claim granting admin", "saml", False),
    "ITAM_SAML_ATTR_USERNAME":      ("str",  "", "Username claim (blank = NameID)",
                                     "saml", False),
    "ITAM_SAML_ALLOW_IDP_INITIATED": ("bool", "0", "Allow sign-in started at Microsoft",
                                      "saml", False),
}

# Read from the environment only; a database row would have no effect.
ENV_ONLY = {
    "ITAM_DB": "Database file. Opening it comes before reading any setting.",
    "ITAM_SITE_ADDRESS": "Certificate names, and what Caddy serves.",
    "ITAM_HTTP_PORT": "Published by Compose, outside the app.",
    "ITAM_HTTPS_PORT": "Published by Compose, outside the app.",
    "ITAM_PORT": "Loopback port, published by Compose.",
    "ITAM_COOKIE_SECURE": "Transport-level. Getting it wrong locks everyone out, "
                          "so it stays in .env.",
    "ITAM_ADMIN_USER": "Used once, to create the first account.",
    "ITAM_ADMIN_PASSWORD": "Used once, to create the first account.",
}


def _env(key: str) -> str | None:
    import os
    raw = os.environ.get(key)
    return raw if raw is not None and raw.strip() != "" else None


def stored(key: str) -> str | None:
    """The database override, if any."""
    row = db.q1("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


def raw(key: str) -> str:
    """Effective raw string: database, then environment, then default."""
    val = stored(key)
    if val is not None:
        return val
    env = _env(key)
    if env is not None:
        return env.strip()
    kind_default = SPEC.get(key)
    return kind_default[1] if kind_default else ""


def source(key: str) -> str:
    if stored(key) is not None:
        return "set here"
    if _env(key) is not None:
        return ".env"
    return "default"


def get(key: str) -> str:
    return raw(key)


def get_bool(key: str) -> bool:
    return raw(key).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, fallback: int) -> int:
    try:
        return int(raw(key))
    except (TypeError, ValueError):
        return fallback


def set_value(key: str, value: str, by: str) -> None:
    if key not in SPEC:
        raise KeyError(key)
    db.execute(
        """INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
        (key, value, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
         by))


def clear(key: str, by: str) -> None:
    """Drop the override so .env (or the default) applies again."""
    db.execute("DELETE FROM settings WHERE key = ?", (key,))


def group(name: str) -> list[dict]:
    out = []
    for key, (kind, default, label, grp, secret) in SPEC.items():
        if grp != name:
            continue
        value = raw(key)
        out.append({"key": key, "kind": kind, "label": label, "secret": secret,
                    "value": "" if secret else value,
                    "is_set": bool(value), "source": source(key),
                    "default": default})
    return out


def audit() -> list:
    return db.q("SELECT * FROM settings ORDER BY key")


# --- typed accessors used across the app ---------------------------------

def currency() -> str:
    return get("ITAM_CURRENCY") or "USD"


def session_hours() -> int:
    return max(1, get_int("ITAM_SESSION_HOURS", 12))


def require_2fa() -> bool:
    return get_bool("ITAM_REQUIRE_2FA")


def totp_issuer() -> str:
    return get("ITAM_TOTP_ISSUER") or "ITAM"


def cookie_secure() -> bool:
    """Environment only - see ENV_ONLY."""
    import os
    return (os.environ.get("ITAM_COOKIE_SECURE") or "1").lower() in ("1", "true", "yes")
