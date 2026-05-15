#!/usr/bin/env bash
# ci/checks/sast.sh – SAST security scanning dispatcher.
# Emits SARIF to ci/reports/sarif/sast.sarif.json.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"
# shellcheck source=ci/lib/sarif.sh
source "$ROOT_DIR/ci/lib/sarif.sh"

cd "$ROOT_DIR"

SARIF_DIR="$ROOT_DIR/ci/reports/sarif"
SARIF_FILE="$SARIF_DIR/sast.sarif.json"
OVERALL_RESULT=$CI_RESULT_PASS
_sast_sarif_initialized=0

_sast_ensure_sarif() {
  if [ "$_sast_sarif_initialized" -eq 0 ]; then
    ci::sarif::init "ci-gate-sast" "1.0.0" "$SARIF_FILE"
    _sast_sarif_initialized=1
  fi
}

_sast_tool_skip() {
  ci::log::info "skipped: ${1} not installed"
}

_sast_record_fail() {
  local tool="$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  _sast_ensure_sarif
  ci::sarif::add_result "${tool}" "${tool} found security issues" "" 1 "error"
}

# ---------------------------------------------------------------------------
# Individual SAST tools
# ---------------------------------------------------------------------------

_sast_run_semgrep() {
  if ! ci::common::command_exists semgrep; then
    _sast_tool_skip semgrep
    return 0
  fi
  ci::log::info "Running semgrep --config=auto..."
  mkdir -p "$SARIF_DIR"
  local rc=0
  semgrep --config=auto --sarif --output="$SARIF_DIR/semgrep.sarif.json" . || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "semgrep"
  fi
}

_sast_run_bandit() {
  local has_python=0
  [ -f pyproject.toml ] || [ -f setup.py ] || [ -f requirements.txt ] && has_python=1
  if [ "$has_python" -eq 0 ]; then
    return 0
  fi
  if ! ci::common::command_exists bandit; then
    _sast_tool_skip bandit
    return 0
  fi
  ci::log::info "Running bandit..."
  mkdir -p "$SARIF_DIR"
  local rc=0
  bandit -r . -f sarif -o "$SARIF_DIR/bandit.sarif.json" \
    --exclude ./node_modules,./.venv,./venv 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "bandit"
  fi
}

_sast_run_gosec() {
  if [ ! -f go.mod ]; then
    return 0
  fi
  if ! ci::common::command_exists gosec; then
    _sast_tool_skip gosec
    return 0
  fi
  ci::log::info "Running gosec..."
  local rc=0
  gosec ./... || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "gosec"
  fi
}

_sast_run_cargo_audit() {
  if [ ! -f Cargo.toml ]; then
    return 0
  fi
  if ! cargo audit --version >/dev/null 2>&1 && ! ci::common::command_exists cargo-audit; then
    _sast_tool_skip "cargo-audit"
    return 0
  fi
  ci::log::info "Running cargo audit..."
  local rc=0
  cargo audit || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "cargo-audit"
  fi
}

_sast_run_npm_audit() {
  if [ ! -f package.json ]; then
    return 0
  fi
  if ! ci::common::command_exists npm; then
    _sast_tool_skip npm
    return 0
  fi
  ci::log::info "Running npm audit --audit-level=high..."
  local rc=0
  npm audit --audit-level=high 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "npm-audit"
  fi
}

_sast_run_pip_audit() {
  local has_python=0
  [ -f requirements.txt ] || [ -f pyproject.toml ] && has_python=1
  if [ "$has_python" -eq 0 ]; then
    return 0
  fi
  if ! ci::common::command_exists pip-audit; then
    _sast_tool_skip pip-audit
    return 0
  fi
  ci::log::info "Running pip-audit..."
  local rc=0
  if [ -f requirements.txt ]; then
    pip-audit -r requirements.txt || rc=$?
  else
    pip-audit . || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "pip-audit"
  fi
}

_sast_run_osv_scanner() {
  if ! ci::common::command_exists osv-scanner; then
    _sast_tool_skip osv-scanner
    return 0
  fi
  ci::log::info "Running osv-scanner..."
  local rc=0
  osv-scanner -r . || rc=$?
  if [ "$rc" -ne 0 ]; then
    _sast_record_fail "osv-scanner"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_sast_main() {
  ci::log::section "Check: sast"

  _sast_run_semgrep
  _sast_run_bandit
  _sast_run_gosec
  _sast_run_cargo_audit
  _sast_run_npm_audit
  _sast_run_pip_audit
  _sast_run_osv_scanner

  if [ "$_sast_sarif_initialized" -eq 1 ]; then
    ci::sarif::finish
    ci::log::info "SARIF report written to ${SARIF_FILE}"
  fi

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "SAST result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _sast_main "$@"
fi
