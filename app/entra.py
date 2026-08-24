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
SELECT = "id,userPrincipalName,displayName,jobTitle,department,accountEnabled"


def is_configured() -> bool:
    return all(os.environ.get(k) for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"))


def config_status() -> dict:
    return {
        "configured": is_configured(),
        "tenant_id": os.environ.get("ENTRA_TENANT_ID", ""),
        "client_id": os.environ.get("ENTRA_CLIENT_ID", ""),
        "secret_set": bool(os.environ.get("ENTRA_CLIENT_SECRET")),
        "filter": os.environ.get("ENTRA_USER_FILTER", ""),
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


def fetch_users() -> list[dict]:
    """Page through all users in the tenant."""
    token = _token()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"$select": SELECT, "$top": "999"}
    user_filter = os.environ.get("ENTRA_USER_FILTER")
    if user_filter:
        params["$filter"] = user_filter
    url = f"{GRAPH}/users"
    out: list[dict] = []
    with httpx.Client(timeout=60) as client:
        while url:
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("value", []))
            url = body.get("@odata.nextLink")
            params = None  # nextLink already carries the query string
    return out


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
