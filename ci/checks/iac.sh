#!/usr/bin/env bash
# ci/checks/iac.sh – Infrastructure-as-Code security scanning.
# Runs tflint and checkov when applicable.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

_iac_tool_skip() {
  ci::log::info "skipped: ${1} not installed"
}

_has_tf_files() {
  local count
  count="$(find . -name '*.tf' -not -path './.git/*' 2>/dev/null | wc -l || echo 0)"
  [ "${count:-0}" -gt 0 ]
}

# ---------------------------------------------------------------------------
# tflint
# ---------------------------------------------------------------------------

_iac_run_tflint() {
  if ! _has_tf_files; then
    ci::log::info "No Terraform files found; skipping tflint."
    return 0
  fi
  if ! ci::common::command_exists tflint; then
    _iac_tool_skip tflint
    return 0
  fi
  ci::log::info "Running tflint..."
  local rc=0
  tflint --recursive 2>/dev/null || tflint || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
}

# ---------------------------------------------------------------------------
# checkov
# ---------------------------------------------------------------------------

_iac_run_checkov() {
  if ! ci::common::command_exists checkov; then
    _iac_tool_skip checkov
    return 0
  fi
  ci::log::info "Running checkov..."
  local rc=0
  checkov -d . --quiet 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_iac_main() {
  ci::log::section "Check: iac"

  _iac_run_tflint
  _iac_run_checkov

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "IaC result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _iac_main "$@"
fi
