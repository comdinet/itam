"""Every template must compile, and every name it uses must be supplied.

Renaming a context variable is silent: Jinja prints undefined as an empty
string, so a page keeps rendering with a blank where a number used to be, and
nothing fails until somebody notices the figure is missing. This has bitten
this codebase twice - once when a settings refactor left three pages reading
names that no longer existed. So: parse main.py for what each render() call
passes, parse each template for what it reads, and compare.
"""
import ast, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "app", "templates")

import jinja2
from jinja2 import meta, nodes

fails = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok: fails.append(label)

env = jinja2.Environment(loader=jinja2.FileSystemLoader(TPL))
env.globals["categories"] = lambda: []
env.filters["money"] = str

# --- 1. everything compiles -------------------------------------------------
names = sorted(f for f in os.listdir(TPL) if f.endswith(".html"))
broken = []
sources = {}
for name in names:
    src = open(os.path.join(TPL, name), encoding="utf-8").read()
    sources[name] = src
    try:
        env.parse(src, filename=name)
    except jinja2.TemplateSyntaxError as e:
        broken.append(f"{name}:{e.lineno} {e.message}")
check("every template parses", broken, [])

# --- 2. what each template reads, including from its parents ---------------
def parents(name):
    """The chain a template inherits or imports names through."""
    out, src = [], sources[name]
    ast_ = env.parse(src, filename=name)
    for ref in meta.find_referenced_templates(ast_):
        if ref and ref in sources:
            out.append(ref)
            out.extend(parents(ref))
    return out

def bound(tree):
    """Names the template gives itself: {% set %}, macros, imported macros.

    find_undeclared_variables is deliberately conservative - a {% set %} inside
    an {% if %} might not run, so it reports the name anyway. For this check
    that is noise: the template supplies it or it does not, and a route was
    never going to.
    """
    out = set()
    for node in tree.find_all((nodes.Assign, nodes.AssignBlock)):
        target = node.target
        if isinstance(target, nodes.Name):
            out.add(target.name)
        elif isinstance(target, (nodes.Tuple, nodes.List)):
            out |= {n.name for n in target.items if isinstance(n, nodes.Name)}
    for node in tree.find_all(nodes.Macro):
        out.add(node.name)
    for node in tree.find_all(nodes.FromImport):
        out |= {n if isinstance(n, str) else n[1] for n in node.names}
    for node in tree.find_all(nodes.Import):
        out.add(node.target)
    return out


def reads(name):
    seen, given = set(), set()
    for tpl in [name] + parents(name):
        tree = env.parse(sources[tpl], filename=tpl)
        seen |= meta.find_undeclared_variables(tree)
        given |= bound(tree)
    return seen - given

# --- 3. what main.py passes to each template -------------------------------
# render(request, "x.html", a=1, b=2) and the defaults render() itself sets.
# render() sets these on every context; `request` is put there by
# Starlette itself, since TemplateResponse takes it as its first argument.
RENDER_DEFAULTS = {"flash", "currency", "me", "request"}
tree = ast.parse(open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read())
passed: dict[str, set] = {}
starred = set()
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "render"):
        continue
    if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
        continue
    tpl = node.args[1].value
    given = set()
    for kw in node.keywords:
        if kw.arg is None:          # render(..., **ctx) - contents unknowable
            starred.add(tpl)
        else:
            given.add(kw.arg)
    passed.setdefault(tpl, set())
    # A template rendered from several routes must be satisfied by each of
    # them, so the guarantee is the intersection, not the union.
    passed[tpl] = given if tpl not in passed or not passed[tpl] else passed[tpl] & given

# Whatever main.py registers as a Jinja global is available everywhere, so
# read that off the source rather than keeping a second list in step by hand.
for node in ast.walk(tree):
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if (isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and ast.unparse(target.value).endswith("env.globals")):
            env.globals.setdefault(target.slice.value, None)

known = set(env.globals) | RENDER_DEFAULTS
missing = []
for tpl, given in sorted(passed.items()):
    if tpl in starred or tpl not in sources:
        continue
    for var in sorted(reads(tpl) - given - known):
        missing.append(f"{tpl} reads {{{{ {var} }}}}, which no route passes")
check("no template reads a name its route does not pass", missing, [])

check("every template main.py renders exists",
      sorted(t for t in passed if t not in sources), [])

print("\nFAILURES:", ", ".join(fails) if fails else "none")
sys.exit(1 if fails else 0)
