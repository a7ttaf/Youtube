#!/usr/bin/env bash
# ci/checks/commit-hygiene.sh – Commit hygiene checks.
# Checks: conventional commit format, subject length, diff size, forbidden files.
# Usage: commit-hygiene.sh [<commit-msg-file>]
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

OVERALL_RESULT=$CI_RESULT_PASS

# Thresholds (can be overridden by env)
MAX_SUBJECT_LEN="${CI_GATE_MAX_SUBJECT_LEN:-72}"
MIN_SUBJECT_LEN="${CI_GATE_MIN_SUBJECT_LEN:-10}"
WARN_DIFF_LINES="${CI_GATE_WARN_DIFF_LINES:-500}"
MAX_DIFF_LINES="${CI_GATE_MAX_DIFF_LINES:-1000}"

# Conventional commit regex (Bash ERE)
CONVENTIONAL_COMMIT_RE='^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(\(.+\))?: .+'

# Forbidden file patterns (gitignore-style basename matches)
FORBIDDEN_PATTERNS=('.env' '*.pem' 'id_rsa' 'id_ed25519' '*.key' '*.p12' '*.pfx' '*.jks' '.npmrc' '.pypirc')

_hygiene_fail() {
  ci::log::error "$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
}

_hygiene_warn() {
  ci::log::warn "$1"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_PASS_WITH_KNOWN_DEBT")"
}

# ---------------------------------------------------------------------------
# Commit message checks
# ---------------------------------------------------------------------------

_hygiene_check_message() {
  local subject="$1"

  # Conventional commit format
  if ! echo "$subject" | grep -qE "$CONVENTIONAL_COMMIT_RE"; then
    _hygiene_fail "Commit subject does not follow Conventional Commits format: '${subject}'"
    _hygiene_fail "Expected: <type>(<scope>)?: <description>"
    _hygiene_fail "Types: feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert"
  fi

  # Subject length
  local len="${#subject}"
  if [ "$len" -gt "$MAX_SUBJECT_LEN" ]; then
    _hygiene_fail "Commit subject too long: ${len} chars (max ${MAX_SUBJECT_LEN}): '${subject}'"
  fi
  if [ "$len" -lt "$MIN_SUBJECT_LEN" ]; then
    _hygiene_fail "Commit subject too short: ${len} chars (min ${MIN_SUBJECT_LEN}): '${subject}'"
  fi
}

# ---------------------------------------------------------------------------
# Diff size check
# ---------------------------------------------------------------------------

_hygiene_check_diff_size() {
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    ci::log::info "No HEAD commit; skipping diff size check."
    return 0
  fi
  local lines_changed
  lines_changed="$(git diff --cached --shortstat 2>/dev/null | \
    grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)"
  local lines_deleted
  lines_deleted="$(git diff --cached --shortstat 2>/dev/null | \
    grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)"
  local total=$(( lines_changed + lines_deleted ))

  if [ "$total" -gt "$MAX_DIFF_LINES" ]; then
    _hygiene_fail "Diff too large: ${total} lines changed (max ${MAX_DIFF_LINES}). Consider splitting this commit."
  elif [ "$total" -gt "$WARN_DIFF_LINES" ]; then
    _hygiene_warn "Large diff: ${total} lines changed (warn threshold ${WARN_DIFF_LINES}). Consider splitting."
  fi
}

# ---------------------------------------------------------------------------
# Forbidden file patterns
# ---------------------------------------------------------------------------

_basename_matches_pattern() {
  local name="$1" pattern="$2"
  # shellcheck disable=SC2254
  case "$name" in
    $pattern) return 0 ;;
  esac
  return 1
}

_hygiene_check_forbidden_files() {
  while IFS= read -r path; do
    [ -z "$path" ] && continue
    local base
    base="$(basename "$path")"
    for pattern in "${FORBIDDEN_PATTERNS[@]}"; do
      if _basename_matches_pattern "$base" "$pattern"; then
        _hygiene_fail "Forbidden file staged: ${path} (matches pattern '${pattern}')"
        break
      fi
    done
  done < <(git diff --cached --name-only 2>/dev/null || true)
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_commit_hygiene_main() {
  ci::log::section "Check: commit-hygiene"

  # Read commit message: from file arg (commit-msg hook) or from last commit
  local subject=""
  if [ -n "${1:-}" ] && [ -f "$1" ]; then
    # Strip comments and blank lines, take first non-empty line
    subject="$(grep -v '^#' "$1" | grep -v '^[[:space:]]*$' | head -n1 || true)"
  else
    if git rev-parse --verify HEAD >/dev/null 2>&1; then
      subject="$(git log -1 --pretty=%s 2>/dev/null || true)"
    fi
  fi

  if [ -z "$subject" ]; then
    _hygiene_fail "Could not determine commit message subject."
  else
    _hygiene_check_message "$subject"
  fi

  _hygiene_check_diff_size
  _hygiene_check_forbidden_files

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Commit-hygiene result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _commit_hygiene_main "$@"
fi
