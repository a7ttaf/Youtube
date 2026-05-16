#!/usr/bin/env bash
# ci/checks/typecheck.sh – Multi-language type-check dispatcher.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

_tc_tool_missing() {
  ci::log::info "skipped: ${1} not installed"
}

# ---------------------------------------------------------------------------
# Per-language typecheck functions
# ---------------------------------------------------------------------------

typecheck::run_js() {
  if ! ci::common::command_exists tsc; then
    _tc_tool_missing tsc
    return 0
  fi
  if [ ! -f tsconfig.json ]; then
    ci::log::info "skipped: no tsconfig.json found"
    return 0
  fi
  ci::log::info "Running tsc --noEmit..."
  local rc=0
  tsc --noEmit || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

typecheck::run_python() {
  if ! ci::common::command_exists mypy && ! ci::common::command_exists pyright; then
    _tc_tool_missing "mypy/pyright"
    return 0
  fi
  ci::log::info "Running Python type checker..."
  local rc=0
  if ci::common::command_exists mypy; then
    mypy . --ignore-missing-imports || rc=$?
  else
    pyright . || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

typecheck::run_go() {
  if ! ci::common::command_exists go; then
    _tc_tool_missing go
    return 0
  fi
  if [ ! -f go.mod ]; then
    ci::log::info "skipped: no go.mod found"
    return 0
  fi
  ci::log::info "Running go vet ./..."
  local rc=0
  go vet ./... || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

typecheck::run_rust() {
  if ! ci::common::command_exists cargo; then
    _tc_tool_missing cargo
    return 0
  fi
  if [ ! -f Cargo.toml ]; then
    ci::log::info "skipped: no Cargo.toml found"
    return 0
  fi
  ci::log::info "Running cargo check..."
  local rc=0
  cargo check || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

_typecheck_main() {
  ci::log::section "Check: typecheck"

  local check_id="${CI_GATE_CHECK_ID:-all}"

  case "$check_id" in
    typecheck-js)     typecheck::run_js ;;
    typecheck-python) typecheck::run_python ;;
    typecheck-go)     typecheck::run_go ;;
    typecheck-rust)   typecheck::run_rust ;;
    all|*)
      typecheck::run_js
      typecheck::run_python
      typecheck::run_go
      typecheck::run_rust
      ;;
  esac

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Typecheck result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _typecheck_main "$@"
fi
