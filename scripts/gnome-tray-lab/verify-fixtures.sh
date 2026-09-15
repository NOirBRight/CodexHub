#!/usr/bin/env bash
# Static check of saved actor snapshots. This is not a live regression.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
lab="$root/scripts/gnome-tray-lab"
evidence="${1:-$root/docs/evidence/gnome-tray-2026-09-15}"
fail=0

expect_fail() {
  local file="$1"
  if gjs -m "$lab/check.js" "$file" >/tmp/codexhub-tray-fixture-out.$$ 2>/tmp/codexhub-tray-fixture-err.$$; then
    echo "FAIL expected empty-label sample to fail: $file" >&2
    fail=1
  else
    if grep -q '"kind":"empty-labels"' /tmp/codexhub-tray-fixture-out.$$; then
      echo "ok  empty-label fixture $(basename "$file")"
    else
      echo "FAIL $file did not report empty-labels" >&2
      cat /tmp/codexhub-tray-fixture-out.$$ >&2
      fail=1
    fi
  fi
}

expect_pass() {
  local file="$1"
  if gjs -m "$lab/check.js" "$file"; then
    echo "ok  readable fixture $(basename "$file")"
  else
    echo "FAIL expected readable sample to pass: $file" >&2
    fail=1
  fi
}

expect_fail "$evidence/stock-light-both.json"
expect_fail "$evidence/stock-dark-both.json"
expect_pass "$evidence/patched-light-both.json"
expect_pass "$evidence/patched-dark-both.json"
expect_fail "$evidence/source-f82c24a/empty-labels.json"
expect_pass "$evidence/source-f82c24a/pass-light.json"
rm -f /tmp/codexhub-tray-fixture-out.$$ /tmp/codexhub-tray-fixture-err.$$
exit "$fail"
