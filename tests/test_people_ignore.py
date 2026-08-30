"""Ignoring people, filtering groups, and finding who holds nothing.

The OData user filter can say "these". It is poor at "these, except the service
accounts", and a filter Graph declines to run comes back as an empty sync rather
than an error - which is exactly how the device filter wasted an afternoon. So
exceptions are matched here instead.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ITAM_ADMIN_PASSWORD"] = "PeopleTest!2345"
os.environ["ITAM_COOKIE_SECURE"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings; warnings.filterwarnings("ignore")
from app import db, devices, people
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

def user(upn, name, dept="", title="", country="Israel"):
    db.execute("""INSERT INTO users (upn, display_name, department, job_title,
                                     country, source)
                  VALUES (?,?,?,?,?,'entra')""", (upn, name, dept, title, country))

user("yael@x.com", "Yael Bar-On", "Design", "Designer")
user("noa@x.com", "Noa Levi", "Engineering", "Engineer")
user("svc-backup@x.com", "Backup Service")
user("svc-scanner@x.com", "Scanner Service")
user("room-tlv@x.com", "TLV Meeting Room", "Facilities")

print("--- the five fields the sync brings across are all matchable ---")
check("every field offered",
      sorted(people.FIELDS), ["country", "department", "display_name", "job_title", "upn"])

print("\n--- service accounts, by UPN prefix ---")
check("adding the rule", people.add_rule("upn", "starts", "svc-"), None)
check("two hidden", people.recompute(), 2)
check("and the rest are untouched",
      sorted(u["upn"] for u in people.listing()),
      ["noa@x.com", "room-tlv@x.com", "yael@x.com"])
check("it says which rule",
      db.q1("SELECT ignored_reason FROM users WHERE upn='svc-backup@x.com'")["ignored_reason"],
      "UPN starts with “svc-”")

print("\n--- and a meeting room, by name ---")
check("by display name", people.add_rule("display_name", "contains", "Meeting Room"), None)
check("three hidden now", people.recompute(), 3)
check("two people left", len(people.listing()), 2)
check("but nobody was deleted", db.q1("SELECT COUNT(*) c FROM users")["c"], 5)
check("and they can still be shown", len(people.listing(show_ignored=True)), 5)

print("\n--- 'is blank' needs no value ---")
check("blank department", people.add_rule("department", "blank", ""), None)
check("still three, the service accounts already matched", people.recompute(), 3)
user("ghost@x.com", "No Department", "", "")
check("a newcomer with no department is caught", people.recompute(), 4)

print("\n--- validation ---")
check("a duplicate", people.add_rule("upn", "starts", "svc-") is not None, True)
check("an unknown field", people.add_rule("shoe_size", "eq", "9") is not None, True)
check("an unknown match", people.add_rule("upn", "rhymes-with", "x") is not None, True)
check("an empty value where one is needed",
      people.add_rule("upn", "contains", "  ") is not None, True)

print("\n--- removing a rule brings them back at once ---")
rule = [r for r in people.rules() if r["field"] == "display_name"][0]
people.delete_rule(rule["id"])
check("the room is back", people.recompute(), 3)
check("listed again", "room-tlv@x.com" in [u["upn"] for u in people.listing()], True)

print("\n--- the People page leaves ignored people out ---")
from fastapi.testclient import TestClient          # noqa: E402
from app import main                               # noqa: E402
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "PeopleTest!2345"},
                follow_redirects=False)
    db.execute("INSERT INTO assets (name, category, cost_cents, assigned_upn) "
               "VALUES ('MacBook Air','Laptop',129900,'yael@x.com')")
    sub = db.execute("INSERT INTO subscriptions (name, monthly_cost_cents) VALUES ('Figma',1500)")
    db.execute("INSERT INTO subscription_seats (subscription_id, upn, assigned_on) "
               "VALUES (?,'noa@x.com','2026-08-30')", (sub,))

    page = client.get("/users").text
    check("a service account is not listed", "svc-backup@x.com" in page, False)
    check("real people are", "yael@x.com" in page, True)

    print("\n--- and can be filtered to who holds nothing ---")
    def upns(query):
        text = client.get("/users" + query).text
        return sorted(u for u in ("yael@x.com", "noa@x.com", "room-tlv@x.com")
                      if u in text)
    check("everybody", upns(""), ["noa@x.com", "room-tlv@x.com", "yael@x.com"])
    check("no assets", upns("?holdings=no-assets"), ["noa@x.com", "room-tlv@x.com"])
    check("no licences", upns("?holdings=no-licences"), ["room-tlv@x.com", "yael@x.com"])
    check("nothing at all", upns("?holdings=none"), ["noa@x.com", "room-tlv@x.com"])

print("\n--- filtering a long group list ---")
now = "2026-08-30T00:00:00+00:00"
for gid, name, desc, rule, kind in [
        ("g-vm", "Virtual Machines", None, '(device.deviceOSType -eq "Windows")', "device"),
        ("g-mac", "Managed Macs", "All the Macs", '(device.deviceOSType -eq "macOS")', "device"),
        ("g-il", "Israel", "Everyone in Israel", '(user.country -eq "Israel")', "user"),
        ("g-cse", "CSE", None, None, "assigned")]:
    db.execute("""INSERT INTO entra_groups (id, display_name, description,
                     membership_rule, looks_like, discovered_at, sync_devices)
                  VALUES (?,?,?,?,?,?,?)""",
               (gid, name, desc, rule, kind, now, 1 if gid == "g-vm" else 0))

rows = db.q("SELECT *, sync_devices AS ticked, NULL AS synced FROM entra_groups "
            "ORDER BY display_name")
def ids(**kw):
    return sorted(r["id"] for r in devices.filter_groups(rows, **kw))

check("plain substring on the name", ids(q="virtual"), ["g-vm"])
check("a wildcard", ids(q="managed*macs"), ["g-mac"])
check("the description is searched too", ids(q="everyone in israel"), ["g-il"])
check("and so is the membership rule, which is the point",
      ids(q="device."), ["g-mac", "g-vm"])
check("case does not matter", ids(q="DEVICE."), ["g-mac", "g-vm"])
check("ticked only", ids(state="ticked"), ["g-vm"])
check("not ticked", ids(state="unticked"), ["g-cse", "g-il", "g-mac"])
check("no members here", ids(state="empty"), ["g-cse", "g-il", "g-mac", "g-vm"])
check("by membership kind", ids(state="device"), ["g-mac", "g-vm"])
check("assigned membership", ids(state="assigned"), ["g-cse"])
check("search and state together", ids(q="device.", state="unticked"), ["g-mac"])
check("a search matching nothing", ids(q="zzz"), [])

print("\n--- saving a filtered page leaves the hidden groups alone ---")
with TestClient(main.app) as client:
    client.post("/login", data={"username": "admin", "password": "PeopleTest!2345"},
                follow_redirects=False)
    # The page showed only g-mac, and nothing was ticked on it.
    client.post("/settings/entra/device-groups/pick",
                data={"shown": ["g-mac"]}, follow_redirects=False)
    check("g-vm is still ticked, though it was not on screen",
          db.q1("SELECT sync_devices FROM entra_groups WHERE id='g-vm'")["sync_devices"], 1)
    check("and g-mac is still unticked",
          db.q1("SELECT sync_devices FROM entra_groups WHERE id='g-mac'")["sync_devices"], 0)
    client.post("/settings/entra/device-groups/pick",
                data={"shown": ["g-vm"], "pick": []}, follow_redirects=False)
    check("unticking a group that WAS on screen works",
          db.q1("SELECT sync_devices FROM entra_groups WHERE id='g-vm'")["sync_devices"], 0)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
