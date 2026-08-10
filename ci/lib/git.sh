#!/usr/bin/env bash
set -Eeuo pipefail

ci::git::current_branch() {
  git branch --show-current 2>/dev/null || true
}

ci::git::current_sha() {
  git rev-parse HEAD 2>/dev/null || true
}

ci::git::changed_files() {
  git diff --name-only 2>/dev/null || true
}

ci::git::staged_files() {
  git diff --cached --name-only 2>/dev/null || true
}

ci::git::untracked_files() {
  git ls-files --others --exclude-standard 2>/dev/null || true
}

ci::git::is_repo() {
  git rev-parse --git-dir >/dev/null 2>&1
}

ci::git::has_conflict_markers_in_staged() {
  git diff --cached -U0 | grep -E '^\+[[:space:]]*(<{7}|={7}|>{7})([[:space:]]|$)' >/dev/null 2>&1
}

# ci::git::push_range – echo the `A..B` range a pre-push gate is standing behind.
#
# The hook's own old/new SHAs when it has them, the merge base with the upstream
# otherwise, and HEAD~1 as a last resort. A root commit has no predecessor, so
# the empty tree stands in: the range is then the whole of HEAD, which is
# exactly what a first push contains.
#
# Lives here rather than in one check because two of them need the same answer,
# and a second copy is how the last duplicated computation in this gate drifted
# out of step with the first.
ci::git::push_range() {
  local old_sha="${CI_GATE_PUSH_OLD_SHA:-${GITHUB_EVENT_BEFORE:-}}"
  local new_sha="${CI_GATE_PUSH_NEW_SHA:-HEAD}"
  local zero_sha="0000000000000000000000000000000000000000"
  # The hash of git's empty tree, which is stable across every repository.
  local empty_tree="4b825dc642cb6eb9a060e54bf8d69288fbee4904"
  local upstream base=""

  if [ -n "$old_sha" ] && [ "$old_sha" != "$zero_sha" ] \
    && git rev-parse --verify "${old_sha}^{commit}" >/dev/null 2>&1 \
    && git rev-parse --verify "${new_sha}^{commit}" >/dev/null 2>&1; then
    printf '%s..%s' "$old_sha" "$new_sha"
    return 0
  fi

  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
  if [ -n "$upstream" ]; then
    base="$(git merge-base HEAD "$upstream" 2>/dev/null || true)"
  fi
  if [ -z "$base" ]; then
    base="$(git rev-parse HEAD~1 2>/dev/null || true)"
  fi
  if [ -z "$base" ]; then
    git rev-parse --verify HEAD >/dev/null 2>&1 || return 1
    base="$empty_tree"
  fi
  printf '%s..HEAD' "$base"
}

ci::git::has_conflict_markers_in_changed() {
  git diff -U0 | grep -E '^\+[[:space:]]*(<{7}|={7}|>{7})([[:space:]]|$)' >/dev/null 2>&1
}
