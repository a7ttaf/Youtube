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
if [ "${CI_GATE_MODE:-}" = "ship" ] && [ "${CI_GATE_PUSH_DELETIONS_ONLY:-0}" = "1" ]; then
  # `git push --delete origin feature` carries no content, so there is nothing
  # here to scan. Without this the missing new sha defaulted to HEAD and every
  # scan below reported on the checked-out branch -- commits that are not being
  # pushed anywhere, blocking the deletion of an unrelated ref. Whether the ref
  # may be deleted at all is branch-protection.sh's question, and it still runs.
  echo "Push deletes refs and carries no content; nothing to scan."
  exit "$CI_RESULT_PASS"
fi

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
  local sha rc=0
  if [ -n "$GATE_RANGE" ]; then
    while IFS= read -r sha; do
      [ -n "$sha" ] || continue
      # A commit whose blobs cannot be read -- a partial clone whose promisor
      # remote is unavailable is the realistic case -- used to become an empty
      # diff here, and an empty diff is indistinguishable from a clean one. The
      # failure is recorded and turned into FAIL_INFRA by the caller rather than
      # silently narrowing what was scanned.
      git show --format= "$@" "$sha" || rc=1
    done <<< "$GATE_COMMITS"
  else
    git diff --cached "$@"
  fi
  return "$rc"
}

# Whether every enumerated commit can actually be read. Asked once, up front, so
# each individual scan below does not have to carry the error path.
_gs_commits_readable() {
  local sha
  [ -n "$GATE_RANGE" ] || return 0
  while IFS= read -r sha; do
    [ -n "$sha" ] || continue
    # `--numstat`, not `--name-only`. Names come from the trees alone, so a
    # repository whose trees are present and whose blobs are not -- a partial
    # clone whose promisor remote is unreachable -- passed this probe. The
    # scans below then loaded the content, failed there, and the whitespace
    # check reported "conflict markers or whitespace errors" and exit 20 for an
    # object it could not read: a diagnostic naming the wrong problem, and a
    # new-issue verdict for what is infrastructure. Counting changed lines
    # needs the blobs, so this now fails where the scans would.
    git show --format= --numstat "$sha" >/dev/null 2>&1 || return 1
  done <<< "$GATE_COMMITS"
  return 0
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

# Asked here rather than beside the enumeration above, because the function has
# to be defined before it can be called.
if ! _gs_commits_readable; then
  echo "A commit in ${GATE_RANGE} cannot be read; its content was never scanned."
  echo "  A partial clone with an unavailable promisor remote does this."
  echo "  Refusing to report on commits that could not be inspected."
  exit "$CI_RESULT_FAIL_INFRA"
fi

# NUL-delimited, and read that way by the caller.
#
# `--name-only` alone emits git's *quoted* representation for any path outside
# plain ASCII: "cafÃ©.env" rather than cafe.env. The suffix patterns below
# then match nothing, because the path they are matching ends in a quote
# character, and `git cat-file -s "$sha:$path"` looks up a path that does not
# exist. A sensitive file with an accent in its name walked straight through.
# `-z` is git's documented way to get the literal bytes.
#
# Additions, copies, renames and modifications only. A path being *removed* is
# not content this commit introduces, and listing deletions blocked the one
# change that fixes the problem: committing `git rm secrets.env` failed the gate
# for the file it deletes. The index form had the same flaw and the same cure.
_gs_content_files() {
  _gs_diff --name-only -z --diff-filter=ACMR 2>/dev/null || true
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

# Collected once, and deduplicated, before anything is asked about them.
#
# _gs_content_files emits a record per commit per modification, so a file
# touched by forty of the pushed commits arrived forty times -- and every
# arrival called _gs_max_blob_size, which walks the whole commit list again.
# That is |emissions| x |commits| separate `git cat-file` processes. It is
# invisible on an ordinary push, where the base is one commit back, and
# quadratic on a push with no resolvable base: a first push, or any branch the
# remote has never seen, makes GATE_COMMITS the entire history. Measured in
# this tree at 419 commits and ~3800 emissions, with one path costing 19s, and
# the pre-push budget is 120s.
_GS_PATHS=()
_GS_SIZES=()
_GS_NL_PATHS=()
_gs_lf=$'\n'
_gs_seen="$_gs_lf"
while IFS= read -r -d '' path; do
  [ -z "$path" ] && continue
  case "$path" in
    *"$_gs_lf"*)
      # A newline inside a path cannot survive the line-oriented batch below;
      # it would split into two records and desynchronise every result after
      # it. Sized one at a time instead of dropped, because a path this gate
      # cannot measure must not silently measure zero -- and because leaving it
      # to the slow path is what stops a file named with a newline from being
      # a way to push the gate back over its budget on purpose.
      #
      # Deduplicated here rather than through _gs_seen, and this arm was
      # bypassing that set entirely: a newline path modified by forty of the
      # pushed commits arrived forty times and was sized forty times, each call
      # walking every outgoing commit again. That is the quadratic cost the
      # batch below exists to remove, reintroduced on the one path that cannot
      # use it -- and it is reachable on purpose, since the name is the
      # attacker's to choose.
      #
      # _gs_seen cannot answer it: that set is newline-delimited, so a path
      # containing a newline can match the join of two unrelated records --
      # "a\nb" reads as already seen once `a` and `b` have been. A linear scan
      # over the list itself has no separator to be confused by, and the list is
      # empty in every ordinary push.
      _gs_dup=0
      if [ "${#_GS_NL_PATHS[@]}" -gt 0 ]; then
        for _gs_prev in "${_GS_NL_PATHS[@]}"; do
          if [ "$_gs_prev" = "$path" ]; then _gs_dup=1; break; fi
        done
      fi
      if [ "$_gs_dup" -eq 0 ]; then
        _GS_NL_PATHS+=("$path")
      fi
      continue
      ;;
  esac
  case "$_gs_seen" in *"${_gs_lf}${path}${_gs_lf}"*) continue ;; esac
  _gs_seen="${_gs_seen}${path}${_gs_lf}"
  _GS_PATHS+=("$path")
