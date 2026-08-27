"""Microsoft Graph user sync (app-only / client credentials).

Configure via environment variables:
    ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET
Required Graph application permission: User.Read.All (admin consented).

UPN (userPrincipalName) is the unique key used throughout the app.
"""
import datetime
import fnmatch
import os

import httpx

from . import db, settings

GRAPH = "https://graph.microsoft.com/v1.0"
# Intune custom attribute shell scripts are only exposed on the beta endpoint.
GRAPH_BETA = "https://graph.microsoft.com/beta"
SELECT = ("id,userPrincipalName,displayName,jobTitle,department,accountEnabled,"
          "country,usageLocation")

# Friendly names keyed on skuPartNumber, the stable string id. Published GUID
# lists disagree with one another, so nothing here is keyed on a GUID, and the
# tenant's own /subscribedSkus is the source of truth for what exists.
SKU_NAMES = {
    "SPB": "Microsoft 365 Business Premium",
    "SPB_NOTEAMS": "Microsoft 365 Business Premium (no Teams)",
    "O365_BUSINESS_ESSENTIALS": "Microsoft 365 Business Basic",
    "O365_BUSINESS_PREMIUM": "Microsoft 365 Business Standard",
    "SPE_E3": "Microsoft 365 E3",
    "SPE_E5": "Microsoft 365 E5",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "EXCHANGESTANDARD": "Exchange Online (Plan 1)",
    "EXCHANGEENTERPRISE": "Exchange Online (Plan 2)",
    "AAD_PREMIUM": "Microsoft Entra ID P1",
    "AAD_PREMIUM_P2": "Microsoft Entra ID P2",
    "POWER_BI_PRO": "Power BI Pro",
    "PROJECTPROFESSIONAL": "Project Plan 3",
    "VISIOCLIENT": "Visio Plan 2",
    "FLOW_FREE": "Power Automate Free",
    "TEAMS_EXPLORATORY": "Microsoft Teams Exploratory",
}


def sku_display_name(part_number: str | None) -> str:
    if not part_number:
        return "(unknown SKU)"
    return SKU_NAMES.get(part_number, part_number)

DEVICE_SELECT = ("id,deviceName,serialNumber,manufacturer,model,operatingSystem,"
                 "osVersion,userPrincipalName,complianceState,enrolledDateTime,"
                 "lastSyncDateTime,totalStorageSpaceInBytes,freeStorageSpaceInBytes")


def is_configured() -> bool:
    return all(settings.get(k) for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID",
                                        "ENTRA_CLIENT_SECRET"))


def config_status() -> dict:
    return {
        "configured": is_configured(),
        "tenant_id": settings.get("ENTRA_TENANT_ID"),
        "client_id": settings.get("ENTRA_CLIENT_ID"),
        "secret_set": bool(settings.get("ENTRA_CLIENT_SECRET")),
        "filter": settings.get("ENTRA_USER_FILTER"),
        "group_filter": settings.get("ENTRA_GROUP_FILTER"),
        "device_filter": settings.get("INTUNE_DEVICE_FILTER"),
    }


# Which Graph application permission each endpoint needs, so a 403 can say
# what is actually missing instead of just the status code.
PERMISSION_FOR = [
    # Microsoft moved this endpoint to DeviceManagementScripts.* in July 2025;
    # it used to be DeviceManagementConfiguration.*.
    ("/deviceManagement/deviceCustomAttributeShellScripts",
     "DeviceManagementScripts.Read.All"),
    ("/deviceManagement/managedDevices", "DeviceManagementManagedDevices.Read.All"),
    ("/subscribedSkus", "Organization.Read.All"),
    ("/groups", "Group.Read.All"),
    ("/users", "User.Read.All"),
]


class GraphError(Exception):
    """A Graph failure with the reason Graph actually gave."""


def _permission_for(path: str) -> str:
    for prefix, permission in PERMISSION_FOR:
        if path.startswith(prefix):
            return permission
    return "the relevant Graph"


