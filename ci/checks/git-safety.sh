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

# Which content is this run vouching for?
#
# Everything below scanned the index, which describes the pre-commit gate and
# nothing else. In ship mode -- the pre-push gate -- the commit already exists
# and the index matches HEAD, so `git diff --cached` is empty: the sensitive
# file, build artifact, large blob, conflict marker and secret pattern scans all
# inspected nothing and the gate passed. Proven, not inferred: the same
# secrets.env exits 20 staged and exited 0 once committed.
#
# So the reference follows the gate's own mode. GATE_RANGE empty means the
# index; otherwise it is the range of commits being pushed.
GATE_RANGE=""
GATE_COMMITS=""
GATE_WHAT="staged"
if [ "${CI_GATE_MODE:-}" = "ship" ]; then
  GATE_RANGE="$(ci::git::push_range 2>/dev/null || true)"
  if [ -z "$GATE_RANGE" ]; then
    echo "Cannot determine the range being pushed; refusing to report on it."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
  GATE_WHAT="in the pushed commits"

  # Enumerated once, and fail-closed. Every scan below walks this list, and each
  # of them reached it through `git rev-list ... || true`: an unresolvable range
  # produced no commits, no scanning, and a confident PASS on the security path.
  # "Nothing to push" and "the walk failed" are indistinguishable in the output
  # and must not be indistinguishable in the result, so the exit status decides.
  if ! GATE_COMMITS="$(git rev-list "$GATE_RANGE" 2>&1)"; then
    echo "Cannot enumerate the commits being pushed (${GATE_RANGE}):"
    printf '%s\n' "$GATE_COMMITS"
    echo "  Refusing to report on a range that could not be walked."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi

# The diff this run is reporting on.
#
# In ship mode that is *every outgoing commit*, not the net change between the
# base and HEAD. `git diff base..HEAD` collapses the endpoints, so a token added
# by one commit and removed by a later one in the same push disappears from the
# diff while both commits are pushed and the token stays in the history forever.
# `git rev-list` walks each commit; `git show` prints what that commit added.
#
# --first-parent is deliberately absent: a merged side branch is part of what is
# being pushed, and its commits carry their content into the remote too.
_gs_diff() {
  local sha
  if [ -n "$GATE_RANGE" ]; then
    while IFS= read -r sha; do
      [ -n "$sha" ] || continue
      git show --format= "$@" "$sha" 2>/dev/null || true
    done <<< "$GATE_COMMITS"
  else
    git diff --cached "$@"
  fi
}

# The largest this path ever was anywhere in what is being reported on. `HEAD:`
# cannot see a blob that existed only in an intermediate commit, which is the
# same hole as the net diff: a 40MB file added and removed within one push is
# still 40MB in the objects the remote receives.
_gs_max_blob_size() {
  local path="$1" sha size max=0
  if [ -z "$GATE_RANGE" ]; then
    git cat-file -s ":$path" 2>/dev/null || echo 0
    return 0
  fi
  while IFS= read -r sha; do
    [ -n "$sha" ] || continue
    size="$(git cat-file -s "${sha}:${path}" 2>/dev/null || echo 0)"
    [ "${size:-0}" -gt "$max" ] && max="$size"
  done <<< "$GATE_COMMITS"
  printf '%s' "$max"
}

_gs_content_files() {
  # Additions, copies, renames and modifications only. A path that is being
  # *removed* is not content this commit introduces, and listing deletions here
  # blocked the one change that fixes the problem: committing `git rm secrets.env`
  # failed the gate for the file it deletes. The index form had the same flaw and
  # the same cure.
  _gs_diff --name-only --diff-filter=ACMR 2>/dev/null || true
}

# `--check` reports through its exit status, and _gs_diff swallows that: it runs
# one git per commit and cannot return all of them. So this asks the question
# separately rather than reusing the helper — the alternative is an `if` that
# can never be false, which is the failure mode this whole PR is about.
_gs_check() {
  local sha rc=0
  if [ -z "$GATE_RANGE" ]; then
    git diff --cached --check || rc=$?
    return "$rc"
  fi
  while IFS= read -r sha; do
    [ -n "$sha" ] || continue
    git show --format= --check "$sha" || rc=1
  done <<< "$GATE_COMMITS"
  return "$rc"
}

