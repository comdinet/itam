"""Microsoft Graph user sync (app-only / client credentials).

Configure via environment variables:
    ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET
Required Graph application permission: User.Read.All (admin consented).

UPN (userPrincipalName) is the unique key used throughout the app.
"""
import datetime
import fnmatch
import os
import re

import httpx

from . import db, settings
from . import devices as devices_mod

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
                 "lastSyncDateTime,totalStorageSpaceInBytes,freeStorageSpaceInBytes,"
                 "azureADDeviceId")


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
    ("/devices", "Device.Read.All"),
    ("/subscribedSkus", "Organization.Read.All"),
    ("/groups", "Group.Read.All"),
    ("/users", "User.Read.All"),
]

# Paths whose permission is genuinely not known. Naming one here would be
# worse than naming none: a 403 on deviceInventories reported
# "DeviceManagementManagedDevices.Read.All is missing" at somebody who already
# had it and whose device sync was working, because the prefix match ran on
# past the resource and onto a sub-resource of it.
PERMISSION_UNKNOWN = ("deviceInventories",)


class GraphError(Exception):
    """A Graph failure with the reason Graph actually gave."""


def _permission_for(path: str) -> str:
    """The permission a path needs, or a hedge when it is not known.

    Matched at a path boundary. Plain startswith let
    /deviceManagement/managedDevices('id')/deviceInventories inherit the
    permission of managedDevices, which is a different resource with a
    different answer.
    """
    if any(part in path for part in PERMISSION_UNKNOWN):
        return ""
    for prefix, permission in PERMISSION_FOR:
        if path == prefix or path.startswith(prefix + "/") \
                or path.startswith(prefix + "("):
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
        if not permission:
            # Deliberately names nothing. This endpoint's permission is not
            # documented, and inventing one sends people to add a permission
            # they already have.
            return (f"403 Forbidden from Graph ({code or 'no code'}): {message} "
                    f"-- Graph refused this endpoint. Which permission it wants "
                    f"is not documented, and it may not accept an application "
                    f"(client-credentials) token at all.")
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
            # Graph says "syntax error at position N" and stops there. Nearly
            # every time, the filter was copied from an Intune dynamic group
            # rule, which is a different language that looks similar.
            for text in (settings.get("ENTRA_USER_FILTER"),
                         settings.get("ENTRA_GROUP_FILTER"),
                         settings.get("INTUNE_DEVICE_FILTER")):
                verdict = check_filter(text or "")
                if verdict["dialect"]:
                    hint = (f" -- that filter is {verdict['dialect']}, not OData: "
                            f"{verdict['why'][0]}."
                            + (f" In OData it is: {verdict['suggestion']}"
                               if verdict["suggestion"] else ""))
                    break
        elif "page size" in message.lower():
            hint = (" -- this endpoint rejects a page-size argument; that is a "
                    "bug in the caller, not your configuration.")
        return f"400 Bad request ({code}): {message}{hint}"
    if resp.status_code == 429:
        return (f"429 Throttled by Graph: {message} -- too many requests; "
                f"try again shortly.")
    return f"{resp.status_code} from Graph ({code}): {message}"


# Intune dynamic-group membership rules and OData $filter look alike and are
# not the same language. Pasting the first into the second is the single most
# common way these filters fail, so it is worth naming rather than leaving
# somebody to read "syntax error at position 20".
_DYNAMIC_OPS = re.compile(
    r"-(eq|ne|co|notContains|contains|startsWith|notStartsWith|match|in|notIn|any|all)\b",
    re.IGNORECASE)
_DYNAMIC_PROP = re.compile(r"\b(device|user)\.(\w+)")
_DOUBLE_QUOTED = re.compile(r'"[^"]*"')

# Dynamic-group property -> the managedDevice property OData knows it by.
DEVICE_PROPERTY_MAP = {
    "devicemodel": "model",
    "devicemanufacturer": "manufacturer",
    "deviceostype": "operatingSystem",
    "deviceosversion": "osVersion",
    "displayname": "deviceName",
    "deviceid": "azureADDeviceId",
    "deviceownership": "managedDeviceOwnerType",
    "devicecategory": "deviceCategoryDisplayName",
}


