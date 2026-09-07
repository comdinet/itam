"""CSV import from ITAM's own templates.

Deliberately not an importer for anybody's export format. Claude, ChatGPT,
Cursor and Notion all export something different, all of them change it without
telling you, and chasing that is a permanent job. Instead ITAM publishes a
template: download it, paste your data into the columns, upload it back. One
shape to support, and it works for a vendor nobody has heard of yet.

Nothing is written until a preview has been looked at. An import that assigns
a hundred seats is not something to run blind.
"""
import csv
import datetime
import io

from . import db, fx

# A header is matched case- and space-insensitively, so "Subscription Name"
# and "subscription name" are the same column.
def _key(header: str) -> str:
    return " ".join((header or "").replace("﻿", "").split()).lower()


SUBSCRIPTION_SEATS = {
    "id": "subscription-seats",
    "label": "Subscription seats",
    "filename": "itam-subscription-seats.csv",
    "blurb": ("One row per person per licence. Creates any subscription the file "
              "names, and gives that person a seat on it."),
    "columns": [
        ("Email", True, "The person's UPN, as it is in Entra ID."),
        ("Subscription name", True, "The product. Rows sharing a name share a subscription."),
        ("Subscription tier", False,
         "Premium, Standard, Business… Appended to the name, so Claude AI + Premium "
         "becomes one subscription and Claude AI + Standard another. Leave blank "
         "if the product has no tiers."),
        ("Monthly cost", False, "Per seat, per month. Leave blank to set it later."),
        ("Currency", False, "Required if you give a cost. ILS, EUR, GBP, USD…"),
        ("Vendor", False, "Optional, for your own reporting."),
    ],
    "example": [
        ["someone@example.com", "Claude AI", "Premium", "150.00", "USD", "Anthropic"],
        ["someone.else@example.com", "Claude AI", "Standard", "25.00", "USD", "Anthropic"],
    ],
}

DEVICE_SPECS = {
    "id": "device-specs",
    "label": "Device specs",
    "filename": "itam-device-specs.csv",
    "blurb": ("What a machine is made of, keyed on its serial. For the Windows "
              "facts Graph will not give up: Intune's Device inventory page has "
              "an Export button, and this takes what comes out of it."),
    "columns": [
        ("Serial", True, "The serial number, as Intune reports it. This is what "
                         "matches the device already in ITAM."),
        ("CPU", False, "Processor model, e.g. Intel(R) Core(TM) Ultra 5 125U."),
        ("RAM", False, "Total memory, e.g. 32GB or 32."),
        ("Disk", False, "Disk size, e.g. 512GB or 512."),
    ],
    "example": [
        ["4CN8RQ3", "Intel(R) Core(TM) Ultra 5 125U", "16GB", "512GB"],
        ["2CH2N64", "Intel(R) Core(TM) Ultra 7 165U", "32GB", "1TB"],
    ],
}

TEMPLATES = {SUBSCRIPTION_SEATS["id"]: SUBSCRIPTION_SEATS,
             DEVICE_SPECS["id"]: DEVICE_SPECS}


def template_csv(spec) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([name for name, _, _ in spec["columns"]])
    for row in spec["example"]:
        writer.writerow(row)
    return buf.getvalue()


class ImportError_(Exception):
    """Something wrong with the file itself, as opposed to a row in it."""