def _explain(resp, path: str) -> str:
    """Turn a Graph error response into something worth reading."""
    code = message = ""
    try:
        err = (resp.json() or {}).get("error") or {}
        code = str(err.get("code") or "")
        message = str(err.get("message") or "")
    except Exception:
        message = (resp.text or "")[:200]

    permission = _permission_for(path)
    if resp.status_code == 403:
        return (f"403 Forbidden from Graph ({code or 'no code'}): {message} "
                f"-- the app registration is missing the '{permission}' "
                f"APPLICATION permission, or admin consent has not been granted "
                f"for it. Add it under API permissions, then click "
                f"'Grant admin consent'.")
    if resp.status_code == 401:
        return (f"401 Unauthorized ({code}): {message} -- the tenant id, client id "
                f"or client secret is wrong, or the secret has expired.")
    if resp.status_code == 400:
        hint = ""
        if "filter" in message.lower() or "filter" in code.lower():
            hint = " -- check the OData filters on this page."
        elif "page size" in message.lower():
            hint = (" -- this endpoint rejects a page-size argument; that is a "
                    "bug in the caller, not your configuration.")
        return f"400 Bad request ({code}): {message}{hint}"
    if resp.status_code == 429:
        return (f"429 Throttled by Graph: {message} -- too many requests; "
                f"try again shortly.")
    return f"{resp.status_code} from Graph ({code}): {message}"


def _token() -> str:
    tenant = settings.get("ENTRA_TENANT_ID")
    resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": settings.get("ENTRA_CLIENT_ID"),
            "client_secret": settings.get("ENTRA_CLIENT_SECRET"),
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json() or {}
            detail = f"{body.get('error', '')}: {body.get('error_description', '')}"
        except Exception:
            detail = (resp.text or "")[:200]
        raise GraphError(
            f"Could not get a token from Entra ({resp.status_code}). {detail.strip()} "
            f"-- check the tenant id, client id and client secret.")
    return resp.json()["access_token"]


def _get_all(path: str, params: dict | None = None, base: str = GRAPH,
             advanced: bool = False) -> list[dict]:
    """GET a Graph collection, following @odata.nextLink to the end.

    `advanced` opts into Graph's advanced query capabilities. Several directory
    filters need it - userType, ne, not, startsWith and endsWith among them -
    and it costs nothing for the simple ones, so any configured filter uses it.
    """
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    if advanced:
        headers["ConsistencyLevel"] = "eventual"
        params = dict(params or {})
        params["$count"] = "true"
    url = f"{base}{path}"
    out: list[dict] = []
    with httpx.Client(timeout=60) as client:
        while url:
            resp = client.get(url, headers=headers, params=params)
            if resp.status_code >= 400:
                raise GraphError(_explain(resp, path))
            body = resp.json()
            out.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
            params = None          # nextLink already carries the query string
    return out


def fetch_users() -> list[dict]:
    """Page through all users in the tenant."""
    params = {"$select": SELECT, "$top": "999"}
    user_filter = settings.get("ENTRA_USER_FILTER")
    if user_filter:
        params["$filter"] = user_filter
    return _get_all("/users", params, advanced=bool(user_filter))