def check_filter(text: str) -> dict:
    """Is this OData, or an Intune dynamic-group rule wearing its clothes?

    Pure - no network. Returns the dialect it looks like, why, and the OData
    form where the translation is unambiguous.
    """
    text = (text or "").strip()
    out = {"dialect": None, "why": [], "suggestion": None}
    if not text:
        return out
    if _DYNAMIC_OPS.search(text):
        out["why"].append("operators are written -eq / -ne, where OData uses eq / ne")
    if _DYNAMIC_PROP.search(text):
        out["why"].append("properties are prefixed device. or user., which OData has no notion of")
    if _DOUBLE_QUOTED.search(text):
        out["why"].append("values are in double quotes, where OData wants single quotes")
    if not out["why"]:
        return out
    out["dialect"] = "an Intune dynamic-group membership rule"

    # Translate the shape people actually paste: one property, one operator,
    # one quoted value, optionally wrapped in brackets.
    simple = re.fullmatch(
        r'\(?\s*(?:device|user)\.(\w+)\s+-(eq|ne|startsWith|contains)\s+"([^"]*)"\s*\)?',
        text, re.IGNORECASE)
    if simple:
        prop, op, value = simple.group(1), simple.group(2), simple.group(3)
        mapped = DEVICE_PROPERTY_MAP.get(prop.lower(), prop[0].lower() + prop[1:])
        value = value.replace("'", "''")
        if op.lower() in ("eq", "ne"):
            out["suggestion"] = f"{mapped} {op.lower()} '{value}'"
        elif op.lower() == "startswith":
            out["suggestion"] = f"startsWith({mapped}, '{value}')"
        else:
            out["suggestion"] = f"contains({mapped}, '{value}')"
    return out


FILTER_TARGETS = {
    "user":   ("ENTRA_USER_FILTER", "/users", {"$select": "id", "$top": "1"}, True),
    "group":  ("ENTRA_GROUP_FILTER", "/groups", {"$select": "id", "$top": "1"}, True),
    "device": ("INTUNE_DEVICE_FILTER", "/deviceManagement/managedDevices",
               {"$select": "id", "$top": "1"}, False),
}


def try_filter(kind: str) -> dict:
    """Send the configured filter to Graph and report what it says.

    The only way to know whether an endpoint accepts a filter is to ask it.
    managedDevices in particular supports $filter on far fewer properties than
    /users does, and the documented list has moved; guessing on somebody's
    behalf is how they end up debugging a sync at 6pm.
    """
    if kind not in FILTER_TARGETS:
        return {"ok": False, "detail": "Unknown filter"}
    key, path, params, advanced = FILTER_TARGETS[kind]
    text = (settings.get(key) or "").strip()
    verdict = check_filter(text)
    if not text:
        return {"ok": True, "empty": True, "verdict": verdict,
                "detail": "No filter set - everything is synced."}
    if verdict["dialect"]:
        return {"ok": False, "verdict": verdict,
                "detail": f"This is {verdict['dialect']}, not OData. "
                          + "; ".join(verdict["why"]) + "."}
    params = dict(params)
    params["$filter"] = text
    try:
        rows = _get_all_once(path, params, advanced=advanced)
    except GraphError as exc:
        return {"ok": False, "verdict": verdict, "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "verdict": verdict,
                "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "verdict": verdict,
            "detail": ("Accepted by Graph, and at least one record matches."
                       if rows else
                       "Accepted by Graph, but nothing matches it - a sync would "
                       "bring in nothing.")}


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
    # A renamed UPN, last: by now the new name has a row of its own, so this is
    # a merge rather than a rename, and every reference can be moved without
    # touching a primary key SQLite will not cascade.
    #
    # Without it, renaming somebody in Entra invents a second person here and
    # leaves the first holding their laptop and licences under a name nobody
    # uses. Entra keeps the object id across a rename, which is what makes the
    # two recognisable as one.
    from . import people
    renamed = 0
    for u in users:
        upn = (u.get("userPrincipalName") or "").strip().lower()
        oid = (u.get("id") or "").strip()
        if not upn or not oid:
            continue
        for old in db.q("SELECT upn FROM users WHERE entra_id = ? AND upn != ?",
                        (oid, upn)):
            if isinstance(people.merge(old["upn"], upn), dict):
                renamed += 1
                created = max(0, created - 1)   # not a new person, a renamed one

    return {"fetched": len(users), "created": created, "updated": updated,
            "skipped": skipped, "renamed": renamed}


# --- groups --------------------------------------------------------------

