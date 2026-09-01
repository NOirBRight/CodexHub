#!/usr/bin/env bash
# One-shot Linux local verification: Python core + Rust + physical GUI input.
# Mirrors the Linux half of docs/agents/verification-policy.md.
# Windows cmd/PowerShell launcher and release-script tests skip on this host;
# they remain part of the Windows core suite.
#
# Each leg always runs. A failing leg is recorded and later legs still execute;
# the script exits non-zero if any leg failed.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ "${1:-}" == "--list" && "$#" -eq 1 ]]; then
  printf '%s\n' \
    "Python core (excluding real-client E2E)" \
    "Python test partition completeness" \
    "Rust tests" \
    "Rust clippy" \
    "Linux physical pointer input E2E (90s watchdog; requires timeout, dbus-run-session, xvfb-run, xwininfo, xprop)"
  exit 0
fi
if [[ "$#" -ne 0 ]]; then
  echo "usage: $0 [--list]" >&2
  exit 2
fi

python_launcher="$repo_root/scripts/codexhub-python.sh"
if [[ ! -x "$python_launcher" ]]; then
  echo "error: missing $python_launcher" >&2
  exit 1
fi

failures=0

run_leg() {
  local name="$1"
  shift
  echo "==> $name"
  if "$@"; then
    echo "ok  $name"
  else
    local status=$?
    echo "FAIL $name (exit ${status})"
    failures=$((failures + 1))
  fi
}

run_leg "Python core (excluding real-client E2E)" \
  "$python_launcher" -m pytest -q --ignore=tests/test_real_client_e2e.py

run_leg "Python test partition completeness" \
  "$python_launcher" scripts/ci/check_python_test_partitions.py

run_leg "Rust tests" bash -c 'cd "$1/src-tauri" && cargo test --locked -- --test-threads=1' _ "$repo_root"

run_leg "Rust clippy" bash -c 'cd "$1/src-tauri" && cargo clippy --locked --all-targets -- -D warnings' _ "$repo_root"

run_linux_input_e2e() {
  local dependency
  for dependency in timeout dbus-run-session xvfb-run xwininfo xprop; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
      echo "error: $dependency is required for the Linux pointer-input E2E" >&2
      return 1
    fi
  done
  (cd "$repo_root/frontend" && npm run build) || return
  (cd "$repo_root/src-tauri" && cargo build --locked --features custom-protocol) || return
  timeout --signal=TERM --kill-after=10s 90s \
    dbus-run-session -- xvfb-run -a -s '-screen 0 1280x800x24' \
    "$python_launcher" "$repo_root/scripts/e2e_linux_window_input.py" \
    --bin "$repo_root/src-tauri/target/debug/codexhub"
}

run_leg "Linux physical pointer input E2E" run_linux_input_e2e

echo "==> summary: ${failures} leg(s) failed"
if [[ "$failures" -eq 0 ]]; then
  echo "==> Linux local verification passed"
  exit 0
fi
exit 1