def sync() -> dict:
    """Upsert Entra users into the local table. Never deletes; disabled
    accounts are marked so their assignments stay auditable."""
    users = fetch_users()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    created = updated = skipped = 0
    with db.cursor() as conn:
        for u in users:
            upn = (u.get("userPrincipalName") or "").strip().lower()
            if not upn:
                skipped += 1
                continue
            exists = conn.execute("SELECT 1 FROM users WHERE upn = ?", (upn,)).fetchone()
            conn.execute(
                """INSERT INTO users (upn, display_name, job_title, department, entra_id,
                                      account_enabled, country, usage_location,
                                      source, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,'entra',?)
                   ON CONFLICT(upn) DO UPDATE SET
                       display_name=excluded.display_name,
                       job_title=excluded.job_title,
                       department=excluded.department,
                       entra_id=excluded.entra_id,
                       account_enabled=excluded.account_enabled,
                       country=excluded.country,
                       usage_location=excluded.usage_location,
                       source='entra',
                       synced_at=excluded.synced_at""",
                (
                    upn,
                    u.get("displayName") or upn,
                    u.get("jobTitle"),
                    u.get("department"),
                    u.get("id"),
                    1 if u.get("accountEnabled", True) else 0,
                    u.get("country"),
                    u.get("usageLocation"),
                    now,
                ),
            )
            if exists:
                updated += 1
            else:
                created += 1
    return {"fetched": len(users), "created": created, "updated": updated, "skipped": skipped}


# --- groups --------------------------------------------------------------

def sync_groups() -> dict:
    """Pull groups and their user membership.

    Needs the Graph application permission Group.Read.All (plus the existing
    User.Read.All) with admin consent.
    """
    group_filter = settings.get("ENTRA_GROUP_FILTER")
    params = {"$select": "id,displayName,description", "$top": "999"}
    if group_filter:
        params["$filter"] = group_filter
    groups = _get_all("/groups", params, advanced=bool(group_filter))

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    created = updated = members_linked = skipped_members = 0

    for g in groups:
        gid = g.get("id")
        if not gid:
            continue
        # transitiveMembers, cast to user: includes people in nested groups and
        # excludes non-user members outright. Plain /members would return only
        # direct members, so anyone in a nested group would silently vanish.
        members = _get_all(
            f"/groups/{gid}/transitiveMembers/microsoft.graph.user",
            {"$select": "id,userPrincipalName", "$top": "999"})
        upns = [(m.get("userPrincipalName") or "").strip().lower()
                for m in members if m.get("userPrincipalName")]

        with db.cursor() as conn:
            exists = conn.execute("SELECT 1 FROM groups WHERE id = ?", (gid,)).fetchone()
            conn.execute(
                """INSERT INTO groups (id, display_name, description, member_count, synced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       display_name = excluded.display_name,
                       description  = excluded.description,
                       member_count = excluded.member_count,
                       synced_at    = excluded.synced_at""",
                (gid, g.get("displayName") or gid, g.get("description"), len(upns), now))
            conn.execute("DELETE FROM group_members WHERE group_id = ?", (gid,))
            conn.execute("DELETE FROM group_members_unlinked WHERE group_id = ?", (gid,))
            for upn in upns:
                known = conn.execute("SELECT 1 FROM users WHERE upn = ?", (upn,)).fetchone()
                if known:
                    conn.execute(
                        "INSERT OR IGNORE INTO group_members (group_id, upn) VALUES (?,?)",
                        (gid, upn))
                else:
                    # Recorded by name, so the group page can say who is missing
                    # rather than only how many.
                    conn.execute(
                        """INSERT OR IGNORE INTO group_members_unlinked (group_id, upn)
                           VALUES (?,?)""", (gid, upn))
            if exists:
                updated += 1
            else:
                created += 1

        linked = db.q1("SELECT COUNT(*) c FROM group_members WHERE group_id = ?", (gid,))["c"]
        members_linked += linked
        skipped_members += len(upns) - linked

    return {"groups": len(groups), "created": created, "updated": updated,
            "members_linked": members_linked, "members_unknown": skipped_members}


def unlinked_reason() -> str:
    """Why a group member might not be an ITAM user."""
    user_filter = settings.get("ENTRA_USER_FILTER")
    if user_filter:
        return (f"These people are in the group in Entra but are not users here. "
                f"The user sync is filtered by: {user_filter} - anyone it excludes "
                f"cannot be linked. Run the user sync, and check that filter.")
    return ("These people are in the group in Entra but are not users here. "
            "Run the user sync under Entra ID; if they still do not appear, they "
            "are probably not user accounts.")