done < <(_gs_content_files)

# One `git cat-file --batch-check` for every (commit, path) pair in the run,
# rather than one `git cat-file -s` per pair. Identical question, identical
# answer -- the max of `<commit>:<path>` over the enumerated commits, missing
# objects counting zero exactly as `|| echo 0` did -- asked in one process.
# 113,549 pairs answer in 3.2s here; the same pairs cost hours one at a time.
if [ "${#_GS_PATHS[@]}" -gt 0 ]; then
  if [ -z "$GATE_RANGE" ]; then
    for path in "${_GS_PATHS[@]}"; do
      _GS_SIZES+=("$(_gs_max_blob_size "$path")")
    done
  else
    _gs_ncommits="$(printf '%s\n' "$GATE_COMMITS" | grep -c '[^[:space:]]' || true)"
    if [ "${_gs_ncommits:-0}" -gt 0 ]; then
      _gs_rc=0
      # Pairs are emitted path-major, so the answers arrive in groups of
      # _gs_ncommits and the n-th group is the n-th path. batch-check prints
      # exactly one line per input line, which is what makes that hold; the
      # count check below refuses to trust the mapping if it ever stops holding.
      _gs_sizes_out="$(printf '%s\n' "${_GS_PATHS[@]}" \
        | awk -v commits="$GATE_COMMITS" '
            BEGIN { n = split(commits, c, "\n") }
            { for (i = 1; i <= n; i++) if (c[i] != "") print c[i] ":" $0 }' \
        | git cat-file --batch-check 2>/dev/null \
        | awk -v n="$_gs_ncommits" '
            { size = (NF == 3 && $2 == "blob" && $3 ~ /^[0-9]+$/) ? $3 + 0 : 0
              if (size > max) max = size
              if (FNR % n == 0) { print max + 0; max = 0 } }')" || _gs_rc=$?
      if [ "$_gs_rc" -ne 0 ]; then
        echo "Could not size the blobs in ${GATE_RANGE} (git cat-file exited ${_gs_rc})."
        echo "  Refusing to report on file sizes that were never measured."
        exit "$CI_RESULT_FAIL_INFRA"
      fi
      while IFS= read -r _gs_sz; do
        [ -n "$_gs_sz" ] || continue
        _GS_SIZES+=("$_gs_sz")
      done <<< "$_gs_sizes_out"
      # An answer per path, or the positional mapping is not one. Silently
      # short output would shift every later path onto another path's size,
      # which is the same class of wrong answer as not measuring at all.
      if [ "${#_GS_SIZES[@]}" -ne "${#_GS_PATHS[@]}" ]; then
        echo "Blob sizing returned ${#_GS_SIZES[@]} results for ${#_GS_PATHS[@]} paths."
        echo "  The results cannot be matched to the paths they describe."
        exit "$CI_RESULT_FAIL_INFRA"
      fi
    else
      while [ "${#_GS_SIZES[@]}" -lt "${#_GS_PATHS[@]}" ]; do
        _GS_SIZES+=(0)
      done
    fi
  fi
