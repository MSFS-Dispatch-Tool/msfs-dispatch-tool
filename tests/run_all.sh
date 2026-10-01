#!/usr/bin/env bash
# Every check in one go: static checks, unit tests, then the browser suites.
#
#   tests/run_all.sh            # everything
#   tests/run_all.sh --quick    # static checks and unit tests only
#
# The browser suites need DATABASE_URL pointing at a throwaway Postgres
# database and Playwright's Chromium (see tests/README.md); without
# DATABASE_URL they're skipped. TEST_DATABASE_URL turns on the database
# unit tests (it can be the same throwaway database).
set -u
cd "$(dirname "$0")/.."
failed=()

PY="${PYTHON:-python3}"
step() { local label="$1"; shift; echo; echo "== $label"; "$@" || failed+=("$label"); }

step "Python static check" "$PY" -m pyflakes $(git ls-files '*.py')
step "JavaScript syntax" node --check static/js/app.js
step "UI copy style" "$PY" tools/check_ui_copy.py
step "Unit tests" "$PY" -m pytest -q
if [ "${1:-}" != "--quick" ]; then
  if [ -n "${DATABASE_URL:-}" ]; then
    step "Browser suites" "$PY" tests/e2e/run.py
  else
    echo; echo "== Browser suites skipped: DATABASE_URL is not set"
  fi
fi

echo
if [ ${#failed[@]} -eq 0 ]; then echo "All checks passed"; else echo "Failed: ${failed[*]}"; exit 1; fi
