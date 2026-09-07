"""Command-line sync jobs, for running on a schedule.

    docker compose exec -T itam python -m app.jobs all
    docker compose exec -T itam python -m app.jobs licences

Every job is safe to re-run: syncs upsert and never delete inventory. Exit
status is non-zero if any job failed, so cron will report a real failure.
"""
import datetime
import sys

from . import db, devices, entra, people


def _sync_users():
    result = entra.sync()
    # A newcomer must not walk past the ignore rules just because they were
    # written before that person existed.
    result["ignored"] = people.recompute()
    return result


def _sync_devices():
    result = entra.sync_devices()
    result["ignored"] = devices.recompute()
    return result


def _fill_holders():
    """Give assets the holder Intune already knows about.

    The holder is copied from the device when the ASSET is created, and only
    then. A machine handed to somebody else last week updates devices.primary_upn
    and leaves the asset exactly as it was, so without this step the two drift
    apart quietly and for good. Only assets with no holder at all are filled in;
    a disagreement is reported on the Devices page and left alone.
    """
    gap = devices.holder_gap()
    filled = devices.fill_holders_from_intune()
    return {"filled": filled, "still_unknown_person": len(gap["unknown"]),
            "no_primary_user": len(gap["nobody"]),
            "disagreements": len(gap["mismatch"])}


JOBS = {
    "users": ("Entra ID users", _sync_users),
    "groups": ("Entra ID groups", lambda: entra.sync_groups()),
    "device_groups": ("Entra device groups", lambda: entra.sync_device_groups()),
    "licences": ("Entra ID licences", lambda: entra.sync_licenses()),
    "devices": ("Intune devices", _sync_devices),
    "attributes": ("Intune custom attributes (macOS)", lambda: entra.sync_custom_attributes()),
    "hardware": ("Intune hardware inventory (Windows)",
                 lambda: entra.sync_hardware_inventory()),
    "holders": ("Asset holders from Intune", _fill_holders),
}

# Users first: groups, licences and devices all reference them. Holders last:
# it needs both the people and the devices to be current.
ORDER = ["users", "groups", "device_groups", "licences", "devices", "attributes",
         "hardware", "holders"]


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def record(job: str, started: str, ok: bool, detail: str, source: str = "cron") -> None:
    """Write down that this ran. Kept short - the last 200 runs is plenty."""
    db.execute(
        """INSERT INTO sync_runs (job, started_at, finished_at, ok, detail, source)
           VALUES (?,?,?,?,?,?)""",
        (job, started, _iso(), 1 if ok else 0, (detail or "")[:500], source))
    db.execute("""DELETE FROM sync_runs WHERE id NOT IN
                  (SELECT id FROM sync_runs ORDER BY id DESC LIMIT 200)""")


def run(names: list[str], source: str = "cron") -> int:
    db.init_db()
    if not entra.is_configured():
        print(f"{_stamp()}  ERROR  Entra ID is not configured "
              "(ENTRA_TENANT_ID / ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET)", file=sys.stderr)
        return 2

    failed = 0
    for name in names:
        label, fn = JOBS[name]
        started = _iso()
        try:
            result = fn()
            detail = ", ".join(f"{k}={v}" for k, v in result.items())
            record(name, started, True, detail, source)
            print(f"{_stamp()}  OK     {label}: {detail}", flush=True)
        except Exception as exc:
            failed += 1
            detail = str(exc) if isinstance(exc, entra.GraphError) \
                else f"{type(exc).__name__}: {exc}"
            record(name, started, False, detail, source)
            print(f"{_stamp()}  FAILED {label}: {detail}", file=sys.stderr, flush=True)
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    args = argv[1:] or ["all"]
    if args[0] in ("-h", "--help"):
        print(__doc__)
        print("Jobs: " + ", ".join(ORDER) + ", all")
        return 0
    if args == ["all"]:
        return run(ORDER)
    unknown = [a for a in args if a not in JOBS]
    if unknown:
        print(f"Unknown job(s): {', '.join(unknown)}. "
              f"Available: {', '.join(ORDER)}, all", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