# --- Intune devices ------------------------------------------------------

def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_devices() -> dict:
    """Pull managed devices from Intune and link them to assets by serial.

    Needs the Graph application permission
    DeviceManagementManagedDevices.Read.All with admin consent.
    """
    params = {"$select": DEVICE_SELECT, "$top": "999"}
    device_filter = settings.get("INTUNE_DEVICE_FILTER")
    if device_filter:
        params["$filter"] = device_filter
    # Intune's managedDevices does not support advanced query, so no opt-in here.
    devices = _get_all("/deviceManagement/managedDevices", params)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    created = updated = linked = 0

    with db.cursor() as conn:
        for d in devices:
            did = d.get("id")
            if not did:
                continue
            serial = (d.get("serialNumber") or "").strip() or None
            upn = (d.get("userPrincipalName") or "").strip().lower() or None

            # An asset with the same serial is the same physical thing.
            asset_id = None
            if serial:
                row = conn.execute(
                    "SELECT id FROM assets WHERE serial = ? COLLATE NOCASE", (serial,)).fetchone()
                if row:
                    asset_id = row["id"]

            exists = conn.execute("SELECT 1 FROM devices WHERE id = ?", (did,)).fetchone()
            conn.execute(
                """INSERT INTO devices (id, device_name, serial_number, manufacturer, model,
                                        os, os_version, primary_upn, compliance_state,
                                        enrolled_at, last_contact, storage_total,
                                        storage_free, synced_at, asset_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       device_name=excluded.device_name, serial_number=excluded.serial_number,
                       manufacturer=excluded.manufacturer, model=excluded.model,
                       os=excluded.os, os_version=excluded.os_version,
                       primary_upn=excluded.primary_upn,
                       compliance_state=excluded.compliance_state,
                       enrolled_at=excluded.enrolled_at, last_contact=excluded.last_contact,
                       storage_total=excluded.storage_total, storage_free=excluded.storage_free,
                       synced_at=excluded.synced_at,
                       -- keep a link made by hand if the serial match finds nothing
                       asset_id=COALESCE(excluded.asset_id, devices.asset_id)""",
                (did, d.get("deviceName"), serial, d.get("manufacturer"), d.get("model"),
                 d.get("operatingSystem"), d.get("osVersion"), upn, d.get("complianceState"),
                 d.get("enrolledDateTime"), d.get("lastSyncDateTime"),
                 _int_or_none(d.get("totalStorageSpaceInBytes")),
                 _int_or_none(d.get("freeStorageSpaceInBytes")), now, asset_id))
            if exists:
                updated += 1
            else:
                created += 1
            if asset_id:
                linked += 1

    return {"devices": len(devices), "created": created, "updated": updated,
            "linked_to_assets": linked}


# --- macOS custom attributes --------------------------------------------

def attribute_patterns() -> list[str]:
    """Which custom attributes to sync. Empty list means all of them."""
    raw = settings.get("INTUNE_ATTRIBUTE_FILTER")
    return [p.strip() for p in raw.split(",") if p.strip()]


def attribute_wanted(name: str, patterns: list[str]) -> bool:
    """Case-insensitive match, with * wildcards, against the attribute name.

    Filtering happens on the script list, before the per-script device-state
    calls - which is the expensive part, one paged request each.
    """
    if not patterns:
        return True
    candidate = (name or "").strip().lower()
    for pattern in patterns:
        p = pattern.lower()
        if fnmatch.fnmatch(candidate, p if "*" in p else p):
            return True
    return False