def sync_groups() -> dict:
    """Pull user membership for the groups ticked under Groups.

    Only the ticked ones: membership is a call per group, and on a real tenant
    most groups are nothing to do with ITAM.

    Needs the Graph application permission Group.Read.All (plus the existing
    User.Read.All) with admin consent.
    """
    groups = [dict(g) for g in db.q(
        "SELECT id, display_name AS displayName, description FROM entra_groups "
        "WHERE sync_users = 1 ORDER BY display_name")]

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


def scope_group_ids() -> list[str]:
    """Groups the device sync is limited to. Empty means every managed device."""
    return [r["id"] for r in
            db.q("SELECT id FROM entra_groups WHERE scope_devices = 1")]


def sync_devices() -> dict:
    """Pull managed devices from Intune and link them to assets by serial.

    With scope groups set, only devices in those groups are kept. managedDevices
    cannot be filtered by group membership at the API - $filter there is very
    limited - so the whole list comes down and is narrowed here against the
    membership the device-group sync recorded.

    Needs the Graph application permission
    DeviceManagementManagedDevices.Read.All with admin consent.
    """
    params = {"$select": DEVICE_SELECT, "$top": "999"}
    device_filter = settings.get("INTUNE_DEVICE_FILTER")
    if device_filter:
        params["$filter"] = device_filter
    # Intune's managedDevices does not support advanced query, so no opt-in here.
    devices = _get_all("/deviceManagement/managedDevices", params)

    scope = scope_group_ids()
    out_of_scope = 0
    if scope:
        marks = ",".join("?" for _ in scope)
        allowed = {r["azure_device_id"] for r in db.q(
            f"SELECT azure_device_id FROM device_group_members WHERE group_id IN ({marks})",
            scope)}
        if not allowed:
            # Filtering against nothing would drop every device and read as a
            # sync that simply found none. Refuse instead: that is a missing
            # group membership, not an empty fleet.
            raise GraphError(
                "The device sync is scoped to a group with no members recorded. "
                "Sync device groups first (Settings > Entra ID > Device groups), "
                "or clear the scope.")
        kept = [d for d in devices
                if (d.get("azureADDeviceId") or "").strip().lower() in allowed]
        out_of_scope = len(devices) - len(kept)
        devices = kept

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    created = updated = linked = 0

    with db.cursor() as conn:
        for d in devices:
            did = d.get("id")
            if not did:
                continue
            serial = (d.get("serialNumber") or "").strip() or None
            upn = (d.get("userPrincipalName") or "").strip().lower() or None
            azure_id = (d.get("azureADDeviceId") or "").strip().lower() or None

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
                                        storage_free, azure_device_id, synced_at, asset_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       device_name=excluded.device_name, serial_number=excluded.serial_number,
                       manufacturer=excluded.manufacturer, model=excluded.model,
                       os=excluded.os, os_version=excluded.os_version,
                       primary_upn=excluded.primary_upn,
                       azure_device_id=excluded.azure_device_id,
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
                 _int_or_none(d.get("freeStorageSpaceInBytes")), azure_id, now, asset_id))
            if exists:
                updated += 1
            else:
                created += 1
            if asset_id:
                linked += 1

    # Group membership decides what to hide, so it has to be as fresh as the
    # devices themselves - add a VM to the group in Entra and the next sync
    # hides it, with no separate step to remember.
    groups_refreshed = refresh_ignore_groups()
    hidden = devices_mod.recompute()

    return {"devices": len(devices), "created": created, "updated": updated,
            "linked_to_assets": linked, "ignored": hidden,
            "out_of_scope": out_of_scope,
            "groups_refreshed": groups_refreshed}


