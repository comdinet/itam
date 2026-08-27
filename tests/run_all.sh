#!/bin/sh
# Run every test suite. Needs the virtualenv (see README) for the unit suites;
# the SAML suite needs the container, because it uses xmlsec to sign assertions.
#
#   ./tests/run_all.sh
cd "$(dirname "$0")/.." || exit 1
PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3

fail=0
for t in tests/test_*.py; do
    out="$("$PY" "$t" 2>&1)"
    last="$(printf '%s' "$out" | tail -1)"
    case "$last" in
        *"FAILURES: none"*) printf '  PASS  %s\n' "$t" ;;
        *) printf '  FAIL  %s\n        %s\n' "$t" "$last"; fail=1 ;;
    esac
done

echo
echo "The SAML suite runs inside the container (it signs real assertions):"
echo "  docker compose cp tests/saml_attacks.py itam:/tmp/ &&
  docker compose exec -w /srv/itam itam python /tmp/saml_attacks.py"
exit $fail
