#!/usr/bin/env bash
# ci/checks/branch-protection.sh – Local branch protection simulation.
# Enforces: no direct push to main/master, signed commits (optional),
# linear history (optional).
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"
# shellcheck source=ci/lib/git.sh
source "$ROOT_DIR/ci/lib/git.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

# Protected branches (space-separated, can be overridden)
CI_GATE_PROTECTED_BRANCHES="${CI_GATE_PROTECTED_BRANCHES:-main master}"
CI_GATE_REQUIRE_SIGNED_COMMITS="${CI_GATE_REQUIRE_SIGNED_COMMITS:-0}"
CI_GATE_REQUIRE_LINEAR_HISTORY="${CI_GATE_REQUIRE_LINEAR_HISTORY:-0}"

_bp_fail() {
  ci::log::error "$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
}

# The range this check reasons about is the same one git-safety.sh scans for
# staged-content problems, so it is computed in one place. Kept as a name here
# because the call sites below read better with it.
_bp_commit_range() {
  ci::git::push_range
}

_is_protected_branch() {
  local branch="$1"
  for protected in $CI_GATE_PROTECTED_BRANCHES; do
    if [ "$branch" = "$protected" ]; then
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# Check: no direct commits to protected branches
# ---------------------------------------------------------------------------

_bp_check_protected_branch() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"

  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    ci::log::info "Detached HEAD; skipping branch protection check."
    return 0
  fi

  if _is_protected_branch "$branch"; then
    _bp_fail "Direct push to protected branch '${branch}' is not allowed. Use a feature branch and pull request."
  else
    ci::log::info "Branch '${branch}' is not a protected branch; OK."
  fi
}

# ---------------------------------------------------------------------------
# Check: signed commits
# ---------------------------------------------------------------------------

_bp_check_signed_commits() {
  if [ "${CI_GATE_REQUIRE_SIGNED_COMMITS:-0}" != "1" ]; then
    return 0
  fi
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    ci::log::info "No HEAD commit; skipping signed commit check."
    return 0
  fi
  local range
  if ! range="$(_bp_commit_range)"; then
    ci::log::info "No push range available; skipping signed commit check."
    return 0
  fi

  ci::log::info "Checking commit signatures in range: ${range}"
  local sig_status commit found=0
  while IFS=' ' read -r sig_status commit; do
    [ -z "$commit" ] && continue
    found=1
    case "$sig_status" in
      G|U|X|Y) ;;
      *)
        _bp_fail "Unsigned or unverified commit ${commit} (status: ${sig_status})."
        ;;
    esac
  done < <(git log --pretty='%G? %H' "$range" 2>/dev/null || true)
  [ "$found" -eq 1 ] || ci::log::info "No commits found in push range; skipping signed commit check."
}

# ---------------------------------------------------------------------------
# Check: linear history (no merge commits)
# ---------------------------------------------------------------------------

_bp_check_linear_history() {
  if [ "${CI_GATE_REQUIRE_LINEAR_HISTORY:-0}" != "1" ]; then
    return 0
  fi
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    return 0
  fi

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
    return 0
  fi

  local range
  if ! range="$(_bp_commit_range)"; then
    ci::log::info "No push range available; skipping linear-history check."
    return 0
  fi

  ci::log::info "Checking for merge commits in range: ${range}"
  local merge_count
  merge_count="$(git rev-list --count --merges "$range" 2>/dev/null || echo 0)"
  if [ "${merge_count:-0}" -gt 0 ]; then
    _bp_fail "Linear history required: ${merge_count} merge commit(s) found. Use rebase instead of merge."
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_branch_protection_main() {
  ci::log::section "Check: branch-protection"

  _bp_check_protected_branch
  _bp_check_signed_commits
  _bp_check_linear_history

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Branch-protection result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" = "${0}" ]]; then
  _branch_protection_main "$@"
fi
