"""Command-line sync jobs, for running on a schedule.

    docker compose exec -T itam python -m app.jobs all
    docker compose exec -T itam python -m app.jobs licences

Every job is safe to re-run: syncs upsert and never delete inventory. Exit
status is non-zero if any job failed, so cron will report a real failure.
"""
import datetime
import sys

from . import db, entra

JOBS = {
    "users": ("Entra ID users", lambda: entra.sync()),
    "groups": ("Entra ID groups", lambda: entra.sync_groups()),
    "licences": ("Entra ID licences", lambda: entra.sync_licenses()),
    "devices": ("Intune devices", lambda: entra.sync_devices()),
    "attributes": ("Intune custom attributes", lambda: entra.sync_custom_attributes()),
}

# Users first: groups, licences and devices all reference them.
ORDER = ["users", "groups", "licences", "devices", "attributes"]


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def run(names: list[str]) -> int:
    db.init_db()
    if not entra.is_configured():
        print(f"{_stamp()}  ERROR  Entra ID is not configured "
              "(ENTRA_TENANT_ID / ENTRA_CLIENT_ID / ENTRA_CLIENT_SECRET)", file=sys.stderr)
        return 2

    failed = 0
    for name in names:
        label, fn = JOBS[name]
        try:
            result = fn()
            detail = ", ".join(f"{k}={v}" for k, v in result.items())
            print(f"{_stamp()}  OK     {label}: {detail}", flush=True)
        except Exception as exc:
            failed += 1
            detail = str(exc) if isinstance(exc, entra.GraphError) \
                else f"{type(exc).__name__}: {exc}"
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
