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

# Use the workspace's own tsc and nothing else. A globally resolved binary can
# be a different major version than the one the lockfile pins, and npx in
# particular will walk out of the workspace to find one.
# Returns 127 when the workspace-local tsc is not available.
_tc_js_workspace() {
  local ws="$1"
  cd "$ws" || return 30

  local bin="" candidate
  for candidate in node_modules/.bin/tsc node_modules/.bin/tsc.exe node_modules/.bin/tsc.cmd; do
    if [ -x "$candidate" ]; then
      bin="$candidate"
      break
    fi
  done
  if [ -z "$bin" ]; then
    # Deliberately no PATH fallback, for the same reason the JS test runner has
    # none: a global tsc is not the version this workspace pins. An older one
    # rejects supported syntax and a newer one accepts what the locked
    # toolchain would refuse, so either way the result is not reproducible.
    # Reporting the compiler unavailable is recoverable; a false red or a false
    # green is not.
    return 127
  fi

  "$bin" --noEmit
}

typecheck::run_js() {
  local workspaces
  workspaces="$(ci::common::node_workspaces tsconfig.json)"

  if [ -z "$workspaces" ]; then
    ci::log::info "skipped: no tsconfig.json found"
    return 0
  fi

  local ws rc
  while IFS= read -r ws; do
    [ -n "$ws" ] || continue

    rc=0
    # Subshell so the cd cannot leak into later languages.
    ( _tc_js_workspace "$ws" ) || rc=$?

    if [ "$rc" -eq 127 ]; then
      _tc_tool_missing "workspace-local tsc in ${ws}/node_modules/.bin (a global tsc is deliberately not used)"
      continue
    fi

    if [ "$rc" -ne 0 ]; then
      ci::log::error "tsc --noEmit failed in ${ws} (exit ${rc})"
      OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
    else
      ci::log::info "tsc --noEmit passed in ${ws}"
    fi
  done <<< "$workspaces"

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

if [[ "${BASH_SOURCE[0]}" = "${0}" ]]; then
  _typecheck_main "$@"
fi
