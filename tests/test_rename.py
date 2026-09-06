"""A renamed UPN is the same person, not a new one.

john.a@remedio.io became john.addeo@remedio.io in Entra. UPN is the key
everything here hangs off, so the sync invented a second person, left the first
holding the laptop and three licences under a name nobody uses, and moved
nothing. Entra's own stable identifier is the object id, which is already
stored, so this is detectable rather than guessable.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, entra, people, pooled
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

OID = "05f3a757-d958-4fe5-bca1-5238bdb39242"

def seed_john(upn):
    db.execute("""INSERT INTO users (upn, display_name, job_title, department,
                                     country, entra_id, source)
                  VALUES (?,'John Addeo','VP of Channels','Business',
                          'United States of America (the)',?,'entra')""", (upn, OID))

def give_john_things(upn):
    aid = db.execute("""INSERT INTO assets (name, category, cost_cents, serial, assigned_upn)
                        VALUES ('MacBook Air 13 M4','Laptop',0,'C17HVWL00H',?)""", (upn,))
    db.execute("""INSERT INTO devices (id, device_name, model, os, serial_number,
                                       primary_upn, asset_id, synced_at)
                  VALUES ('d1',"John's MacBook Air (2)",'Mac17,4','macOS','C17HVWL00H',
                          ?,?,'2026-09-06T00:00:00+00:00')""", (upn, aid))
    for n in range(3):
        sid = db.execute("INSERT INTO subscriptions (name, monthly_cost_cents) VALUES (?,?)",
                         (f"Licence {n}", 1478))
        db.execute("INSERT INTO subscription_seats (subscription_id, upn, assigned_on) "
                   "VALUES (?,?,'2026-08-01')", (sid, upn))
    db.execute("INSERT INTO groups (id, display_name, member_count) VALUES ('g-biz','Business',1)")
    db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-biz',?)", (upn,))
    item = pooled.create("Dell U2723QE", "Monitor", 59900)
    pooled.assign(item, upn, 2)
    return aid, item

seed_john("john.a@remedio.io")
aid, item = give_john_things("john.a@remedio.io")

print("--- the state Edgar found ---")
check("everything is on the old name",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"],
      "john.a@remedio.io")
check("three licences", db.q1("SELECT COUNT(*) c FROM subscription_seats")["c"], 3)

print("\n--- the sync sees the rename by object id and moves everything ---")
entra.fetch_users = lambda: [{
    "id": OID, "userPrincipalName": "john.addeo@remedio.io",
    "displayName": "John Addeo", "jobTitle": "VP of Channels",
    "department": "Business", "accountEnabled": True,
    "country": "United States of America (the)", "usageLocation": "US"}]
r = entra.sync()
check("reported as a rename, not a new person", r["renamed"], 1)
check("nobody was created", r["created"], 0)
check("one person, not two", db.q1("SELECT COUNT(*) c FROM users")["c"], 1)
check("under the new name",
      db.q1("SELECT upn FROM users")["upn"], "john.addeo@remedio.io")
check("the laptop came with him",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"],
      "john.addeo@remedio.io")
check("all three licences",
      db.q1("SELECT COUNT(*) c FROM subscription_seats WHERE upn='john.addeo@remedio.io'")["c"], 3)
check("the monitors", pooled.held_by("john.addeo@remedio.io", "Monitor"), 2)
check("group membership",
      db.q1("SELECT COUNT(*) c FROM group_members WHERE upn='john.addeo@remedio.io'")["c"], 1)
check("and the device is not left pointing at a name nobody uses",
      db.q1("SELECT primary_upn FROM devices WHERE id='d1'")["primary_upn"],
      "john.addeo@remedio.io")
check("so the holder check sees no disagreement",
      len(__import__("app.devices", fromlist=["devices"]).holder_gap()["mismatch"]), 0)

print("\n--- the state Edgar is in NOW: both rows already exist ---")
db.execute("DELETE FROM users"); db.execute("DELETE FROM subscription_seats")
db.execute("DELETE FROM group_members"); db.execute("DELETE FROM pooled_allocations")
seed_john("john.a@remedio.io")
seed_john("john.addeo@remedio.io")
db.execute("UPDATE assets SET assigned_upn='john.a@remedio.io' WHERE id=?", (aid,))
for n in range(3):
    db.execute("INSERT INTO subscription_seats (subscription_id, upn, assigned_on) "
               "SELECT id,'john.a@remedio.io','2026-08-01' FROM subscriptions LIMIT 1 OFFSET ?", (n,))
db.execute("INSERT INTO group_members (group_id, upn) VALUES ('g-biz','john.a@remedio.io')")
pooled.assign(item, "john.a@remedio.io", 2)

check("ITAM shows the pair", [(p["old_upn"], p["new_upn"]) for p in people.renamed()],
      [("john.a@remedio.io", "john.addeo@remedio.io")])
r = entra.sync()
check("the sync folds them together", r["renamed"], 1)
check("one person left", db.q1("SELECT COUNT(*) c FROM users")["c"], 1)
check("holding the laptop",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (aid,))["assigned_upn"],
      "john.addeo@remedio.io")
check("and the licences", db.q1(
    "SELECT COUNT(*) c FROM subscription_seats WHERE upn='john.addeo@remedio.io'")["c"], 3)
check("no pair reported any more", people.renamed(), [])

print("\n--- merging by hand, for somebody recreated as a NEW Entra object ---")
db.execute("INSERT INTO users (upn, display_name, entra_id, source) "
           "VALUES ('old@x.com','Old Account','oid-old','entra')")
db.execute("INSERT INTO users (upn, display_name, entra_id, source) "
           "VALUES ('new@x.com','New Account','oid-new','entra')")
a2 = db.execute("INSERT INTO assets (name,category,cost_cents,assigned_upn) "
                "VALUES ('ThinkPad','Laptop',0,'old@x.com')")
pooled.assign(item, "old@x.com", 1)
pooled.assign(item, "new@x.com", 3)
check("different object ids, so the sync cannot tell", people.renamed(), [])
out = people.merge("old@x.com", "new@x.com")
check("the asset moved",
      db.q1("SELECT assigned_upn FROM assets WHERE id=?", (a2,))["assigned_upn"], "new@x.com")
check("counted units are ADDED, not dropped", pooled.held_by("new@x.com", "Monitor"), 4)
check("the old person is gone",
      db.q1("SELECT COUNT(*) c FROM users WHERE upn='old@x.com'")["c"], 0)
check("and it says what it did", out["moved"]["assets"], 1)

print("\n--- refusals ---")
check("into itself", people.merge("new@x.com", "new@x.com"), "That is the same person")
check("a stranger", people.merge("ghost@x.com", "new@x.com"), "No such person: ghost@x.com")
check("a missing target", people.merge("new@x.com", "ghost@x.com"),
      "No such person: ghost@x.com")
check("nothing given", people.merge("", "new@x.com"), "Both people are needed")

print("\n--- every table that points at a person is covered ---")
tables = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")}
referencing = set()
for t in sorted(tables):
    cols = [c["name"] for c in db.q(f"PRAGMA table_info({t})")]
    if t not in ("users", "devices") and any("upn" in c for c in cols):
        referencing.add(t)
covered = {t for t, _c, _k in people.UPN_TABLES} | {"pooled_allocations"}
check("nothing left behind to strand a holding", sorted(referencing - covered), [])

print("\n--- the two pages that count a person's assets must agree ---")
db.execute("DELETE FROM assets")
db.execute("INSERT INTO assets (name,category,cost_cents,assigned_upn) "
           "VALUES ('MacBook','Laptop',0,'new@x.com')")
from app.main import USER_COSTS                      # noqa: E402
costs = db.q1(USER_COSTS + " WHERE u.upn = ?", ("new@x.com",))
listed = [u for u in people.listing() if u["upn"] == "new@x.com"][0]
check("People page and the Users tab report the same count",
      listed["assets"], costs["asset_count"])

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
