#!/usr/bin/env bash
# ci/checks/container.sh – Container security checks.
# Runs hadolint for Dockerfile lint and trivy for vulnerability scanning.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

_container_tool_skip() {
  ci::log::info "skipped: ${1} not installed"
}

# ---------------------------------------------------------------------------
# hadolint Dockerfile lint
# ---------------------------------------------------------------------------

_container_run_hadolint() {
  local dockerfiles=()
  while IFS= read -r f; do
    dockerfiles+=("$f")
  done < <(find . -name 'Dockerfile*' \
    -not -path './node_modules/*' \
    -not -path './.git/*' 2>/dev/null || true)

  if [ "${#dockerfiles[@]}" -eq 0 ]; then
    ci::log::info "No Dockerfiles found; skipping hadolint."
    return 0
  fi
  if ! ci::common::command_exists hadolint; then
    _container_tool_skip hadolint
    return 0
  fi
  ci::log::info "Running hadolint on Dockerfiles..."
  local rc=0
  hadolint "${dockerfiles[@]}" || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
}

# ---------------------------------------------------------------------------
# trivy filesystem scan
# ---------------------------------------------------------------------------

_container_run_trivy_fs() {
  if ! ci::common::command_exists trivy; then
    _container_tool_skip trivy
    return 0
  fi
  ci::log::info "Running trivy filesystem scan..."
  local rc=0
  trivy fs . --scanners vuln --exit-code 1 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  fi
}

# ---------------------------------------------------------------------------
# trivy image scan (if image name is detectable)
# ---------------------------------------------------------------------------

_container_run_trivy_image() {
  if ! ci::common::command_exists trivy; then
    return 0
  fi
  # Try to detect image name from Dockerfile or docker-compose
  local image_name=""
  if [ -f Dockerfile ]; then
    image_name="$(grep -m1 '^FROM ' Dockerfile 2>/dev/null | awk '{print $2}' || true)"
  fi
  if [ -z "$image_name" ]; then
    return 0
  fi
  ci::log::info "Running trivy image scan on: ${image_name}"
  local rc=0
  trivy image --exit-code 1 "$image_name" 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_PASS_WITH_KNOWN_DEBT")"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_container_main() {
  ci::log::section "Check: container"

  _container_run_hadolint
  _container_run_trivy_fs
  _container_run_trivy_image

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Container result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _container_main "$@"
fi
