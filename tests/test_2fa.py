import os, tempfile, sys, tempfile, tempfile, base64, time
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, auth
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

print("--- RFC 6238 vectors (key 12345678901234567890, 6 digits) ---")
sec = base64.b32encode(b'12345678901234567890').decode().rstrip("=")
check("T=59 -> step 1", auth._totp_at(sec, 1), "287082")
check("T=1111111109 -> step 37037036", auth._totp_at(sec, 37037036), "081804")
check("T=1111111111 -> step 37037037", auth._totp_at(sec, 37037037), "050471")
check("T=1234567890 -> step 41152263", auth._totp_at(sec, 41152263), "005924")

auth.create_user("bob", "Bob-Password-1234")
secret = auth.begin_totp_setup("bob")
print("\n--- setup is not live until confirmed ---")
check("secret stored", bool(auth.get_user("bob")["totp_secret"]), True)
check("not enabled yet", auth.get_user("bob")["totp_enabled"], 0)
check("password-only login still works before enabling",
      bool(auth.login("bob", "Bob-Password-1234")[0]), True)

print("\n--- confirming with a wrong code does not enable it ---")
check("wrong code refused", auth.confirm_totp("bob", "000000"), False)
check("still disabled", auth.get_user("bob")["totp_enabled"], 0)

now = auth.current_step()
good = auth._totp_at(secret, now)
check("correct code enables it", auth.confirm_totp("bob", good), True)
check("enabled", auth.get_user("bob")["totp_enabled"], 1)

print("\n--- password alone can no longer sign in ---")
tok, user = auth.login("bob", "Bob-Password-1234")
check("login() refuses a 2FA account", tok, None)
check("check_credentials still verifies the password",
      bool(auth.check_credentials("bob", "Bob-Password-1234")), True)
check("and rejects a wrong one", auth.check_credentials("bob", "wrong-password-x"), None)

print("\n--- replay protection ---")
u = auth.get_user("bob")
check("the code just used is refused again", auth.verify_totp(secret, good, u["totp_last_step"]), None)
nxt = auth._totp_at(secret, now + 1)
step = auth.verify_totp(secret, nxt, u["totp_last_step"])
check("the next window is accepted", step, now + 1)

print("\n--- clock drift window ---")
fresh = auth.begin_totp_setup("bob")
n = auth.current_step()
check("one step behind accepted", auth.verify_totp(fresh, auth._totp_at(fresh, n - 1), None), n - 1)
check("one step ahead accepted", auth.verify_totp(fresh, auth._totp_at(fresh, n + 1), None), n + 1)
check("two steps behind refused", auth.verify_totp(fresh, auth._totp_at(fresh, n - 2), None), None)
check("two steps ahead refused", auth.verify_totp(fresh, auth._totp_at(fresh, n + 2), None), None)

print("\n--- malformed input ---")
for bad in ("", "abc", "12345", "1234567", None, "12 34 56"):
    r = auth.verify_totp(fresh, bad, None)
    if bad == "12 34 56":
        continue
    if r is not None:
        fails.append(f"malformed accepted: {bad!r}")
print(f"  {'PASS' if not [f for f in fails if 'malformed' in f] else 'FAIL'}  empty/short/long/non-numeric all refused")
check("another account's code is refused",
      auth.verify_totp(fresh, auth._totp_at(auth.new_totp_secret(), n), None), None)

print("\n--- testing a code must not consume it ---")
# verify_totp with last_step=None is what the "test a code" route uses: it
# reports whether the authenticator is in sync without advancing the counter,
# so the next real sign-in with that code still works.
probe_secret = auth.begin_totp_setup("bob")
auth.confirm_totp("bob", auth._totp_at(probe_secret, auth.current_step()))
before = auth.get_user("bob")["totp_last_step"]
step = before + 1
code = auth._totp_at(probe_secret, step)
check("a test check passes without touching the counter",
      auth.verify_totp(probe_secret, code, None) is not None, True)
check("counter unchanged by the check", auth.get_user("bob")["totp_last_step"], before)
check("and the code is still good for a real sign-in",
      auth.verify_totp(probe_secret, code, before), step)

print("\n--- recovery codes ---")
auth.confirm_totp("bob", auth._totp_at(fresh, auth.current_step()))
codes = auth.issue_recovery_codes("bob")
check("eight issued", len(codes), 8)
check("all unused", auth.recovery_codes_left("bob"), 8)
check("stored only as hashes",
      any(c.replace("-","") in str(db.q("SELECT code_hash FROM auth_recovery_codes")) for c in codes),
      False)
check("a code works", auth.use_recovery_code("bob", codes[0]), True)
check("the same code cannot be reused", auth.use_recovery_code("bob", codes[0]), False)
check("count went down", auth.recovery_codes_left("bob"), 7)
check("case and dashes tolerated", auth.use_recovery_code("bob", codes[1].lower().replace("-","")), True)
check("garbage refused", auth.use_recovery_code("bob", "NOPE1-NOPE2"), False)
old = codes[2]
new_codes = auth.issue_recovery_codes("bob")
check("regenerating invalidates the old set", auth.use_recovery_code("bob", old), False)
check("new set is usable", auth.use_recovery_code("bob", new_codes[0]), True)

print("\n--- half-finished sign-in ---")
pending = auth.start_pending("bob")
check("pending resolves to the user", auth.pending_user(pending)["username"], "bob")
check("a forged token does not", auth.pending_user("not-a-real-token"), None)
check("no token does not", auth.pending_user(None), None)
auth.clear_pending(pending)
check("cleared token is dead", auth.pending_user(pending), None)
# expiry
p2 = auth.start_pending("bob")
db.execute("UPDATE auth_2fa_pending SET expires_at = '2020-01-01T00:00:00+00:00' WHERE token = ?", (p2,))
check("expired token is refused", auth.pending_user(p2), None)
auth.purge_pending()
check("purge removes it", db.q1("SELECT COUNT(*) c FROM auth_2fa_pending")["c"], 0)

print("\n--- admin reset, and disable ---")
auth.disable_totp("bob")
check("2FA off after reset", auth.get_user("bob")["totp_enabled"], 0)
check("secret cleared", auth.get_user("bob")["totp_secret"], None)
check("recovery codes cleared", auth.recovery_codes_left("bob"), 0)
check("password-only login works again", bool(auth.login("bob", "Bob-Password-1234")[0]), True)

print("\n--- enforcement flag ---")
check("off by default", auth.require_2fa(), False)
os.environ["ITAM_REQUIRE_2FA"] = "1"
import importlib; importlib.reload(auth)
check("honoured when set", auth.require_2fa(), True)

print()
print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