def sync_custom_attributes() -> dict:
    """Merge Intune custom attribute results onto their devices.

    macOS custom attributes are shell scripts whose stdout Intune stores as the
    result. Each script has per-device run states carrying that output, which is
    what gets stored here - so an attribute reporting CPU and RAM shows up on
    the device.

    Beta endpoint (no v1.0 equivalent). Needs DeviceManagementScripts.Read.All
    with admin consent - Microsoft moved this off
    DeviceManagementConfiguration.* in July 2025.
    """
    scripts = _get_all("/deviceManagement/deviceCustomAttributeShellScripts",
                       {"$select": "id,displayName,customAttributeName"},
                       base=GRAPH_BETA)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    patterns = attribute_patterns()
    stored = skipped = 0
    wanted_names: list[str] = []
    skipped_names: list[str] = []

    for script in scripts:
        sid = script.get("id")
        # customAttributeName is the attribute Intune actually reports under;
        # displayName is just the script's name in the console.
        label = (script.get("customAttributeName")
                 or script.get("displayName") or sid)
        if not sid:
            continue
        if not attribute_wanted(label, patterns):
            skipped_names.append(label)
            continue
        wanted_names.append(label)
        states = _get_all(
            f"/deviceManagement/deviceCustomAttributeShellScripts/{sid}/deviceRunStates",
            {"$expand": "managedDevice($select=id,deviceName)"}, base=GRAPH_BETA)
        with db.cursor() as conn:
            for st in states:
                managed = st.get("managedDevice") or {}
                did = managed.get("id") or st.get("managedDeviceId")
                value = (st.get("resultMessage") or "").strip()
                if not did or not value:
                    skipped += 1
                    continue
                # Only for devices we know about, so attributes cannot outlive
                # their device row.
                if not conn.execute("SELECT 1 FROM devices WHERE id = ?", (did,)).fetchone():
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO device_attributes (device_id, name, value, collected_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(device_id, name) DO UPDATE SET
                           value = excluded.value, collected_at = excluded.collected_at""",
                    (did, label, value, st.get("lastStateUpdateDateTime") or now))
                stored += 1

    # Drop values for attributes the filter no longer covers, or they would
    # linger forever with no way to clear them from the UI.
    removed = 0
    if patterns:
        for row in db.q("SELECT DISTINCT name FROM device_attributes"):
            if not attribute_wanted(row["name"], patterns):
                db.execute("DELETE FROM device_attributes WHERE name = ?", (row["name"],))
                removed += 1

    return {"scripts_found": len(scripts), "scripts_synced": len(wanted_names),
            "attributes_stored": stored, "skipped_values": skipped,
            "filtered_out": len(skipped_names),
            "stale_attributes_removed": removed,
            "available": sorted(set(wanted_names + skipped_names))}


# --- licences ------------------------------------------------------------

def sync_licenses() -> dict:
    """Pull the tenant's licence SKUs and who holds them.

    Needs Organization.Read.All (or Directory.Read.All) for /subscribedSkus,
    alongside the existing User.Read.All for the per-user assignments.

    The tenant is the source of truth for which SKUs exist: nothing is matched
    against a hardcoded GUID, only against what Entra reports.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    skus = _get_all("/subscribedSkus",
                    {"$select": "skuId,skuPartNumber,prepaidUnits,consumedUnits"})
    with db.cursor() as conn:
        for sku in skus:
            sku_id = sku.get("skuId")
            if not sku_id:
                continue
            part = sku.get("skuPartNumber")
            prepaid = (sku.get("prepaidUnits") or {}).get("enabled") or 0
            conn.execute(
                """INSERT INTO licenses (sku_id, sku_part_number, display_name,
                                         prepaid, consumed, synced_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(sku_id) DO UPDATE SET
                       sku_part_number=excluded.sku_part_number,
                       display_name=excluded.display_name,
                       prepaid=excluded.prepaid,
                       consumed=excluded.consumed,
                       synced_at=excluded.synced_at""",
                (sku_id, part, sku_display_name(part), _int_or_none(prepaid) or 0,
                 _int_or_none(sku.get("consumedUnits")) or 0, now))

    # Per-user assignments. The same filter as the user sync, so the two views
    # cannot disagree about who is in scope.
    params = {"$select": "userPrincipalName,assignedLicenses", "$top": "999"}
    user_filter = settings.get("ENTRA_USER_FILTER")
    if user_filter:
        params["$filter"] = user_filter
    people = _get_all("/users", params, advanced=bool(user_filter))

    assigned = unknown_user = unknown_sku = 0
    known_skus = {r["sku_id"] for r in db.q("SELECT sku_id FROM licenses")}
    with db.cursor() as conn:
        conn.execute("DELETE FROM user_licenses")
        for person in people:
            upn = (person.get("userPrincipalName") or "").strip().lower()
            if not upn:
                continue
            if not conn.execute("SELECT 1 FROM users WHERE upn = ?", (upn,)).fetchone():
                # Licensed in Entra but not synced here - usually a filter
                # difference. Counted so it is visible rather than silent.
                if person.get("assignedLicenses"):
                    unknown_user += 1
                continue
            for lic in person.get("assignedLicenses") or []:
                sku_id = lic.get("skuId")
                if not sku_id:
                    continue
                if sku_id not in known_skus:
                    unknown_sku += 1
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO user_licenses (upn, sku_id) VALUES (?,?)",
                    (upn, sku_id))
                assigned += 1

    return {"skus": len(skus), "assignments": assigned,
            "licensed_not_synced": unknown_user, "unknown_skus": unknown_sku}