def fetch_group_devices(group_id: str) -> dict:
    """Devices in a group, following nested groups, and what happened.

    Cast to device for the same reason the user sync casts to user: /members
    would give direct members only, and a group of groups would silently
    resolve to nothing.

    No $select. Asking for id,deviceId,displayName came back with the right
    three device objects and no deviceId on any of them, while the portal was
    showing a Device Id for each - so the projection was the problem, not the
    permission. Taking whatever Graph offers costs nothing here: a group holds
    tens of devices, not thousands.

    Where a member still arrives without one, it is looked up by object id
    rather than dropped. deviceId is what Intune's azureADDeviceId matches on,
    so a member without it is useless, and silently discarding it is how "0
    devices" came to mean four different things.
    """
    members = _get_all(f"/groups/{group_id}/transitiveMembers/microsoft.graph.device",
                       {"$top": "999"})
    usable, unresolved = [], []
    for m in members:
        device_id = (m.get("deviceId") or "").strip().lower()
        if device_id:
            usable.append({"azure_device_id": device_id,
                           "device_name": m.get("displayName")})
        elif m.get("id"):
            unresolved.append(m)

    looked_up = 0
    lookup_error = None
    for m in unresolved:
        try:
            row = _get_one(f"/devices/{m['id']}")
        except GraphError as exc:
            lookup_error = str(exc)
            break
        device_id = (row.get("deviceId") or "").strip().lower()
        if device_id:
            looked_up += 1
            usable.append({"azure_device_id": device_id,
                           "device_name": row.get("displayName") or m.get("displayName")})

    out = {"devices": usable, "returned": len(members),
           "no_device_id": len(members) - len(usable), "looked_up": looked_up,
           "lookup_error": lookup_error, "probe": None}
    if members:
        return out

    # Nothing came back from the cast. Ask again without it and count what is
    # actually in there, so the page can say whether the group is empty or the
    # cast is being refused.
    try:
        raw = _get_all(f"/groups/{group_id}/transitiveMembers", {"$top": "999"})
    except GraphError:
        return out
    kinds: dict[str, int] = {}
    for m in raw:
        kind = str(m.get("@odata.type") or "unknown").split(".")[-1]
        kinds[kind] = kinds.get(kind, 0) + 1
    out["probe"] = {"total": len(raw), "kinds": kinds}
    return out


def _store_group_devices(group_id: str, members: list[dict]) -> None:
    db.execute("DELETE FROM device_group_members WHERE group_id = ?", (group_id,))
    for m in members:
        db.execute(
            """INSERT OR IGNORE INTO device_group_members
                   (group_id, azure_device_id, device_name) VALUES (?,?,?)""",
            (group_id, m["azure_device_id"], m["device_name"]))


def is_device_rule(group: dict) -> bool:
    """Does this group's own membership rule say it collects devices?

    A "Dynamic Device" group in the portal is a group whose groupTypes include
    DynamicMembership and whose membershipRule is written against `device.`.
    That is decided here rather than in an OData $filter on purpose: filtering
    directory objects with contains() is not reliably supported, and a filter
    Graph silently will not run is worse than no filter.
    """
    types = [str(t).lower() for t in (group.get("groupTypes") or [])]
    if "dynamicmembership" not in types:
        return False
    return "device." in (group.get("membershipRule") or "").lower()


GROUP_SELECT = "id,displayName,description,groupTypes,membershipRule"


def looks_like(group: dict) -> str:
    """A guess at what a group collects, to sort the list sensibly.

    Only a hint for the eye. Nothing syncs on the strength of it - you tick the
    boxes, because only you know that "Kiosks" is really a device group.
    """
    if is_device_rule(group):
        return "device"
    types = [str(t).lower() for t in (group.get("groupTypes") or [])]
    if "dynamicmembership" in types:
        return "user"
    return "assigned"


def discover_groups() -> dict:
    """List every group once and remember it. No membership is fetched.

    One paged call for the whole tenant, so this is cheap to re-run. Ticks
    already made are preserved: rediscovering must never silently switch a sync
    off.
    """
    group_filter = settings.get("ENTRA_GROUP_FILTER")
    params = {"$select": GROUP_SELECT, "$top": "999"}
    if group_filter:
        params["$filter"] = group_filter
    groups = _get_all("/groups", params, advanced=bool(group_filter))
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    seen = 0
    for g in groups:
        gid = g.get("id")
        if not gid:
            continue
        seen += 1
        db.execute(
            """INSERT INTO entra_groups (id, display_name, description, group_types,
                                         membership_rule, looks_like, discovered_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   display_name=excluded.display_name,
                   description=excluded.description,
                   group_types=excluded.group_types,
                   membership_rule=excluded.membership_rule,
                   looks_like=excluded.looks_like,
                   discovered_at=excluded.discovered_at""",
            (gid, g.get("displayName") or "(no name)", g.get("description"),
             ",".join(str(t) for t in (g.get("groupTypes") or [])),
             g.get("membershipRule"), looks_like(g), now))
    return {"groups": seen, "filter": group_filter}


