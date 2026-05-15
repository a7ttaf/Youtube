#!/usr/bin/env bash
# ci/checks/supply-chain.sh – Lockfile integrity and drift detection.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

_sc_warn() {
  ci::log::warn "$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_PASS_WITH_KNOWN_DEBT")"
}

_sc_fail() {
  ci::log::error "$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
}

# ---------------------------------------------------------------------------
# Per-ecosystem checks
# ---------------------------------------------------------------------------

_sc_check_npm() {
  if [ ! -f package.json ]; then
    return 0
  fi
  if [ ! -f package-lock.json ] && [ ! -f yarn.lock ] && [ ! -f pnpm-lock.yaml ]; then
    _sc_warn "package.json found but no lockfile (package-lock.json/yarn.lock/pnpm-lock.yaml)"
    return 0
  fi
  if [ -f package-lock.json ] && ci::common::command_exists npm; then
    ci::log::info "Verifying npm lockfile integrity..."
    local rc=0
    npm ls --json >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      _sc_fail "package-lock.json appears out of sync with package.json (npm ls failed)"
    else
      ci::log::info "npm lockfile OK"
    fi
  fi
}

_sc_check_go() {
  if [ ! -f go.mod ]; then
    return 0
  fi
  if [ ! -f go.sum ]; then
    _sc_warn "go.mod found but go.sum is missing"
    return 0
  fi
  if ! ci::common::command_exists go; then
    ci::log::info "skipped: go not installed"
    return 0
  fi
  ci::log::info "Verifying go.sum integrity..."
  local rc=0
  go mod verify || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sc_fail "go mod verify failed: go.sum may be out of sync with go.mod"
  else
    ci::log::info "go.sum OK"
  fi
}

_sc_check_cargo() {
  if [ ! -f Cargo.toml ]; then
    return 0
  fi
  if [ ! -f Cargo.lock ]; then
    _sc_warn "Cargo.toml found but Cargo.lock is missing"
    return 0
  fi
  if ! ci::common::command_exists cargo; then
    ci::log::info "skipped: cargo not installed"
    return 0
  fi
  ci::log::info "Verifying Cargo.lock integrity..."
  local rc=0
  cargo metadata --locked --format-version 1 >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sc_fail "Cargo.lock appears out of sync with Cargo.toml (cargo metadata --locked failed)"
  else
    ci::log::info "Cargo.lock OK"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_supply_chain_main() {
  ci::log::section "Check: supply-chain"

  _sc_check_npm
  _sc_check_go
  _sc_check_cargo

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Supply-chain result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _supply_chain_main "$@"
fi
