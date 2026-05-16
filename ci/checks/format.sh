#!/usr/bin/env bash
# ci/checks/format.sh – Multi-language format check/fix dispatcher.
# Set CI_GATE_FIX=1 to auto-fix instead of checking.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

CI_GATE_FIX="${CI_GATE_FIX:-0}"
OVERALL_RESULT=$CI_RESULT_PASS

_fmt_tool_missing() {
  ci::log::info "skipped: ${1} not installed"
}

# ---------------------------------------------------------------------------
# Per-language format functions
# ---------------------------------------------------------------------------

format::run_shell() {
  if ! ci::common::command_exists shfmt; then
    _fmt_tool_missing shfmt
    return 0
  fi
  ci::log::info "Running shfmt on shell scripts..."
  local rc=0
  if [ "$CI_GATE_FIX" = "1" ]; then
    shfmt -w . || rc=$?
  else
    shfmt -d . || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    ci::log::warn "Shell files need formatting. Run: shfmt -w ."
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

format::run_js() {
  if ! ci::common::command_exists prettier; then
    _fmt_tool_missing prettier
    return 0
  fi
  ci::log::info "Running prettier on JS/TS files..."
  local rc=0
  if [ "$CI_GATE_FIX" = "1" ]; then
    prettier --write . || rc=$?
  else
    prettier --check . || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    ci::log::warn "JS/TS files need formatting. Run: prettier --write ."
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

format::run_python() {
  if ! ci::common::command_exists black && ! ci::common::command_exists ruff; then
    _fmt_tool_missing "black/ruff"
    return 0
  fi
  ci::log::info "Running Python formatter..."
  local rc=0
  if ci::common::command_exists black; then
    if [ "$CI_GATE_FIX" = "1" ]; then
      black . || rc=$?
    else
      black --check . || rc=$?
    fi
  else
    if [ "$CI_GATE_FIX" = "1" ]; then
      ruff format . || rc=$?
    else
      ruff format --check . || rc=$?
    fi
  fi
  if [ "$rc" -ne 0 ]; then
    ci::log::warn "Python files need formatting. Run: black . (or ruff format .)"
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

format::run_go() {
  if ! ci::common::command_exists gofmt; then
    _fmt_tool_missing gofmt
    return 0
  fi
  if [ ! -f go.mod ]; then
    ci::log::info "skipped: no go.mod found"
    return 0
  fi
  ci::log::info "Running gofmt on Go files..."
  local rc=0
  if [ "$CI_GATE_FIX" = "1" ]; then
    gofmt -w . || rc=$?
  else
    local unformatted
    unformatted="$(gofmt -l . 2>/dev/null || true)"
    if [ -n "$unformatted" ]; then
      ci::log::warn "Go files need formatting:"
      printf '%s\n' "$unformatted"
      rc=1
    fi
  fi
  if [ "$rc" -ne 0 ]; then
    ci::log::warn "Go files need formatting. Run: gofmt -w ."
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

format::run_rust() {
  if ! ci::common::command_exists rustfmt; then
    _fmt_tool_missing rustfmt
    return 0
  fi
  if [ ! -f Cargo.toml ]; then
    ci::log::info "skipped: no Cargo.toml found"
    return 0
  fi
  ci::log::info "Running rustfmt on Rust files..."
  local rc=0
  if [ "$CI_GATE_FIX" = "1" ]; then
    cargo fmt || rc=$?
  else
    cargo fmt -- --check || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    ci::log::warn "Rust files need formatting. Run: cargo fmt"
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

_format_main() {
  ci::log::section "Check: format"

  local check_id="${CI_GATE_CHECK_ID:-all}"

  case "$check_id" in
    format-shell)  format::run_shell ;;
    format-js)     format::run_js ;;
    format-python) format::run_python ;;
    format-go)     format::run_go ;;
    format-rust)   format::run_rust ;;
    all|*)
      format::run_shell
      format::run_js
      format::run_python
      format::run_go
      format::run_rust
      ;;
  esac

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Format result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _format_main "$@"
fi
