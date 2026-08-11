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
# Lives here rather than in one check because two of them need the same answer,
# and a second copy is how the last duplicated computation in this gate drifted
# out of step with the first.
#
# The order matters more than any single step. `HEAD~1` was in it as a "last
# resort" and is not one: on the first push of a three-commit branch with no
# upstream configured, it silently reduced the range to the final commit, and a
# secret added by the first of the three sailed through a gate reporting on the
# third. A wrong base is worse than no base, because it produces a confident
# green. So: the hook's own SHAs, then @{push}, then @{upstream}, then the merge
# base with the remote default branch, then the local default branch, and only
# then the whole of HEAD — which is what a genuine first push contains anyway.
# `ci::changeset::detect pre-push` resolves the same question the same way.
ci::git::push_range() {
  local old_sha="${CI_GATE_PUSH_OLD_SHA:-${GITHUB_EVENT_BEFORE:-}}"
  local new_sha="${CI_GATE_PUSH_NEW_SHA:-HEAD}"
  local zero_sha="0000000000000000000000000000000000000000"
  local ref base="" default_branch

  if [ -n "$old_sha" ] && [ "$old_sha" != "$zero_sha" ] \
    && git rev-parse --verify "${old_sha}^{commit}" >/dev/null 2>&1 \
    && git rev-parse --verify "${new_sha}^{commit}" >/dev/null 2>&1; then
    printf '%s..%s' "$old_sha" "$new_sha"
    return 0
  fi

  # Everything below measures against the tip being pushed, not the literal
  # HEAD. The hook can supply a new sha without an old one — a brand-new branch,
  # or `git push origin some-other-branch` — and measuring that against HEAD
  # reports on whichever branch the developer happens to be standing on.
  local tip="$new_sha"
  git rev-parse --verify "${tip}^{commit}" >/dev/null 2>&1 || tip="HEAD"

  # @{push} is the ref this branch would actually push to, which is not always
  # @{upstream} — triangular workflows differ, and this repository's own layout
  # is one push remote away from being such a case.
  for ref in '@{push}' '@{upstream}'; do
    base="$(git merge-base "$tip" "$ref" 2>/dev/null || true)"
    [ -n "$base" ] && break
  done

  if [ -z "$base" ]; then
    default_branch="$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null \
      | sed 's|refs/remotes/origin/||' || true)"
    [ -n "$default_branch" ] || default_branch="main"
    for ref in "origin/${default_branch}" "$default_branch" origin/main main origin/master master; do
      base="$(git merge-base "$tip" "$ref" 2>/dev/null || true)"
      [ -n "$base" ] || continue
      # A remote-tracking ref knows what the remote already has, so a base
      # equal to the tip is a real answer: nothing new is being pushed.
      case "$ref" in origin/*) break ;; esac
      # A bare local name is a guess, and this one guessed us. On a branch
      # named `main` with no remote configured, `merge-base HEAD main` is HEAD,
      # the range is empty, and every ship-mode check reports "nothing changed"
      # over a push carrying the whole branch — the pre-push gate self-skipping
      # the last moment before the commits leave. Discard the guess and let the
      # bare-tip fallback below walk all of HEAD, which is what that push is.
      [ "$base" = "$(git rev-parse --verify "${tip}^{commit}" 2>/dev/null)" ] || break
      base=""
    done
  fi

  if [ -z "$base" ]; then
    git rev-parse --verify "$tip" >/dev/null 2>&1 || return 1
    # Every commit reachable from the tip, which is what a genuine first push
    # contains. Written as a plain rev rather than `<empty-tree>..<tip>`: the
    # empty tree is a tree object, not a commit, and handing a non-commit to a
    # revision walk is one `|| true` away from a silent empty result. Callers
    # pass this straight to `git rev-list` and `git log`, both of which take it.
    printf '%s' "$tip"
    return 0
  fi
  printf '%s..%s' "$base" "$tip"
}

ci::git::has_conflict_markers_in_changed() {
  git diff -U0 | grep -E '^\+[[:space:]]*(<{7}|={7}|>{7})([[:space:]]|$)' >/dev/null 2>&1
}
