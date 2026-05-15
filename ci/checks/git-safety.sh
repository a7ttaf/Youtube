#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/git.sh
source "$ROOT_DIR/ci/lib/git.sh"
# shellcheck source=ci/checks/common.sh
source "$ROOT_DIR/ci/checks/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: git safety"

if ! ci::git::is_repo; then
  echo "Not inside a git repository."
  exit "$CI_RESULT_FAIL_INFRA"
fi

BRANCH="$(ci::git::current_branch)"
if [ -z "$BRANCH" ]; then
  if [ "${CI:-}" = "true" ] || [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "Detached HEAD detected (expected in CI). Skipping branch name check."
    BRANCH="(detached)"
  else
    echo "Cannot detect current branch (detached HEAD or invalid repository state)."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi

echo "Branch: $BRANCH"

if ! git diff --check >/dev/null 2>&1; then
  echo "Whitespace/conflict-marker problems found in unstaged changes."
  git diff --check || true
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

if ! git diff --cached --check >/dev/null 2>&1; then
  echo "Whitespace/conflict-marker problems found in staged changes."
  git diff --cached --check || true
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

# git diff --check already catches conflict markers. Keep this explicit helper
# check so the failure message is specific and easier to understand.
if ci::git::has_conflict_markers_in_changed || ci::git::has_conflict_markers_in_staged; then
  echo "Merge conflict markers found in changed/staged content."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

SENSITIVE_FILE_MATCH=0
BUILD_ARTIFACT_MATCH=0
VENV_OR_NODE_MODULES_MATCH=0
LARGE_ARTIFACT_MATCH=0
SECRET_PATTERN_MATCH=0

if [ -z "${CI_CHECKS_SECRET_PATTERN:-}" ]; then
  echo "CI_CHECKS_SECRET_PATTERN is missing or empty; cannot run secret diff scan."
  exit "$CI_RESULT_FAIL_INFRA"
fi

while IFS= read -r path; do
  [ -z "$path" ] && continue
  case "$path" in
    .env|.env.*|.env-*|env.local|env.local.*|*.env|*.env.*|*.env-*|*.pem|*.key|*.p12|*.pfx|*id_rsa|*id_ed25519|*.jks|.npmrc|.pypirc|*.gpg)
      echo "Sensitive file is staged: $path"
      SENSITIVE_FILE_MATCH=1
      ;;
    node_modules/*)
      echo "node_modules path is staged: $path"
      VENV_OR_NODE_MODULES_MATCH=1
      ;;
    .venv/*|venv/*)
      echo "Virtual environment path is staged: $path"
      VENV_OR_NODE_MODULES_MATCH=1
      ;;
    dist/*|build/*|coverage/*|htmlcov/*)
      echo "Build output path is staged (blocked by default): $path"
      BUILD_ARTIFACT_MATCH=1
      ;;
  esac

  safe_path="./$path"
  if [ -f "$safe_path" ]; then
    size_bytes="$(wc -c < "$safe_path" 2>/dev/null || echo 0)"
    if [ "${size_bytes:-0}" -gt 5242880 ]; then
      echo "Large staged file detected (>5MB): $path (${size_bytes} bytes)"
      LARGE_ARTIFACT_MATCH=1
    fi
  fi
done < <(ci::git::staged_files)

secret_pattern_file="$(mktemp)"
secret_cleanup() {
  rm -f "$secret_pattern_file" 2>/dev/null || true
}
trap secret_cleanup EXIT INT TERM
printf '%s\n' "^\+.*(${CI_CHECKS_SECRET_PATTERN})" > "$secret_pattern_file"
if git diff --cached -U0 | grep -E -f "$secret_pattern_file" >/dev/null 2>&1; then
  echo "Potential secret-like value detected in staged additions."
  SECRET_PATTERN_MATCH=1
fi
secret_cleanup

if [ "$SENSITIVE_FILE_MATCH" -eq 1 ] || [ "$BUILD_ARTIFACT_MATCH" -eq 1 ] || [ "$VENV_OR_NODE_MODULES_MATCH" -eq 1 ] || [ "$LARGE_ARTIFACT_MATCH" -eq 1 ] || [ "$SECRET_PATTERN_MATCH" -eq 1 ]; then
  blocked_reasons=()
  if [ "$SENSITIVE_FILE_MATCH" -eq 1 ]; then
    blocked_reasons+=("sensitive-staged-files")
  fi
  if [ "$BUILD_ARTIFACT_MATCH" -eq 1 ]; then
    blocked_reasons+=("build-artifacts-staged")
  fi
  if [ "$VENV_OR_NODE_MODULES_MATCH" -eq 1 ]; then
    blocked_reasons+=("venv-or-node_modules-staged")
  fi
  if [ "$LARGE_ARTIFACT_MATCH" -eq 1 ]; then
    blocked_reasons+=("large-staged-files")
  fi
  if [ "$SECRET_PATTERN_MATCH" -eq 1 ]; then
    blocked_reasons+=("secret-pattern-match")
  fi
  printf 'Blocking staged content checks: %s\n' "${blocked_reasons[*]}"
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

echo "Git safety checks passed."
exit "$CI_RESULT_PASS"
