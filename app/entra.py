"""Microsoft Graph user sync (app-only / client credentials).

Configure via environment variables:
    ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET
Required Graph application permission: User.Read.All (admin consented).

UPN (userPrincipalName) is the unique key used throughout the app.
"""
import datetime
import os

import httpx

from . import db

GRAPH = "https://graph.microsoft.com/v1.0"
# Intune custom attribute shell scripts are only exposed on the beta endpoint.
GRAPH_BETA = "https://graph.microsoft.com/beta"
SELECT = "id,userPrincipalName,displayName,jobTitle,department,accountEnabled"

DEVICE_SELECT = ("id,deviceName,serialNumber,manufacturer,model,operatingSystem,"
                 "osVersion,userPrincipalName,complianceState,enrolledDateTime,"
                 "lastSyncDateTime,totalStorageSpaceInBytes,freeStorageSpaceInBytes")


def is_configured() -> bool:
    return all(os.environ.get(k) for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"))


def config_status() -> dict:
    return {
        "configured": is_configured(),
        "tenant_id": os.environ.get("ENTRA_TENANT_ID", ""),
        "client_id": os.environ.get("ENTRA_CLIENT_ID", ""),
        "secret_set": bool(os.environ.get("ENTRA_CLIENT_SECRET")),
        "filter": (os.environ.get("ENTRA_USER_FILTER") or "").strip(),
        "group_filter": (os.environ.get("ENTRA_GROUP_FILTER") or "").strip(),
        "device_filter": (os.environ.get("INTUNE_DEVICE_FILTER") or "").strip(),
    }


def _token() -> str:
    tenant = os.environ["ENTRA_TENANT_ID"]
    resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": os.environ["ENTRA_CLIENT_ID"],
            "client_secret": os.environ["ENTRA_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
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
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
            params = None          # nextLink already carries the query string
    return out


def fetch_users() -> list[dict]:
    """Page through all users in the tenant."""
    params = {"$select": SELECT, "$top": "999"}
    user_filter = (os.environ.get("ENTRA_USER_FILTER") or "").strip()
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
                                      account_enabled, source, synced_at)
                   VALUES (?,?,?,?,?,?,'entra',?)
                   ON CONFLICT(upn) DO UPDATE SET
                       display_name=excluded.display_name,
                       job_title=excluded.job_title,
                       department=excluded.department,
                       entra_id=excluded.entra_id,
                       account_enabled=excluded.account_enabled,
                       source='entra',
                       synced_at=excluded.synced_at""",
                (
                    upn,
                    u.get("displayName") or upn,
                    u.get("jobTitle"),
                    u.get("department"),
                    u.get("id"),
                    1 if u.get("accountEnabled", True) else 0,
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
    group_filter = (os.environ.get("ENTRA_GROUP_FILTER") or "").strip()
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
        # Only members already synced as users can be linked, so run the user
        # sync first; anything else is counted and reported.
        members = _get_all(f"/groups/{gid}/members",
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
            for upn in upns:
                known = conn.execute("SELECT 1 FROM users WHERE upn = ?", (upn,)).fetchone()
                if known:
                    conn.execute(
                        "INSERT OR IGNORE INTO group_members (group_id, upn) VALUES (?,?)",
                        (gid, upn))
            if exists:
                updated += 1
            else:
                created += 1

        linked = db.q1("SELECT COUNT(*) c FROM group_members WHERE group_id = ?", (gid,))["c"]
        members_linked += linked
        skipped_members += len(upns) - linked

    return {"groups": len(groups), "created": created, "updated": updated,
            "members_linked": members_linked, "members_unknown": skipped_members}


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
    device_filter = (os.environ.get("INTUNE_DEVICE_FILTER") or "").strip()
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

def sync_custom_attributes() -> dict:
    """Merge Intune custom attribute results onto their devices.

    macOS custom attributes are shell scripts whose stdout Intune stores as the
    result. Each script has per-device run states carrying that output, which is
    what gets stored here - so an attribute reporting CPU and RAM shows up on
    the device.

    Beta endpoint (no v1.0 equivalent). Needs
    DeviceManagementConfiguration.Read.All with admin consent.
    """
    scripts = _get_all("/deviceManagement/deviceCustomAttributeShellScripts",
                       {"$select": "id,displayName"}, base=GRAPH_BETA)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    stored = skipped = 0

    for script in scripts:
        sid = script.get("id")
        label = script.get("displayName") or sid
        if not sid:
            continue
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

    return {"scripts": len(scripts), "attributes_stored": stored, "skipped": skipped}
