"""No function may give a local the name of a module the file imported.

This has bitten twice. Once a local `settings` shadowed the settings module and
every SSO page 500'd; then a query result named `devices` shadowed the devices
module in the very function that had to call it. Both are invisible until the
line that needs the module runs, which may be an error path nobody exercises.

Cheap to check, so it is checked.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)


def module_names(tree) -> set:
    """Names bound by `from . import x, y` or `import x` at module level."""
    out = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level:
            out |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, ast.Import):
            out |= {(a.asname or a.name).split(".")[0] for a in node.names}
    return out


def locals_of(fn) -> dict:
    """Names the function binds, mapped to the line that binds them."""
    out = {}
    for arg in list(fn.args.args) + list(fn.args.kwonlyargs) + list(fn.args.posonlyargs):
        out.setdefault(arg.arg, fn.lineno)
    for node in ast.walk(fn):
        # A nested def has its own scope; its locals are not this one's.
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.setdefault(node.id, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # A deliberate local import of the same module is fine - that is
            # the documented way app/pooled.py reaches fx without a cycle.
            for alias in node.names:
                out.pop(alias.asname or alias.name.split(".")[0], None)
    return out


clashes = []
scanned = 0
for filename in sorted(os.listdir(APP)):
    if not filename.endswith(".py"):
        continue
    path = os.path.join(APP, filename)
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    modules = module_names(tree)
    if not modules:
        continue
    scanned += 1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for name, line in locals_of(node).items():
            if name in modules:
                clashes.append(
                    f"app/{filename}:{line} {node.name}() binds `{name}`, "
                    f"which is a module this file imports")

check("no local shadows an imported module", clashes, [])
check("something was actually scanned", scanned > 5, True)
print(f"       ({scanned} module(s) scanned)")

print()
print("FAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