def sync_device_groups() -> dict:
    """Fetch device members for the groups ticked under Device groups.

    One call per ticked group and not one more. Which groups those are is your
    decision, taken on a page that lists every group - guessing it from a name
    filter or a membership rule was never going to be right for everybody.

    Needs Group.Read.All. Reading the device objects behind the membership may
    also need Device.Read.All in some tenants; when the cast comes back empty
    the result says what was actually in the group instead of reporting nothing.
    """
    picked = db.q("SELECT * FROM entra_groups "
                  "WHERE sync_devices = 1 OR scope_devices = 1 "
                  "ORDER BY display_name")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    total_devices = looked_up = 0
    kept_ids, empty = [], []
    for g in picked:
        gid = g["id"]
        found = fetch_group_devices(gid)
        members = found["devices"]
        if not members:
            empty.append({"id": gid, "name": g["display_name"],
                          "returned": found["returned"],
                          "no_device_id": found["no_device_id"],
                          "lookup_error": found["lookup_error"],
                          "probe": found["probe"]})
            continue
        kept_ids.append(gid)
        total_devices += len(members)
        looked_up += found["looked_up"]
        db.execute(
            """INSERT INTO device_groups (id, display_name, description, device_count,
                                          dynamic, membership_rule, synced_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   display_name=excluded.display_name,
                   description=excluded.description,
                   device_count=excluded.device_count,
                   dynamic=excluded.dynamic,
                   membership_rule=excluded.membership_rule,
                   synced_at=excluded.synced_at""",
            (gid, g["display_name"], g["description"], len(members),
             1 if g["looks_like"] == "device" else 0, g["membership_rule"], now))
        _store_group_devices(gid, members)

    # Untick a group and its devices should stop being tracked; a group that
    # came back empty this time keeps what it had, because an empty answer is
    # as likely to be a permission as a real change.
    tracked = {g["id"] for g in picked if g["sync_devices"]}
    dropped = 0
    for row in db.q("SELECT id FROM device_groups"):
        if row["id"] not in tracked:
            db.execute("DELETE FROM device_groups WHERE id = ?", (row["id"],))
            db.execute("DELETE FROM device_group_members WHERE group_id = ?", (row["id"],))
            dropped += 1

    return {"picked": len(picked), "device_groups": len(kept_ids),
            "devices": total_devices, "dropped": dropped, "empty": empty,
            "looked_up": looked_up}


def refresh_ignore_groups() -> int:
    """Re-read just the groups the ignore rules name.

    Cheap - only the groups actually in use - so it can run on every device
    sync. It also means an ignore rule works for a group that the device-group
    sync never looked at, because a filter excluded it.
    """
    wanted = devices_mod.group_rules()
    for group_id in wanted:
        _store_group_devices(group_id, fetch_group_devices(group_id)["devices"])
    return len(wanted)


# --- Windows hardware inventory -----------------------------------------

# Intune's Device inventory. Beta, and undocumented at the time of writing:
# Microsoft ships the categories in the portal without publishing the resource.
# So nothing here assumes a category id or a property name - the categories are
# read back from the tenant and whatever properties come with them are stored.
# Guessing at the shape is how the last three Graph details went wrong.
INVENTORY_BASE = "/deviceManagement/managedDevices"


def sync_physical_memory() -> dict:
    """Total RAM per device, from beta managedDevices.

    physicalMemoryInBytes is not in v1.0, but it is one field on a LIST call -
    one paged request for the whole fleet, on the endpoint the device sync
    already has permission for. No Device inventory, no per-device calls.

    It is reported as 0 on some tenants and for some platforms, so the result
    says how many devices actually gave a number. A 0 is stored as nothing
    rather than as "0GB".

    The bytes go on the device row, next to the storage Graph reports the same
    way. They are not device_attributes: that table is what a script running on
    the machine said about itself, and a Mac's spec tag should not arrive
    alongside a figure ITAM worked out by division.
    """
    rows = _get_all("/deviceManagement/managedDevices",
                    {"$select": "id,physicalMemoryInBytes", "$top": "999"},
                    base=GRAPH_BETA)
    stored = zero = unknown = 0
    for row in rows:
        did = row.get("id")
        try:
            total = int(row.get("physicalMemoryInBytes") or 0)
        except (TypeError, ValueError):
            total = 0
        if not did:
            continue
        if not db.q1("SELECT 1 FROM devices WHERE id = ?", (did,)):
            unknown += 1
            continue
        if total <= 0:
            zero += 1
            continue
        db.execute("UPDATE devices SET memory_total = ? WHERE id = ?", (total, did))
        stored += 1
    return {"devices": len(rows), "stored": stored, "reported_zero": zero,
            "not_in_itam": unknown}


