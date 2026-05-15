#!/usr/bin/env bash
# ci/checks/license.sh – License compatibility checking.
# Detects disallowed SPDX licenses in npm and pip dependencies.
# Configure disallowed licenses via CI_GATE_DISALLOWED_LICENSES (space-separated SPDX IDs).
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

# Default disallowed licenses (space-separated SPDX IDs)
CI_GATE_DISALLOWED_LICENSES="${CI_GATE_DISALLOWED_LICENSES:-GPL-2.0 GPL-3.0 AGPL-3.0 AGPL-3.0-only AGPL-3.0-or-later GPL-2.0-only GPL-2.0-or-later GPL-3.0-only GPL-3.0-or-later}"

OVERALL_RESULT=$CI_RESULT_PASS
_license_violations=0

_license_violation() {
  ci::log::warn "$1"
  _license_violations=$((_license_violations + 1))
}

_is_disallowed_license() {
  local lic="$1"
  for disallowed in $CI_GATE_DISALLOWED_LICENSES; do
    if [ "$lic" = "$disallowed" ]; then
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# npm license check
# ---------------------------------------------------------------------------

_license_check_npm() {
  if [ ! -f package.json ]; then
    return 0
  fi
  ci::log::info "Checking npm dependency licenses..."

  if ci::common::command_exists license-checker; then
    local rc=0
    # Build --failOn argument from disallowed list
    local fail_on_arg=""
    for lic in $CI_GATE_DISALLOWED_LICENSES; do
      if [ -z "$fail_on_arg" ]; then
        fail_on_arg="$lic"
      else
        fail_on_arg="${fail_on_arg};${lic}"
      fi
    done
    license-checker --failOn "$fail_on_arg" 2>/dev/null || rc=$?
    if [ "$rc" -ne 0 ]; then
      _license_violation "license-checker found disallowed licenses"
    else
      ci::log::info "npm licenses OK (via license-checker)"
    fi
    return 0
  fi

  # Fallback: scan node_modules/*/package.json for license fields
  if [ ! -d node_modules ]; then
    ci::log::info "skipped: node_modules not present (run npm install first)"
    return 0
  fi
  ci::log::info "Scanning node_modules for disallowed licenses (fallback)..."
  while IFS= read -r pkg_json; do
    local lic=""
    if ci::common::command_exists python3; then
      lic="$(python3 -c "import json,sys; d=json.load(open('${pkg_json}')); print(d.get('license',''))" 2>/dev/null || true)"
    elif ci::common::command_exists jq; then
      lic="$(jq -r '.license // ""' "$pkg_json" 2>/dev/null || true)"
    fi
    [ -z "$lic" ] && continue
    if _is_disallowed_license "$lic"; then
      local pkg_name
      pkg_name="$(basename "$(dirname "$pkg_json")")"
      _license_violation "Disallowed license '${lic}' in npm package: ${pkg_name}"
    fi
  done < <(find node_modules -maxdepth 2 -name 'package.json' 2>/dev/null || true)
}

# ---------------------------------------------------------------------------
# pip license check
# ---------------------------------------------------------------------------

_license_check_pip() {
  local has_python=0
  [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f setup.py ] && has_python=1
  if [ "$has_python" -eq 0 ]; then
    return 0
  fi
  ci::log::info "Checking Python dependency licenses..."

  if ! ci::common::command_exists pip-licenses; then
    ci::log::info "skipped: pip-licenses not installed"
    return 0
  fi
  local output rc=0
  output="$(pip-licenses --format=csv 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    ci::log::info "pip-licenses failed; skipping"
    return 0
  fi
  while IFS=',' read -r name _version lic _rest; do
    # Strip surrounding quotes
    lic="${lic//\"/}"
    [ -z "$lic" ] && continue
    if _is_disallowed_license "$lic"; then
      name="${name//\"/}"
      _license_violation "Disallowed license '${lic}' in Python package: ${name}"
    fi
  done < <(printf '%s\n' "$output" | tail -n +2)
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_license_main() {
  ci::log::section "Check: license"

  _license_check_npm
  _license_check_pip

  if [ "$_license_violations" -gt 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "License result: ${result_name} (${_license_violations} violation(s))"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _license_main "$@"
fi