fi

# Appended after the batch, sized the original way. Same checks, same verdict.
if [ "${#_GS_NL_PATHS[@]}" -gt 0 ]; then
  for path in "${_GS_NL_PATHS[@]}"; do
    _GS_PATHS+=("$path")
    _GS_SIZES+=("$(_gs_max_blob_size "$path")")
  done
fi

_gs_idx=0
while [ "$_gs_idx" -lt "${#_GS_PATHS[@]}" ]; do
  path="${_GS_PATHS[$_gs_idx]}"
  size_bytes="${_GS_SIZES[$_gs_idx]}"
  _gs_idx=$((_gs_idx + 1))
  # Matched on the basename as well as the whole path.
  #
  # The list was written in repository-root spellings, and a case pattern like
  # `.npmrc` matches the string ".npmrc" and nothing else -- so a package
  # manager credential staged at `frontend/.npmrc` or `packages/app/.pypirc`
  # went straight through. The content scan is not a backstop for these: an
  # `//registry.npmjs.org/:_authToken=` line matches none of the canonical
  # token prefixes in CI_CHECKS_SECRET_PATTERN, so nothing else was going to
  # catch it either. `.env` survived only by accident, because `*.env` happens
  # to match a nested path; the ones without a leading-wildcard twin did not.
  #
  # Names are matched where they mean something -- a credential file is
  # sensitive wherever it sits, and nested workspaces are the layout this PR
  # spent rounds teaching the rest of the gate to expect.
  base="${path##*/}"
  case "$base" in
    .env|.env.*|.env-*|env.local|env.local.*|*.env|*.env.*|*.env-* \
      |*.pem|*.key|*.p12|*.pfx|id_rsa|id_ed25519|*id_rsa|*id_ed25519 \
      |*.jks|.npmrc|.pypirc|.netrc|_netrc|.pgpass|credentials|*.gpg)
      echo "Sensitive file ${GATE_WHAT}: $path"
      SENSITIVE_FILE_MATCH=1
      ;;
  esac
  case "$path" in
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

  if [ "${size_bytes:-0}" -gt 5242880 ]; then
    echo "Large file detected ${GATE_WHAT} (>5MB): $path (${size_bytes} bytes)"
    LARGE_ARTIFACT_MATCH=1
  fi
done

secret_pattern_file="$(mktemp)"
secret_cleanup() {
  rm -f "$secret_pattern_file" 2>/dev/null || true
}
trap secret_cleanup EXIT INT TERM
printf '%s\n' "^\+.*(${CI_CHECKS_SECRET_PATTERN})" > "$secret_pattern_file"
# `$?` after `! cmd` is the status of the *negation*, which is 0 whenever the
# condition was taken — so this branch ran with rc=0, the test below was never
# true, and a malformed pattern silently disabled secret detection for the whole
# run. grep's own status is captured without the `!` in front of it: 0 matched,
# 1 no match, 2 or more the pattern itself is broken.
rc=0
grep -E -f "$secret_pattern_file" /dev/null >/dev/null 2>&1 || rc=$?
if [ "$rc" -ge 2 ]; then
  echo "CI_CHECKS_SECRET_PATTERN is not a valid extended regex; the secret scan"
  echo "  cannot run, and would otherwise report no match for every input."
  exit "$CI_RESULT_FAIL_INFRA"
fi
secret_rc=0
_gs_diff -U0 | grep -E -f "$secret_pattern_file" >/dev/null 2>&1 || secret_rc=$?
if [ "$secret_rc" -eq 0 ]; then
  echo "Potential secret-like value detected in additions ${GATE_WHAT}."
  SECRET_PATTERN_MATCH=1
elif [ "$secret_rc" -ge 2 ]; then
  # The validation above should have caught this. The scan is the thing that
  # matters, though, and "grep broke" must never arrive here as "nothing found".
  echo "The secret scan failed to run (grep exited ${secret_rc})."
  exit "$CI_RESULT_FAIL_INFRA"
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
