#!/usr/bin/env bash
# ci/checks/lint.sh – Multi-language lint dispatcher.
# Dispatches to per-language lint functions based on CI_GATE_CHECK_ID or
# detected changed files. Can be sourced or executed directly.
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

SARIF_FILE="$ROOT_DIR/ci/reports/sarif/lint.sarif.json"
OVERALL_RESULT=$CI_RESULT_PASS

_lint_sarif_initialized=0

_lint_ensure_sarif() {
  if [ "$_lint_sarif_initialized" -eq 0 ]; then
    ci::sarif::init "ci-gate-lint" "1.0.0" "$SARIF_FILE"
    _lint_sarif_initialized=1
  fi
}

_lint_tool_missing() {
  local tool="$1"
  ci::log::info "skipped: ${tool} not installed"
}

_lint_record_finding() {
  local rule_id="$1" msg="$2" file="${3:-}" line="${4:-1}"
  _lint_ensure_sarif
  ci::sarif::add_result "$rule_id" "$msg" "$file" "$line" "error"
}

# ---------------------------------------------------------------------------
# Per-language lint functions
# ---------------------------------------------------------------------------

lint::detect() {
  ci::common::command_exists shellcheck || \
  ci::common::command_exists eslint || \
  ci::common::command_exists ruff || \
  ci::common::command_exists golangci-lint || \
  ci::common::command_exists hadolint || \
  ci::common::command_exists yamllint || \
  ci::common::command_exists markdownlint || \
  ci::common::command_exists actionlint
}

lint::run_shell() {
  if ! ci::common::command_exists shellcheck; then
    _lint_tool_missing shellcheck
    return 0
  fi
  ci::log::info "Running shellcheck on shell scripts..."
  local files=()
  if [ -n "${CI_GATE_CHANGED_FILES:-}" ]; then
    for f in $CI_GATE_CHANGED_FILES; do
      [[ "$f" == *.sh ]] && [ -f "$f" ] && files+=("$f")
    done
  else
    while IFS= read -r f; do
      files+=("$f")
    done < <(find . -name '*.sh' \
      -not -path './node_modules/*' \
      -not -path './.venv/*' \
      -not -path './vendor/*' \
      -not -path './.git/*' 2>/dev/null || true)
  fi
  if [ "${#files[@]}" -eq 0 ]; then
    ci::log::info "No shell files found to lint."
    return 0
  fi
  local rc=0
  shellcheck "${files[@]}" || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "shellcheck" "shellcheck reported issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_js() {
  if ! ci::common::command_exists eslint; then
    _lint_tool_missing eslint
    return 0
  fi
  ci::log::info "Running eslint on JS/TS files..."
  local args=()
  if [ -n "${CI_GATE_CHANGED_FILES:-}" ]; then
    for f in $CI_GATE_CHANGED_FILES; do
      case "$f" in *.js|*.ts|*.tsx|*.jsx) [ -f "$f" ] && args+=("$f") ;; esac
    done
    if [ "${#args[@]}" -eq 0 ]; then
      ci::log::info "No JS/TS changed files to lint."
      return 0
    fi
  else
    args+=(--ext ".js,.ts,.tsx,.jsx" .)
  fi
  local rc=0
  eslint "${args[@]}" || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "eslint" "eslint reported issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_python() {
  if ! ci::common::command_exists ruff && ! ci::common::command_exists flake8; then
    _lint_tool_missing "ruff/flake8"
    return 0
  fi
  ci::log::info "Running Python linter..."
  local rc=0
  if ci::common::command_exists ruff; then
    if [ -n "${CI_GATE_CHANGED_FILES:-}" ]; then
      local pyfiles=()
      for f in $CI_GATE_CHANGED_FILES; do
        [[ "$f" == *.py ]] && [ -f "$f" ] && pyfiles+=("$f")
      done
      if [ "${#pyfiles[@]}" -eq 0 ]; then
        ci::log::info "No Python changed files to lint."
        return 0
      fi
      ruff check "${pyfiles[@]}" || rc=$?
    else
      ruff check . || rc=$?
    fi
  else
    flake8 . || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "ruff" "Python linter reported issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_go() {
  if ! ci::common::command_exists golangci-lint; then
    _lint_tool_missing golangci-lint
    return 0
  fi
  if [ ! -f go.mod ]; then
    ci::log::info "No go.mod found; skipping golangci-lint."
    return 0
  fi
  ci::log::info "Running golangci-lint..."
  local rc=0
  golangci-lint run || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "golangci-lint" "golangci-lint reported issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_rust() {
  if ! ci::common::command_exists cargo; then
    _lint_tool_missing cargo
    return 0
  fi
  if [ ! -f Cargo.toml ]; then
    ci::log::info "No Cargo.toml found; skipping clippy."
    return 0
  fi
  ci::log::info "Running cargo clippy..."
  local rc=0
  cargo clippy -- -D warnings || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "clippy" "cargo clippy reported issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_docker() {
  if ! ci::common::command_exists hadolint; then
    _lint_tool_missing hadolint
    return 0
  fi
  local dockerfiles=()
  while IFS= read -r f; do
    dockerfiles+=("$f")
  done < <(find . -name 'Dockerfile*' \
    -not -path './node_modules/*' \
    -not -path './.git/*' 2>/dev/null || true)
  if [ "${#dockerfiles[@]}" -eq 0 ]; then
    ci::log::info "No Dockerfiles found."
    return 0
  fi
  ci::log::info "Running hadolint on Dockerfiles..."
  local rc=0
  hadolint "${dockerfiles[@]}" || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "hadolint" "hadolint reported Dockerfile issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_yaml() {
  if ! ci::common::command_exists yamllint; then
    _lint_tool_missing yamllint
    return 0
  fi
  ci::log::info "Running yamllint..."
  local rc=0
  yamllint . || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "yamllint" "yamllint reported YAML issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_markdown() {
  if ! ci::common::command_exists markdownlint && ! ci::common::command_exists markdownlint-cli2; then
    _lint_tool_missing markdownlint
    return 0
  fi
  ci::log::info "Running markdownlint..."
  local tool="markdownlint"
  ci::common::command_exists markdownlint-cli2 && tool="markdownlint-cli2"
  local rc=0
  if [ "$tool" = "markdownlint-cli2" ]; then
    markdownlint-cli2 "**/*.md" || rc=$?
  else
    markdownlint "**/*.md" || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "markdownlint" "markdownlint reported issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