def inventory_categories(device_id: str) -> list[dict]:
    """Which inventory categories Intune holds for this device.

    One call. Raises GraphError with Graph's own words if the endpoint is not
    available on the tenant, rather than reporting an empty inventory.
    """
    return _get_all(f"{INVENTORY_BASE}('{device_id}')/deviceInventories",
                    {}, base=GRAPH_BETA)


def _flatten(instance: dict, prefix: str) -> dict:
    """Property name -> value, from one inventory instance.

    The shape is nested and undocumented, so this walks whatever comes back
    rather than reaching for known keys. A single-instance category (the CPU)
    yields "CPU / Name"; a multi-instance one (three disks) yields
    "Disk Drive 1 / Size".
    """
    out = {}
    for prop in instance.get("properties") or []:
        name = str(prop.get("displayName") or prop.get("name") or "").strip()
        value = prop.get("value")
        if isinstance(value, dict):
            value = value.get("value", value)
        if name and value not in (None, "", []):
            out[f"{prefix} / {name}"] = str(value)
    return out


def sync_hardware_inventory(limit: int | None = None) -> dict:
    """Store Intune's hardware inventory as device attributes.

    macOS reports CPU and RAM through a custom attribute script; Windows
    reports it through Device inventory instead, so this lands the same facts in
    the same place - device_attributes - and everything downstream (the person's
    card, pricing criteria, the device list) works with no further change.

    It is a call per device, so INTUNE_ATTRIBUTE_FILTER applies here too: only
    the categories you want are fetched, and only their properties are kept.
    """
    wanted = attribute_patterns()
    rows = db.q("SELECT id, device_name FROM devices WHERE ignored_reason IS NULL "
                "ORDER BY device_name" + (f" LIMIT {int(limit)}" if limit else ""))
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    stored = devices_seen = 0
    categories_found: set = set()
    for row in rows:
        try:
            cats = inventory_categories(row["id"])
        except GraphError as exc:
            # The first refusal is the answer for every device, so stop rather
            # than making the same failing call ninety-seven times - and raise,
            # because a job that fetched nothing did not succeed.
            raise GraphError(f"Device inventory is not readable: {exc}") from exc
        if not cats:
            continue
        devices_seen += 1
        for cat in cats:
            label = str(cat.get("displayName") or cat.get("id") or "").strip()
            if not label:
                continue
            categories_found.add(label)
            if wanted and not attribute_wanted(label, wanted):
                continue
            try:
                full = _get_one(
                    f"{INVENTORY_BASE}('{row['id']}')/deviceInventories('{cat.get('id')}')",
                    {"$expand": "instances($expand=properties)"}, base=GRAPH_BETA)
            except GraphError:
                continue
            instances = full.get("instances") or []
            for n, inst in enumerate(instances, start=1):
                prefix = label if len(instances) == 1 else f"{label} {n}"
                for name, value in _flatten(inst, prefix).items():
                    db.execute(
                        """INSERT INTO device_attributes (device_id, name, value, collected_at)
                           VALUES (?,?,?,?)
                           ON CONFLICT(device_id, name) DO UPDATE SET
                               value = excluded.value,
                               collected_at = excluded.collected_at""",
                        (row["id"], name, value, now))
                    stored += 1

    return {"devices": devices_seen, "stored": stored,
            "categories": sorted(categories_found)[:12]}


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


def _get_one(path: str, params: dict | None = None, base: str = GRAPH) -> dict:
    """A single Graph object, not a collection."""
    token = _token()
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{base}{path}",
                          headers={"Authorization": f"Bearer {token}"},
                          params=params or {})
    if resp.status_code >= 400:
        raise GraphError(_explain(resp, path))
    return resp.json() or {}


def _get_all_once(path: str, params: dict, base: str = GRAPH,
                  advanced: bool = False) -> list[dict]:
    """One page only - enough to prove a permission works, without walking a
    whole tenant just to run a test."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    if advanced:
        # Same opt-in the real sync uses, or a filter that works in production
        # would fail its own test.
        headers["ConsistencyLevel"] = "eventual"
        params = dict(params)
        params["$count"] = "true"
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{base}{path}", headers=headers, params=params)
    if resp.status_code >= 400:
        raise GraphError(_explain(resp, path))
    return (resp.json() or {}).get("value", [])

