import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db
fails=[]
cases = [("1299.50",129950),("1 299,50",129950),("1,299.99",129999),("1.299,99",129999),
         ("33",3300),("",0),("abc",0),("0.05",5),("2499.00",249900),
         ("1,250",125000),("1.250",125000),("1,234,567",123456700),("1.234.567",123456700),
         ("12,5",1250),("12.5",1250),("1,50",150),(".5",50),("1 000",100000),
         ("-25.00",-2500),("1 234 567,89",123456789),("0",0),("0,00",0)]
for raw,want in cases:
    got = db.to_cents(raw)
    ok = got==want
    print(f"{'PASS' if ok else 'FAIL'}  to_cents({raw!r}) = {got} (want {want})")
    if not ok: fails.append(raw)
for c in [0,5,99,100,1250,125000,129950,986500,123456789]:
    got = db.to_cents(db.money(c))
    ok = got==c
    print(f"{'PASS' if ok else 'FAIL'}  roundtrip {c} -> {db.money(c)!r} -> {got}")
    if not ok: fails.append(f"rt{c}")
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