lint::run_actions() {
  if ! ci::common::command_exists actionlint; then
    _lint_tool_missing actionlint
    return 0
  fi
  if [ ! -d .github/workflows ]; then
    ci::log::info "No .github/workflows directory; skipping actionlint."
    return 0
  fi
  ci::log::info "Running actionlint on GitHub Actions workflows..."
  local rc=0
  actionlint .github/workflows/*.yml 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    _lint_record_finding "actionlint" "actionlint reported workflow issues" "" 1
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

_lint_main() {
  ci::log::section "Check: lint"

  local check_id="${CI_GATE_CHECK_ID:-all}"

  case "$check_id" in
    lint-shell)    lint::run_shell ;;
    lint-js)       lint::run_js ;;
    lint-python)   lint::run_python ;;
    lint-go)       lint::run_go ;;
    lint-rust)     lint::run_rust ;;
    lint-docker)   lint::run_docker ;;
    lint-yaml)     lint::run_yaml ;;
    lint-markdown) lint::run_markdown ;;
    lint-actions)  lint::run_actions ;;
    all|*)
      lint::run_shell
      lint::run_js
      lint::run_python
      lint::run_go
      lint::run_rust
      lint::run_docker
      lint::run_yaml
      lint::run_markdown
      lint::run_actions
      ;;
  esac

  if [ "$_lint_sarif_initialized" -eq 1 ]; then
    ci::sarif::finish
    ci::log::info "SARIF report written to ${SARIF_FILE}"
  fi

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Lint result: ${result_name}"
  exit "$OVERALL_RESULT"
}

# Run main only when executed directly (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _lint_main "$@"
fi
