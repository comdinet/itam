"""OData $filter versus an Intune dynamic-group membership rule.

They look alike and are not the same language, which is how
`device.deviceModel -ne "Virtual Machine"` ends up in a $filter box and Graph
answers "Syntax error at position 20" and stops. Naming the mistake is worth
more than the position.
"""
import os, sys, tempfile
os.environ["ITAM_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db, entra, settings
db.init_db()

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

print("--- the filter Edgar actually typed ---")
v = entra.check_filter('device.deviceModel -ne "Virtual Machine"')
check("recognised as a dynamic-group rule",
      v["dialect"], "an Intune dynamic-group membership rule")
check("translated to OData", v["suggestion"], "model ne 'Virtual Machine'")
check("all three tells are named", len(v["why"]), 3)

print("\n--- and the -eq form, in brackets, that he tried next ---")
v = entra.check_filter('(device.deviceModel -eq "Virtual Machine")')
check("still recognised", v["dialect"] is not None, True)
check("still translated", v["suggestion"], "model eq 'Virtual Machine'")

print("\n--- property names are mapped, not passed through ---")
for rule, odata in [
        ('device.deviceOSType -eq "Windows"', "operatingSystem eq 'Windows'"),
        ('device.deviceManufacturer -eq "VMware, Inc."', "manufacturer eq 'VMware, Inc.'"),
        ('device.displayName -startsWith "BUILD-"', "startsWith(deviceName, 'BUILD-')"),
        ('device.deviceOSVersion -contains "15."', "contains(osVersion, '15.')")]:
    check(rule, entra.check_filter(rule)["suggestion"], odata)

print("\n--- a property with no mapping keeps its own name, lower-cased ---")
check("user.department", entra.check_filter('user.department -eq "IT"')["suggestion"],
      "department eq 'IT'")

print("\n--- a quote in the value is escaped, not left to break the filter ---")
check("doubled up", entra.check_filter("""device.deviceModel -eq "O'Brien" """.strip())["suggestion"],
      "model eq 'O''Brien'")

print("\n--- real OData is left alone ---")
for good in ["accountEnabled eq true and userType eq 'Member'",
             "model ne 'Virtual Machine'",
             "startsWith(displayName, 'ITAM-')",
             "operatingSystem eq 'macOS'",
             ""]:
    check(f"not flagged: {good!r}", entra.check_filter(good)["dialect"], None)

print("\n--- each tell is caught on its own ---")
check("just the -eq operator",
      entra.check_filter("deviceModel -eq 'x'")["dialect"] is not None, True)
check("just the device. prefix",
      entra.check_filter("device.model eq 'x'")["dialect"] is not None, True)
check("just double quotes",
      entra.check_filter('model eq "x"')["dialect"] is not None, True)

print("\n--- try_filter refuses the wrong dialect without calling Graph ---")
settings.set_value("INTUNE_DEVICE_FILTER", 'device.deviceModel -ne "Virtual Machine"', "test")
r = entra.try_filter("device")
check("rejected", r["ok"], False)
check("and says why, in words", "not OData" in r["detail"], True)
check("without needing credentials", entra.is_configured(), False)

settings.set_value("INTUNE_DEVICE_FILTER", "", "test")
r = entra.try_filter("device")
check("an empty filter is fine", r["ok"], True)
check("and says everything syncs", "everything is synced" in r["detail"].lower(), True)

check("an unknown kind is refused", entra.try_filter("nonsense")["ok"], False)

print("\n--- the sync error explains itself ---")
class FakeResponse:
    status_code = 400
    def json(self):
        return {"error": {"code": "BadRequest",
                          "message": "Invalid filter clause: Syntax error at "
                                     "position 20 in the device filter."}}

settings.set_value("INTUNE_DEVICE_FILTER", 'device.deviceModel -ne "Virtual Machine"', "test")
msg = entra._explain(FakeResponse(), "/deviceManagement/managedDevices")
check("names the dialect", "dynamic-group membership rule" in msg, True)
check("and hands over the OData form", "model ne 'Virtual Machine'" in msg, True)

settings.set_value("INTUNE_DEVICE_FILTER", "model ne 'Virtual Machine'", "test")
msg = entra._explain(FakeResponse(), "/deviceManagement/managedDevices")
check("with a valid filter it does not cry wolf",
      "dynamic-group" in msg, False)
check("but still points at the filters", "OData filters" in msg, True)

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