# --- connection test -----------------------------------------------------

# Each probe carries its own query, because the endpoints do not accept the
# same arguments. /subscribedSkus and the shell-script list reject $top
# outright ("This resource does not support custom page sizes"), and they are
# small collections anyway, so they are fetched whole.
PROBES = [
    {"label": "Users", "path": "/users", "permission": "User.Read.All",
     "params": {"$select": "id", "$top": "1"}, "base": GRAPH},
    {"label": "Groups", "path": "/groups", "permission": "Group.Read.All",
     "params": {"$select": "id", "$top": "1"}, "base": GRAPH},
    {"label": "Licences", "path": "/subscribedSkus",
     "permission": "Organization.Read.All", "params": {}, "base": GRAPH},
    {"label": "Intune devices", "path": "/deviceManagement/managedDevices",
     "permission": "DeviceManagementManagedDevices.Read.All",
     "params": {"$select": "id", "$top": "1"}, "base": GRAPH},
    {"label": "macOS custom attributes",
     "path": "/deviceManagement/deviceCustomAttributeShellScripts",
     "permission": "DeviceManagementScripts.Read.All",
     "params": {}, "base": GRAPH_BETA},
]


def test_connection() -> dict:
    """Check credentials, then each permission separately.

    Deliberately one probe per feature: a tenant may legitimately have
    User.Read.All and nothing else, and that should read as "users work,
    devices need a permission" rather than one blanket failure.
    """
    result = {"token": None, "token_error": None, "probes": []}
    try:
        _token()
        result["token"] = True
    except Exception as exc:
        result["token"] = False
        result["token_error"] = str(exc)
        return result

    for probe in PROBES:
        try:
            _get_all_once(probe["path"], dict(probe["params"]), base=probe["base"])
            detail, ok = "reachable", True
        except GraphError as exc:
            detail, ok = str(exc), False
        except Exception as exc:
            detail, ok = f"{type(exc).__name__}: {exc}", False
        result["probes"].append({"label": probe["label"], "ok": ok,
                                 "permission": probe["permission"], "detail": detail})
    return result


def _get_all_once(path: str, params: dict, base: str = GRAPH) -> list[dict]:
    """One page only - enough to prove a permission works, without walking a
    whole tenant just to run a test."""
    token = _token()
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{base}{path}", headers={"Authorization": f"Bearer {token}"},
                          params=params)
    if resp.status_code >= 400:
        raise GraphError(_explain(resp, path))
    return (resp.json() or {}).get("value", [])
