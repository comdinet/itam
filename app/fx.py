"""Currencies, and the rates used to add them together.

Two rules shape everything here:

  Money is stored in the currency it was paid in, never converted on the way
  in. A laptop bought in Tel Aviv is 11,900 ILS, permanently.

  The rate is frozen when the amount is entered. Last year's totals do not move
  because the shekel did, which is what book value means. A rate changed today
  affects only what is entered from today.

Rates are USD per one unit of the currency, stored times 1,000,000 so every
conversion is integer arithmetic and nothing drifts through floating point.
"""
import datetime

import httpx

from . import db

MICRO = 1_000_000
BASE = "USD"

# Bank of Israel publishes every rate against the shekel, so one call yields
# all of them. Rates are per `unit` of the foreign currency - JPY is quoted
# per 100, for instance - which the unit divisor takes care of.
BOI_URL = "https://boi.org.il/PublicApi/GetExchangeRates?asJson=true"

KNOWN_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "ILS": "₪", "JPY": "¥",
    "CHF": "CHF", "AUD": "A$", "CAD": "C$", "SEK": "kr", "NOK": "kr",
    "DKK": "kr", "ZAR": "R", "EGP": "E£", "JOD": "JD", "LBP": "L£",
}
KNOWN_NAMES = {
    "USD": "US dollar", "EUR": "Euro", "GBP": "Pound sterling",
    "ILS": "Israeli new shekel", "JPY": "Japanese yen", "CHF": "Swiss franc",
    "AUD": "Australian dollar", "CAD": "Canadian dollar",
    "SEK": "Swedish krona", "NOK": "Norwegian krone", "DKK": "Danish krone",
    "ZAR": "South African rand", "EGP": "Egyptian pound",
    "JOD": "Jordanian dinar", "LBP": "Lebanese pound",
}


def _today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --- the reporting currency ---------------------------------------------

def reporting_code() -> str:
    from . import settings
    return (settings.currency() or BASE).upper()


def ensure_base() -> None:
    """The reporting currency always exists, at a rate of exactly 1."""
    code = reporting_code()
    db.execute(
        """INSERT INTO currencies (code, symbol, name, rate_micro, rate_set_on,
                                   rate_source, active)
           VALUES (?,?,?,?,?,'base',1)
           ON CONFLICT(code) DO UPDATE SET rate_micro = 1000000, rate_source = 'base'""",
        (code, KNOWN_SYMBOLS.get(code, code), KNOWN_NAMES.get(code, code),
         MICRO, _today()))


# --- currency records ----------------------------------------------------

def listing(active_only: bool = False):
    sql = "SELECT * FROM currencies"
    if active_only:
        sql += " WHERE active = 1"
    return db.q(sql + " ORDER BY CASE WHEN rate_source='base' THEN 0 ELSE 1 END, code")


def get(code: str):
    return db.q1("SELECT * FROM currencies WHERE code = ?", ((code or "").upper(),))


def add(code: str, symbol: str, name: str, rate_micro: int, source: str,
        by: str, set_on: str | None = None) -> None:
    code = code.strip().upper()
    db.execute(
        """INSERT INTO currencies (code, symbol, name, rate_micro, rate_set_on,
                                   rate_source, active)
           VALUES (?,?,?,?,?,?,1)
           ON CONFLICT(code) DO UPDATE SET
               symbol = excluded.symbol, name = excluded.name,
               rate_micro = excluded.rate_micro, rate_set_on = excluded.rate_set_on,
               rate_source = excluded.rate_source""",
        (code, symbol.strip() or code, name.strip() or code, rate_micro,
         set_on or _today(), source))
    db.execute(
        """INSERT INTO rate_history (code, rate_micro, set_on, source, approved_by,
                                     recorded_at)
           VALUES (?,?,?,?,?,?)""",
        (code, rate_micro, set_on or _today(), source, by, _now()))


def set_active(code: str, active: bool) -> str | None:
    code = (code or "").upper()
    if code == reporting_code():
        return "The reporting currency cannot be switched off"
    if not active and in_use(code):
        return f"{code} is in use, so it cannot be switched off"
    db.execute("UPDATE currencies SET active = ? WHERE code = ?",
               (1 if active else 0, code))
    return None


