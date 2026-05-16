#!/usr/bin/env bash
# ci/checks/tests.sh – Multi-language test runner.
# Outputs JUnit XML to ci/reports/junit/<lang>.xml.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"
# shellcheck source=ci/lib/junit.sh
source "$ROOT_DIR/ci/lib/junit.sh"
# shellcheck source=ci/lib/affected.sh
source "$ROOT_DIR/ci/lib/affected.sh" 2>/dev/null || true

cd "$ROOT_DIR"

JUNIT_DIR="$ROOT_DIR/ci/reports/junit"
OVERALL_RESULT=$CI_RESULT_PASS

# ---------------------------------------------------------------------------
# Affected test selection
# ---------------------------------------------------------------------------
AFFECTED_TESTS=""
if type ci::affected::get_affected_tests >/dev/null 2>&1 && [ -f "ci/config/affected.yml" ]; then
  _changed_files="$(git diff --cached --name-only 2>/dev/null || true)"
  if [ -z "$_changed_files" ]; then
    ci::log::info "No staged files found; falling back to unstaged changes for ad-hoc test selection."
    _changed_files="$(git diff --name-only 2>/dev/null || true)"
  fi
  if [ -n "$_changed_files" ]; then
    AFFECTED_TESTS="$(while IFS= read -r f; do
      [ -n "$f" ] && ci::affected::get_affected_tests "$f"
    done <<< "$_changed_files" | sort -u || true)"
  fi
fi

_tests_filter_affected() {
  local lang="$1"
  local pattern
  [ -n "$AFFECTED_TESTS" ] || return 0

  while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    case "$lang:$pattern" in
      javascript:*.js|javascript:*.jsx|javascript:*.ts|javascript:*.tsx|javascript:*.mjs|javascript:*.cjs)
        printf '%s\n' "$pattern"
        ;;
      python:*.py)
        printf '%s\n' "$pattern"
        ;;
      go:*_test.go)
        printf '%s\n' "$pattern"
        ;;
      rust:*.rs)
        printf '%s\n' "$pattern"
        ;;
    esac
  done <<< "$AFFECTED_TESTS"
}

_tests_tool_missing() {
  ci::log::info "skipped: ${1} not installed"
}

_tests_record_failure() {
  local lang="$1" msg="$2"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  ci::log::error "${lang} tests failed: ${msg}"
}

# ---------------------------------------------------------------------------
# Per-language test functions
# ---------------------------------------------------------------------------

tests::run_js() {
  if [ ! -f package.json ]; then
    ci::log::info "skipped: no package.json found"
    return 0
  fi
  ci::log::info "Running JavaScript tests..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  local jest_pattern=""
  local js_tests=""
  if [ -n "$AFFECTED_TESTS" ]; then
    js_tests="$(_tests_filter_affected javascript)"
    if [ -z "$js_tests" ]; then
      ci::log::info "skipped: no affected JavaScript tests"
      return 0
    fi
    while IFS= read -r pattern; do
      [ -z "$pattern" ] && continue
      if [ -n "$jest_pattern" ]; then
        jest_pattern="${jest_pattern}|${pattern}"
      else
        jest_pattern="$pattern"
      fi
    done <<< "$js_tests"
    [ -n "$jest_pattern" ] && jest_pattern="--testPathPattern=${jest_pattern}"
  fi
  if ci::common::command_exists jest; then
    if ci::common::command_exists node && node -e "require.resolve('jest-junit')" >/dev/null 2>&1; then
      JEST_JUNIT_OUTPUT_FILE="$JUNIT_DIR/js.xml" \
        jest --ci --reporters=default --reporters=jest-junit ${jest_pattern:+$jest_pattern} || rc=$?
    else
      jest --ci ${jest_pattern:+$jest_pattern} || rc=$?
    fi
  elif ci::common::command_exists npx && npx --no-install vitest --version >/dev/null 2>&1; then
    npx --no-install vitest run --reporter=junit --outputFile="$JUNIT_DIR/js.xml" || rc=$?
  elif ci::common::command_exists vitest; then
    vitest run --reporter=junit --outputFile="$JUNIT_DIR/js.xml" || rc=$?
  else
    if ci::common::command_exists npx; then
      npx --no-install jest --ci ${jest_pattern:+$jest_pattern} 2>&1 || rc=$?
    else
      ci::log::info "skipped: no JS test runner (jest/vitest) found"
      return 0
    fi
  fi
  [ "$rc" -ne 0 ] && _tests_record_failure "JavaScript" "exit code ${rc}"
  return 0
}

