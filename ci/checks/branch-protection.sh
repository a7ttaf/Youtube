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

# A repository or tooling failure is not a policy violation. Reporting "cannot
# walk the range" as FAIL_NEW_ISSUE told the developer their commits break a
# rule, when what actually happened is that the check could not run — and it
# disagreed with git-safety.sh, which returns FAIL_INFRA for the same condition
# on the same range. The result contract distinguishes them; so does this.
_bp_infra() {
  ci::log::error "$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
}

# Somewhere for git's stderr to go that is not the variable being parsed.
#
# `2>&1` put diagnostics into the same string as the records: `git log
# --pretty='%G? %H'` output is read as "<signature status> <sha>" pairs, and a
# warning line -- dubious ownership, a replace-ref advisory, anything git
# decides to mention -- arrives as a record whose first word is not one of
# G/U/X/Y, so it is reported as an unsigned commit whose sha is the rest of the
# warning. The merge count has the sharper version of the same problem: one
# warning line and `[ "$merge_count" -gt 0 ]` is comparing prose to an integer.
# git succeeds in both cases, so this is a false failure, not a missed one.
#
# stderr is still reported -- it is the useful part of an infra message -- just
# not parsed.
_BP_ERR_FILE=""
_bp_err_setup() {
  # No predictable fallback. `/tmp/bp-err.$$` is guessable, and this path is
  # then opened for *writing* -- a symlink planted there by any user on the box
  # redirects git's stderr onto whatever the symlink names, with this process's
  # privileges. A gate that cannot obtain a private temp file has no business
  # continuing; there is nothing to fall back to that is not worse.
  _BP_ERR_FILE="$(mktemp 2>/dev/null)" || _BP_ERR_FILE=""
  if [ -z "$_BP_ERR_FILE" ]; then
    ci::log::error "Cannot create a private temporary file (mktemp failed)."
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
    exit "$OVERALL_RESULT"
  fi
  # shellcheck disable=SC2064
  trap "rm -f '$_BP_ERR_FILE'" EXIT
}
_bp_err_text() {
  [ -n "$_BP_ERR_FILE" ] && [ -f "$_BP_ERR_FILE" ] || return 0
  tr '\n' ' ' < "$_BP_ERR_FILE"
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

  # Where the push is going, when that is known, rather than where the working
  # tree happens to be standing.
  #
  # A refspec separates the two: `git push origin feature:main` writes to main
  # while HEAD says `feature`, so asking HEAD approved a direct push to a
  # protected branch. So did `git push origin HEAD:main`, and so did `git push
  # origin :main`, which deletes it. The pre-push hook is handed the
  # destination for every ref on stdin and now passes it on.
  #
  # Set-but-empty is a real answer and is not the same as unset: it means the
  # hook read the push and it names no branch destination -- a tag, say -- and
  # a tag push must not be refused for the branch you were standing on. Unset
  # means nobody told us, which is the pre-commit hook and every direct
  # invocation, and only then is the checkout the best available evidence.
  if [ -n "${CI_GATE_PUSH_REMOTE_REFS+set}" ]; then
    local ref blocked=0 seen=0
    # Unquoted for the word split, so globbing is turned off around it. git's
    # own ref-format rules forbid `*`, `?` and `[` in a branch name, so this
    # cannot fire today -- but if it ever did, an expanded pattern would arrive
    # as some filename and compare unequal to `main`, and this check would
    # approve the push. A fail-open is not the direction to leave to a
    # guarantee made somewhere else.
    set -f
    for ref in ${CI_GATE_PUSH_REMOTE_REFS}; do
      seen=1
      if _is_protected_branch "$ref"; then
        _bp_fail "Push to protected branch '${ref}' is not allowed. Use a feature branch and pull request."
        blocked=1
      fi
    done
    set +f
    if [ "$seen" -eq 0 ]; then
      ci::log::info "Push targets no branch ref; branch protection does not apply."
    elif [ "$blocked" -eq 0 ]; then
      # Deliberately not the words "protected branch": that phrase appearing in
      # this check's output has to mean a refusal and nothing else, or an
      # assertion looking for it passes on the run that allowed the push.
      ci::log::info "Push targets '${CI_GATE_PUSH_REMOTE_REFS}'; none is protected. OK."
    fi
    return 0
  fi

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
  # Enumerated before the loop so a failed walk is distinguishable from an empty
  # one. `git log ... || true` piped into a while-read reported "no commits" for
  # both, and this is the check that decides whether every pushed commit is
  # signed — the one place a silent empty result must not read as a pass.
  local log_out
  if ! log_out="$(git log --pretty='%G? %H' "$range" 2>"$_BP_ERR_FILE")"; then
    _bp_infra "Cannot walk the push range ${range}: $(_bp_err_text)"
    return 0
  fi
  while IFS=' ' read -r sig_status commit; do
    [ -z "$commit" ] && continue
    found=1
    case "$sig_status" in
      G|U|X|Y) ;;
      *)
        _bp_fail "Unsigned or unverified commit ${commit} (status: ${sig_status})."
        ;;
    esac
  done <<< "$log_out"
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
  if ! merge_count="$(git rev-list --count --merges "$range" 2>"$_BP_ERR_FILE")"; then
    _bp_infra "Cannot count merge commits in ${range}: $(_bp_err_text)"
    return 0
  fi
  # A count that is not a number is not a count. Left unchecked, `[ "$x" -gt 0 ]`
  # aborts the shell under errexit rather than reporting anything.
  case "$merge_count" in
    ''|*[!0-9]*)
      _bp_infra "Merge count for ${range} is not a number: '${merge_count}'"
      return 0
      ;;
  esac
  if [ "${merge_count:-0}" -gt 0 ]; then
    _bp_fail "Linear history required: ${merge_count} merge commit(s) found. Use rebase instead of merge."
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_branch_protection_main() {
  ci::log::section "Check: branch-protection"

  # Before any check, because an unset _BP_ERR_FILE makes `2>"$_BP_ERR_FILE"`
  # an ambiguous redirect and the walk would fail for a reason of our own
  # making.
  _bp_err_setup

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