if ! _gs_check >/dev/null 2>&1; then
  echo "Whitespace/conflict-marker problems found ${GATE_WHAT}."
  _gs_check || true
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

# git diff --check already catches conflict markers. Keep this explicit helper
# check so the failure message is specific and easier to understand.
if ci::git::has_conflict_markers_in_changed \
  || _gs_diff -U0 | grep -E '^\+[[:space:]]*(<{7}|={7}|>{7})([[:space:]]|$)' >/dev/null 2>&1; then
  echo "Merge conflict markers found in changed content or ${GATE_WHAT}."
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
      echo "Sensitive file ${GATE_WHAT}: $path"
      SENSITIVE_FILE_MATCH=1
      ;;
    node_modules/*|*/node_modules/*)
      echo "node_modules path ${GATE_WHAT}: $path"
      VENV_OR_NODE_MODULES_MATCH=1
      ;;
    .venv/*|venv/*|*/.venv/*|*/venv/*)
      echo "Virtual environment path ${GATE_WHAT}: $path"
      VENV_OR_NODE_MODULES_MATCH=1
      ;;
    dist/*|build/*|coverage/*|htmlcov/*|*/dist/*|*/build/*|*/coverage/*|*/htmlcov/*)
      echo "Build output path ${GATE_WHAT} (blocked by default): $path"
      BUILD_ARTIFACT_MATCH=1
      ;;
  esac

  size_bytes="$(_gs_max_blob_size "$path")"
  if [ "${size_bytes:-0}" -gt 5242880 ]; then
    echo "Large file detected ${GATE_WHAT} (>5MB): $path (${size_bytes} bytes)"
    LARGE_ARTIFACT_MATCH=1
  fi
done < <(_gs_content_files)

secret_pattern_file="$(mktemp)"
secret_cleanup() {
  rm -f "$secret_pattern_file" 2>/dev/null || true
}
trap secret_cleanup EXIT INT TERM
printf '%s\n' "^\+.*(${CI_CHECKS_SECRET_PATTERN})" > "$secret_pattern_file"
if ! grep -E -f "$secret_pattern_file" /dev/null >/dev/null 2>&1; then
  rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "CI_CHECKS_SECRET_PATTERN is not a valid extended regex."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi
if _gs_diff -U0 | grep -E -f "$secret_pattern_file" >/dev/null 2>&1; then
  echo "Potential secret-like value detected in additions ${GATE_WHAT}."
  SECRET_PATTERN_MATCH=1
fi
secret_cleanup

if [ "$SENSITIVE_FILE_MATCH" -eq 1 ] || [ "$BUILD_ARTIFACT_MATCH" -eq 1 ] || [ "$VENV_OR_NODE_MODULES_MATCH" -eq 1 ] || [ "$LARGE_ARTIFACT_MATCH" -eq 1 ] || [ "$SECRET_PATTERN_MATCH" -eq 1 ]; then
  blocked_reasons=()
  if [ "$SENSITIVE_FILE_MATCH" -eq 1 ]; then
    blocked_reasons+=("sensitive-files")
  fi
  if [ "$BUILD_ARTIFACT_MATCH" -eq 1 ]; then
    blocked_reasons+=("build-artifacts")
  fi
  if [ "$VENV_OR_NODE_MODULES_MATCH" -eq 1 ]; then
    blocked_reasons+=("venv-or-node_modules")
  fi
  if [ "$LARGE_ARTIFACT_MATCH" -eq 1 ]; then
    blocked_reasons+=("large-files")
  fi
  if [ "$SECRET_PATTERN_MATCH" -eq 1 ]; then
    blocked_reasons+=("secret-pattern-match")
  fi
  printf 'Blocking content checks (%s): %s\n' "$GATE_WHAT" "${blocked_reasons[*]}"
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

echo "Git safety checks passed."
exit "$CI_RESULT_PASS"