tests::run_python() {
  if ! ci::common::command_exists pytest; then
    _tests_tool_missing pytest
    return 0
  fi
  local has_python=0
  if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ] || \
     [ -f requirements.txt ] || [ -d tests ]; then
    has_python=1
  fi
  if [ "$has_python" -eq 0 ]; then
    ci::log::info "skipped: no Python project detected"
    return 0
  fi
  ci::log::info "Running pytest..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  if [ -n "$AFFECTED_TESTS" ]; then
    local python_tests=""
    python_tests="$(_tests_filter_affected python)"
    if [ -z "$python_tests" ]; then
      ci::log::info "skipped: no affected Python tests"
      return 0
    fi
    local pytest_args=()
    while IFS= read -r pattern; do
      [ -n "$pattern" ] && pytest_args+=("$pattern")
    done <<< "$python_tests"
    pytest --junitxml="$JUNIT_DIR/python.xml" "${pytest_args[@]}" || rc=$?
  else
    pytest --junitxml="$JUNIT_DIR/python.xml" || rc=$?
  fi
  [ "$rc" -ne 0 ] && _tests_record_failure "Python" "exit code ${rc}"
  return 0
}

tests::run_go() {
  if ! ci::common::command_exists go; then
    _tests_tool_missing go
    return 0
  fi
  if [ ! -f go.mod ]; then
    ci::log::info "skipped: no go.mod found"
    return 0
  fi
  ci::log::info "Running Go tests..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  if ci::common::command_exists gotestsum; then
    gotestsum --junitfile "$JUNIT_DIR/go.xml" ./... || rc=$?
  else
    go test ./... -v 2>&1 || rc=$?
  fi
  [ "$rc" -ne 0 ] && _tests_record_failure "Go" "exit code ${rc}"
  return 0
}

tests::run_rust() {
  if ! ci::common::command_exists cargo; then
    _tests_tool_missing cargo
    return 0
  fi
  if [ ! -f Cargo.toml ]; then
    ci::log::info "skipped: no Cargo.toml found"
    return 0
  fi
  ci::log::info "Running cargo test..."
  local rc=0
  cargo test 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    _tests_record_failure "Rust" "exit code ${rc}"
  else
    # Emit a minimal pass JUnit for consistency
    mkdir -p "$JUNIT_DIR"
    ci::junit::init "rust" "$JUNIT_DIR/rust.xml"
    ci::junit::add_test "cargo" "cargo test" "0" "pass"
    ci::junit::finish
  fi
  return 0
}

tests::run_shell() {
  if ! ci::common::command_exists bats; then
    _tests_tool_missing bats
    return 0
  fi
  if [ ! -d ci/tests ]; then
    ci::log::info "skipped: no ci/tests directory found"
    return 0
  fi
  ci::log::info "Running bats shell tests..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  bats --formatter junit ci/tests/ > "$JUNIT_DIR/shell.xml" 2> "$JUNIT_DIR/shell.stderr" || rc=$?
  [ "$rc" -ne 0 ] && _tests_record_failure "Shell" "exit code ${rc}"
  return 0
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

_tests_main() {
  ci::log::section "Check: tests"

  local check_id="${CI_GATE_CHECK_ID:-all}"

  case "$check_id" in
    tests-js)     tests::run_js ;;
    tests-python) tests::run_python ;;
    tests-go)     tests::run_go ;;
    tests-rust)   tests::run_rust ;;
    tests-shell)  tests::run_shell ;;
    all|*)
      tests::run_js
      tests::run_python
      tests::run_go
      tests::run_rust
      tests::run_shell
      ;;
  esac

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Tests result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _tests_main "$@"
fi