def _rows(text: str, spec) -> list[dict]:
    """Parse to dicts keyed by normalised column name, or explain the refusal."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportError_("That file is empty.")
    seen = [_key(h) for h in header]
    required = [name for name, req, _ in spec["columns"] if req]
    missing = [name for name in required if _key(name) not in seen]
    if missing:
        raise ImportError_(
            "The file is missing " + ", ".join(f"“{m}”" for m in missing)
            + ". Its columns are: " + ", ".join(h for h in header if h.strip())
            + ". Download the template and paste your data into it.")
    out = []
    for number, values in enumerate(reader, start=2):
        if not any((v or "").strip() for v in values):
            continue                       # a blank line is not a row
        row = {seen[i]: (values[i] or "").strip()
               for i in range(min(len(seen), len(values)))}
        row["_line"] = number
        out.append(row)
    return out


def subscription_name(name: str, tier: str) -> str:
    return f"{name} {tier}".strip() if tier else name.strip()


def plan_subscription_seats(text: str) -> dict:
    """What an import would do, without doing any of it."""
    rows = _rows(text, SUBSCRIPTION_SEATS)

    known = {r["upn"] for r in db.q("SELECT upn FROM users")}
    existing = {r["name"].strip().lower(): r
                for r in db.q("SELECT * FROM subscriptions")}
    held = {(r["subscription_id"], r["upn"])
            for r in db.q("SELECT subscription_id, upn FROM subscription_seats")}

    subs: dict[str, dict] = {}
    skipped, seats, already = [], [], []

    for row in rows:
        line = row["_line"]
        email = row.get("email", "").lower()
        product = row.get("subscription name", "")
        tier = row.get("subscription tier", "")
        if not email:
            skipped.append({"line": line, "what": "(no email)", "why": "no email given"})
            continue
        if not product:
            skipped.append({"line": line, "what": email,
                            "why": "no subscription name given"})
            continue
        if email not in known:
            skipped.append({"line": line, "what": email,
                            "why": "nobody in ITAM has that UPN"})
            continue

        name = subscription_name(product, tier)
        key = name.lower()
        entry = subs.get(key)
        if entry is None:
            match = existing.get(key)
            entry = subs[key] = {
                "name": name, "exists": match is not None,
                "id": match["id"] if match else None,
                "cost_cents": match["monthly_cost_cents"] if match else 0,
                "currency": match["currency"] if match else None,
                "rate_micro": match["rate_micro"] if match else None,
                "vendor": match["vendor"] if match else None,
                "priced_here": False, "upns": [], "problems": [],
            }

        cost, currency = row.get("monthly cost", ""), row.get("currency", "").upper()
        if cost:
            if not currency:
                entry["problems"].append(
                    f"line {line}: a cost of {cost} with no currency - "
                    "an amount with no currency is a guess, so the cost is ignored")
            elif not fx.get(currency):
                entry["problems"].append(
                    f"line {line}: {currency} is not set up under Settings > "
                    "Currencies, so the cost is ignored")
            else:
                cents = db.to_cents(cost)
                rate = int(fx.get(currency)["rate_micro"])
                if entry["priced_here"] and (entry["cost_cents"] != cents
                                             or entry["currency"] != currency):
                    entry["problems"].append(
                        f"line {line}: a second price for the same subscription "
                        f"({currency} {db.money(cents)}); the first one wins")
                elif not entry["priced_here"]:
                    entry["cost_cents"], entry["currency"] = cents, currency
                    entry["rate_micro"], entry["priced_here"] = rate, True
        if row.get("vendor"):
            entry["vendor"] = entry["vendor"] or row["vendor"]

        if entry["exists"] and (entry["id"], email) in held:
            already.append({"line": line, "upn": email, "name": name})
        elif email in entry["upns"]:
            already.append({"line": line, "upn": email, "name": name,
                            "duplicate_row": True})
        else:
            entry["upns"].append(email)
            seats.append({"line": line, "upn": email, "name": name})

    ordered = sorted(subs.values(), key=lambda e: e["name"].lower())
    return {"rows": len(rows), "subscriptions": ordered, "seats": seats,
            "already": already, "skipped": skipped,
            "to_create": [e for e in ordered if not e["exists"]],
            "unpriced": [e for e in ordered
                         if not e["exists"] and not e["priced_here"]]}


def apply_subscription_seats(text: str) -> dict:
    """Run the plan. Re-parsed rather than trusted, so it cannot drift."""
    plan = plan_subscription_seats(text)
    today = datetime.date.today().isoformat()
    created = 0
    for entry in plan["subscriptions"]:
        sub_id = entry["id"]
        if sub_id is None:
            sub_id = db.execute(
                """INSERT INTO subscriptions (name, vendor, monthly_cost_cents,
                                              currency, rate_micro)
                   VALUES (?,?,?,?,?)""",
                (entry["name"], entry["vendor"], entry["cost_cents"],
                 entry["currency"], entry["rate_micro"]))
            created += 1
        elif entry["priced_here"]:
            # An existing subscription keeps its price unless the file states
            # one: an import should not silently reprice what you set by hand.
            db.execute(
                """UPDATE subscriptions SET monthly_cost_cents = ?, currency = ?,
                                            rate_micro = ? WHERE id = ?""",
                (entry["cost_cents"], entry["currency"], entry["rate_micro"], sub_id))
        for upn in entry["upns"]:
            db.execute(
                """INSERT OR IGNORE INTO subscription_seats
                       (subscription_id, upn, assigned_on) VALUES (?,?,?)""",
                (sub_id, upn, today))
    return {"created": created, "seats": len(plan["seats"]),
            "skipped": len(plan["skipped"]), "already": len(plan["already"])}


def plan_device_specs(text: str) -> dict:
    """What importing specs would do. Nothing is written.

    Matched on serial, because that is the one identifier every export carries
    and the one ITAM already holds. A serial ITAM does not know is reported, not
    invented: a device arrives from Intune, never from a spreadsheet.
    """
    rows = _rows(text, DEVICE_SPECS)
    fields = [("cpu", "CPU"), ("ram", "RAM"), ("disk", "Disk")]

    known = {(r["serial_number"] or "").strip().lower(): r for r in
             db.q("SELECT id, serial_number, device_name FROM devices "
                  "WHERE TRIM(COALESCE(serial_number,'')) != ''")}
    updates, skipped, empty = [], [], 0
    seen = set()
    for row in rows:
        line = row["_line"]
        serial = row.get("serial", "")
        if not serial:
            skipped.append({"line": line, "what": "(no serial)",
                            "why": "no serial given"})
            continue
        device = known.get(serial.lower())
        if not device:
            skipped.append({"line": line, "what": serial,
                            "why": "no device in ITAM has that serial"})
            continue
        if serial.lower() in seen:
            skipped.append({"line": line, "what": serial,
                            "why": "the same serial appears earlier in the file"})
            continue
        values = [(label, row.get(key, "").strip())
                  for key, label in fields if row.get(key, "").strip()]
        if not values:
            empty += 1
            continue
        seen.add(serial.lower())
        updates.append({"line": line, "serial": serial, "device_id": device["id"],
                        "device_name": device["device_name"], "values": values})
    return {"rows": len(rows), "updates": updates, "skipped": skipped,
            "no_values": empty}


def apply_device_specs(text: str) -> dict:
    """Store the specs as device attributes, under the column they came from."""
    plan = plan_device_specs(text)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    stored = 0
    for row in plan["updates"]:
        for label, value in row["values"]:
            db.execute(
                """INSERT INTO device_attributes (device_id, name, value, collected_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(device_id, name) DO UPDATE SET
                       value = excluded.value, collected_at = excluded.collected_at""",
                (row["device_id"], label, value, now))
            stored += 1
    return {"devices": len(plan["updates"]), "stored": stored,
            "skipped": len(plan["skipped"])}
