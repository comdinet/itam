"""Pricing by what a machine is made of.

A Latitude 5450 with 32GB did not cost what one with 16GB cost, and the model
string is the same for both. Memory and disk are already synced for every
managed device; until now a price rule could not see them.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ["ITAM_DB"] = tempfile.mktemp(suffix=".db")

from app import db, fx, pricing                     # noqa: E402

db.init_db()
fx.add("ILS", "₪", "Israeli shekel", 270_000, "manual", "test")
fails = []


def check(what, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {what}: got={got!r} want={want!r}")
    if not ok:
        fails.append(what)


GB = 1000 ** 3
GIB = 1024 ** 3


def machine(name, model, ram_gb, disk_gb):
    aid = db.execute("""INSERT INTO assets (name, category, cost_cents)
                        VALUES (?,'Laptop',0)""", (name,))
    db.execute("""INSERT INTO devices (id, device_name, model, os, asset_id,
                                       memory_total, storage_total)
                  VALUES (?,?,?,'Windows',?,?,?)""",
               (name, name, model, aid,
                int(ram_gb * GIB) if ram_gb else 0,
                int(disk_gb * GB) if disk_gb else 0))
    return aid


big = machine("PC-BIG", "Latitude 5450", 32, 1000)
small = machine("PC-SMALL", "Latitude 5450", 16, 512)
mid = machine("PC-MID", "Latitude 5450", 16, 1000)
unknown = machine("PC-UNKNOWN", "Latitude 5450", 0, 0)


def matched(*criteria):
    gid = pricing.create("t", 100, currency="ILS", rate_micro=270_000)
    for field, op, value in criteria:
        pricing.add_criterion(gid, field, op, value)
    names = sorted(a["name"] for a in pricing.matching_assets(gid))
    pricing.delete(gid)
    return names


print("--- the same model, priced by what is in it ---")
check("32GB only", matched(("model", "contains", "Latitude 5450"),
                           ("ram_gb", "eq", "32")), ["PC-BIG"])
check("16GB only", matched(("model", "contains", "Latitude 5450"),
                           ("ram_gb", "eq", "16")), ["PC-MID", "PC-SMALL"])
check("at least 32GB", matched(("ram_gb", "gte", "32")), ["PC-BIG"])
check("at most 16GB", matched(("ram_gb", "lte", "16")), ["PC-MID", "PC-SMALL"])

print("\n--- and by disk, which is sold in decimal GB ---")
check("a 512GB disk is found by asking for 512",
      matched(("disk_gb", "eq", "512")), ["PC-SMALL"])
check("a terabyte", matched(("disk_gb", "gte", "1000")), ["PC-BIG", "PC-MID"])

print("\n--- memory is binary, because that is how it is sold ---")
# A 32GB machine reports 34,359,738,368 bytes. Dividing by a billion gives 34,
# which is not a size anybody has bought and would match no rule anybody writes.
check("34359738368 bytes is 32GB, not 34",
      matched(("ram_gb", "eq", "32")), ["PC-BIG"])
check("and asking for 34 finds nothing", matched(("ram_gb", "eq", "34")), [])

print("\n--- the two together, which is the point ---")
check("model and memory", matched(("model", "contains", "Latitude 5450"),
                                  ("ram_gb", "gte", "32"),
                                  ("disk_gb", "gte", "1000")), ["PC-BIG"])

print("\n--- a machine that has not reported is never priced by a guess ---")
check("zero is not 'at most 16'", "PC-UNKNOWN" in matched(("ram_gb", "lte", "16")), False)
check("nor exactly 0", matched(("ram_gb", "eq", "0")), [])

print("\n--- numbers are compared as numbers ---")
# "16" sorting before "8" is the kind of thing that prices a fleet wrong and is
# never noticed.
machine("PC-TINY", "Latitude 5450", 8, 256)
check("8 is less than 16", matched(("ram_gb", "lte", "16")),
      ["PC-MID", "PC-SMALL", "PC-TINY"])
check("and 8 is not at least 16", "PC-TINY" in matched(("ram_gb", "gte", "16")), False)

print("\n--- the form cannot offer nonsense ---")


def refused(field, op, value):
    gid = pricing.create("t", 100, currency="ILS", rate_micro=270_000)
    try:
        pricing.add_criterion(gid, field, op, value)
        return None
    except ValueError as exc:
        return str(exc)
    finally:
        pricing.delete(gid)


check("'contains' on a number is refused",
      refused("ram_gb", "contains", "16") is not None, True)
check("and so is a value that is not one",
      "number of GB" in (refused("ram_gb", "eq", "loads") or ""), True)
check("while text fields keep their own operators",
      refused("model", "contains", "Latitude"), None)
check("a numeric operator on a text field is refused",
      refused("model", "gte", "Latitude") is not None, True)

print("\n--- it reads as a sentence, without quotes round a number ---")
gid = pricing.create("t", 100, currency="ILS", rate_micro=270_000)
pricing.add_criterion(gid, "ram_gb", "gte", "32")
check("described plainly", pricing.describe(pricing.criteria(gid)[0]),
      "Memory (GB) is at least 32")
pricing.delete(gid)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