def delete(code: str) -> str | None:
    code = (code or "").upper()
    if code == reporting_code():
        return "The reporting currency cannot be deleted"
    if in_use(code):
        return f"{code} is in use by existing records, so it cannot be deleted"
    db.execute("DELETE FROM currencies WHERE code = ?", (code,))
    return None


def usage(code: str) -> dict:
    code = (code or "").upper()
    return {
        "assets": db.q1("SELECT COUNT(*) c FROM assets WHERE currency = ?", (code,))["c"],
        "pooled": db.q1("SELECT COUNT(*) c FROM pooled_items WHERE currency = ?", (code,))["c"],
        "subscriptions": db.q1("SELECT COUNT(*) c FROM subscriptions WHERE currency = ?",
                               (code,))["c"],
        "pricing": db.q1("SELECT COUNT(*) c FROM price_groups WHERE currency = ?",
                         (code,))["c"],
    }


def in_use(code: str) -> bool:
    return any(usage(code).values())


def history(code: str, limit: int = 12):
    return db.q("SELECT * FROM rate_history WHERE code = ? ORDER BY id DESC LIMIT ?",
                ((code or "").upper(), limit))


# --- conversion ----------------------------------------------------------

def to_reporting(cents: int, rate_micro: int | None) -> int:
    """Convert an amount to the reporting currency using its frozen rate.

    Integer arithmetic with explicit half-up rounding: floats would drift, and
    round() in Python rounds halves to even, which is not what money does.
    """
    if not cents:
        return 0
    rate = MICRO if rate_micro is None else int(rate_micro)
    return (int(cents) * rate + MICRO // 2) // MICRO


def rate_for(code: str) -> int:
    """Today's rate for a currency, for freezing onto a new record."""
    row = get(code)
    return int(row["rate_micro"]) if row else MICRO


def format_rate(rate_micro: int | None) -> str:
    return f"{(rate_micro or MICRO) / MICRO:.6f}".rstrip("0").rstrip(".")


def parse_rate(text: str) -> int | None:
    """Accept a typed rate such as 0.3357 and store it as micro-units."""
    try:
        value = float(str(text).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(round(value * MICRO))


def symbol(code: str) -> str:
    row = get(code)
    if row and row["symbol"]:
        return row["symbol"]
    return KNOWN_SYMBOLS.get((code or "").upper(), (code or "").upper())


def money(cents: int, code: str | None) -> str:
    """An amount with its own symbol, for showing what was actually paid."""
    return f"{symbol(code or reporting_code())} {db.money(cents)}"


# --- Bank of Israel ------------------------------------------------------

class RateFetchError(Exception):
    """The rate source could not be read."""


def fetch_boi() -> dict:
    """Ask Bank of Israel for today's rates and express them against the
    reporting currency.

    Nothing is written: the result is a proposal for someone to approve, so a
    published rate never changes a stored figure on its own.
    """
    try:
        resp = httpx.get(BOI_URL, timeout=20,
                         headers={"Accept": "application/json"})
    except Exception as exc:
        raise RateFetchError(f"Could not reach Bank of Israel: {exc}") from exc
    if resp.status_code >= 400:
        raise RateFetchError(
            f"Bank of Israel returned {resp.status_code}. Try again shortly.")
    try:
        rows = (resp.json() or {}).get("exchangeRates") or []
    except Exception as exc:
        raise RateFetchError(f"Bank of Israel sent something unreadable: {exc}") from exc
    if not rows:
        raise RateFetchError("Bank of Israel returned no rates.")

    # ILS per one unit of each currency, correcting for the quoted unit size.
    per_ils: dict[str, float] = {"ILS": 1.0}
    as_of = ""
    for row in rows:
        code = (row.get("key") or "").upper()
        try:
            rate = float(row.get("currentExchangeRate"))
            unit = float(row.get("unit") or 1) or 1
        except (TypeError, ValueError):
            continue
        if not code or rate <= 0:
            continue
        per_ils[code] = rate / unit
        as_of = as_of or (row.get("lastUpdate") or "")[:10]

    target = reporting_code()
    if target not in per_ils:
        raise RateFetchError(
            f"Bank of Israel does not quote {target}, so rates cannot be "
            f"expressed against it. Set the reporting currency to one it "
            f"publishes, or enter rates by hand.")

    target_ils = per_ils[target]
    proposals = []
    for code, ils in sorted(per_ils.items()):
        if code == target:
            continue
        rate_micro = int(round((ils / target_ils) * MICRO))
        if rate_micro <= 0:
            continue
        current = get(code)
        proposals.append({
            "code": code,
            "symbol": KNOWN_SYMBOLS.get(code, code),
            "name": KNOWN_NAMES.get(code, code),
            "rate_micro": rate_micro,
            "rate": format_rate(rate_micro),
            "ils_per_unit": f"{ils:.5f}",
            "known": bool(current),
            "current": format_rate(current["rate_micro"]) if current else None,
            "changed": bool(current) and int(current["rate_micro"]) != rate_micro,
        })
    return {"as_of": as_of or _today(), "target": target,
            "source": "boi", "proposals": proposals}


def breakdown() -> list[dict]:
    """What was paid, per currency, with the reporting-currency equivalent.

    Every active currency gets a row, even at zero. A table that quietly omits
    the currencies you deal in is not a picture of the estate - and worse, the
    old version omitted rows whose currency was never recorded, while the
    headline cards counted those at a rate of 1. The two disagreed, and the
    table was the one that looked right.

    So anything with no currency recorded gets its own row here, visibly, and
    the totals add up to what the cards say.
    """
    from . import pooled

    owned = pooled.owned_expr("p")
    sources = {
        "asset": ("""SELECT COALESCE(NULLIF(TRIM(a.currency),''),'') AS code,
                            SUM(a.cost_cents) AS raw,
                            SUM(""" + db.conv("a.cost_cents", "a.rate_micro") + """) AS rep
                     FROM assets a GROUP BY code"""),
        "pooled": ("""SELECT COALESCE(NULLIF(TRIM(p.currency),''),'') AS code,
                             SUM(""" + owned + """ * p.unit_cost_cents) AS raw,
                             SUM(""" + db.conv(owned + " * p.unit_cost_cents", "p.rate_micro") + """) AS rep
                      FROM pooled_items p GROUP BY code"""),
        "monthly": ("""SELECT COALESCE(NULLIF(TRIM(s.currency),''),'') AS code,
                              SUM(s.monthly_cost_cents) AS raw,
                              SUM(""" + db.conv("s.monthly_cost_cents", "s.rate_micro") + """) AS rep
                       FROM subscription_seats ss
                       JOIN subscriptions s ON s.id = ss.subscription_id
                       GROUP BY code"""),
    }

    rows: dict[str, dict] = {}

    def row(code: str) -> dict:
        if code not in rows:
            known = get(code) if code else None
            rows[code] = {
                "code": code, "symbol": symbol(code) if code else "",
                "name": known["name"] if known else "",
                "rate_micro": known["rate_micro"] if known else MICRO,
                "rate_source": known["rate_source"] if known else "none",
                "active": bool(known["active"]) if known else False,
                "unset": not code,
                "asset_raw": 0, "asset_rep": 0, "pooled_raw": 0, "pooled_rep": 0,
                "monthly_raw": 0, "monthly_rep": 0,
            }
        return rows[code]

    for code in [c["code"] for c in listing(active_only=True)]:
        row(code)
    for kind, sql in sources.items():
        for found in db.q(sql):
            here = row(found["code"])
            here[f"{kind}_raw"] += found["raw"] or 0
            here[f"{kind}_rep"] += found["rep"] or 0

    out = []
    for entry in rows.values():
        entry["oneoff_raw"] = entry["asset_raw"] + entry["pooled_raw"]
        entry["oneoff_rep"] = entry["asset_rep"] + entry["pooled_rep"]
        entry["used"] = bool(entry["oneoff_raw"] or entry["monthly_raw"])
        out.append(entry)
    # Currencies actually carrying money first, then the rest alphabetically;
    # anything with no currency recorded last, since it is a data problem
    # rather than a currency.
    out.sort(key=lambda e: (e["unset"], not e["used"], e["code"]))
    return out
