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

# ci::git::worktree_covers_push – 0 when the worktree can stand in for what is
# being pushed.
#
# Named for the question rather than for one way of answering it. The first
# version asked "is the pushed tip the checkout", which is the common case and
# not the whole of it: a tag pointing at an ancestor of HEAD is content the
# worktree already contains, and refusing that failed `git tag v1.0 <older
# commit>; git push origin v1.0` -- an ordinary release workflow -- on a gate
# whose whole point is to not block correct work.
#
# Ranges are resolved from the hook's SHAs, so the *history* checks read the
# right commits. Everything that runs content — the node lane, the test-layout
# guard, the suites, the build — reads the worktree instead, and there is only
# one of those. `git push origin other-branch` therefore gated the branch you
# happen to be standing on: a passing checkout vouching for an outgoing branch
# whose tests fail, reported as a clean push. That is the confident green this
# gate exists to remove, and it cannot be fixed by looking harder at the wrong
# tree.
#
# Answered here rather than in each lane so the lanes cannot drift apart about
# it — the last several rounds were all one rule agreeing with its counterpart
# on one axis and not another.
#
# Unset means nobody said, which is CI and every direct invocation: those run
# against whatever is checked out by design, and there is no second tree to be
# wrong about.
ci::git::worktree_covers_push() {
  local tip="${CI_GATE_PUSH_NEW_SHA:-}"
  [ -n "$tip" ] || return 0
  local pushed head
  pushed="$(git rev-parse --verify "${tip}^{commit}" 2>/dev/null || true)"
  head="$(git rev-parse --verify "HEAD^{commit}" 2>/dev/null || true)"
  # A tip that cannot be resolved is not evidence that it matches. Refusing on
  # an unreadable sha is the same fail-closed direction as everywhere else.
  [ -n "$pushed" ] || return 1
  [ -n "$head" ] || return 1

  # *Every* branch destination, not just the widest of them.
  #
  # The hook collapses the records to one A..B range, which is right for the
  # history checks -- one chain, one range. It is wrong for everything that
  # reads content, because there is one worktree and it can only be one of the
  # tips. `git push origin broken:staging fixed:main`, where the repair
  # descends from the break, collapsed to `fixed`: the lanes validated that
  # tree, reported a pass, and `staging` was left at the commit whose suite
  # fails. Ancestry proves the range covers the chain; it proves nothing about
  # the ancestor's tree having been checked.
  #
  # Unset is the same statement as everywhere else here -- nobody said, so there
  # is no second tree to be wrong about, and CI keeps working.
  local _wc_tip _wc_res
  # Word-splitting is how the list is carried; the shas cannot hold whitespace.
  # shellcheck disable=SC2086
  for _wc_tip in ${CI_GATE_PUSH_BRANCH_TIPS:-}; do
    _wc_res="$(git rev-parse --verify "${_wc_tip}^{commit}" 2>/dev/null || true)"
    # An unreadable tip is not evidence that it matches, as above.
    [ -n "$_wc_res" ] || return 1
    [ "$_wc_res" = "$head" ] || return 1
  done

  [ "$pushed" = "$head" ] && return 0

  # A push that names no branch destination at all is a tag push. Two reviewers
  # pulled this in opposite directions and both were right about their own
  # failure, so the rule is narrower than either.
  #
  # Refusing every tag push blocks `git tag v1.0 <older commit>; git push origin
  # v1.0`, an ordinary release workflow, and failed the whole ship gate on it.
  # But "the tag is an ancestor of HEAD" is not enough either: a failing commit
  # can be tagged, repaired in a descendant, and the tag pushed while the lanes
  # validate the repaired HEAD. Ancestry says the worktree contains the tagged
  # commit's history; it says nothing about the tagged *tree* having been
  # checked.
  #
  # What settles it is whether the tagged commit has already been published. A
  # commit contained in a remote-tracking branch went out as part of a branch
  # push and was gated then, so the tag adds a label and no content -- there is
  # nothing here for a content lane to vouch for. A tag on a commit no remote
  # branch contains is carrying that commit out with it, and that is a tree
  # nothing has ever checked.
  #
  # Remote-tracking refs can be stale, and stale makes this stricter rather than
  # looser: fewer refs contain the commit, so the answer is refuse.
  #
  # Set-and-empty, not unset: unset means nobody told us and there is no second
  # tree to be wrong about, which the early return above already handles.
  if [ -n "${CI_GATE_PUSH_REMOTE_REFS+set}" ] && [ -z "${CI_GATE_PUSH_REMOTE_REFS}" ]; then
    # Published *to the destination*, not published anywhere.
    #
    # `git branch -r --contains` walks every remote-tracking branch, so a
    # commit that exists only on `upstream` counted as already gated when the
    # push is going to `origin` -- and the tag then carries a tree origin has
    # never seen, while the lanes report on the repaired HEAD. The remote is
    # the destination this push named.
    #
    # Without a remote name there is nothing to scope to, and answering from
    # every remote is the bug. Refuse instead: a tag push outside the pre-push
    # hook has no destination this can verify.
    local contained remote="${CI_GATE_PUSH_REMOTE:-}"
    [ -n "$remote" ] || return 1
    contained="$(git branch -r --contains "$pushed" 2>/dev/null \
      | sed 's/^[[:space:]]*//' \
      | grep "^${remote}/" | head -1 || true)"
    [ -n "$contained" ] && return 0
  fi
  return 1
}

# The message, in one place, because two callers print it.
ci::git::explain_push_tip_drift() {
  local tip="${CI_GATE_PUSH_NEW_SHA:-<unknown>}"
  # More than one branch tip is a different sentence from ordinary drift, and
  # printing the collapsed one alone sends people to compare a sha that does
  # match HEAD against a HEAD that does.
  case "${CI_GATE_PUSH_BRANCH_TIPS:-}" in
    *' '*)
      local _d_tip
      echo "This push updates branches to more than one commit:"
      # shellcheck disable=SC2086
      for _d_tip in ${CI_GATE_PUSH_BRANCH_TIPS}; do
        echo "    ${_d_tip}"
      done
      echo "  Everything that runs content — the node lane, the build, the"
      echo "  test-layout guard — reads the worktree, and there is one of those,"
      echo "  so it can vouch for one of these commits and not the other. That"
      echo "  holds even when they are ancestor and descendant: the history"
      echo "  checks cover the chain, the content checks cover one end of it."
      echo "  Push the branches separately so each is gated against its own tree."
      return 0
      ;;
  esac
  echo "The commit being pushed is not the commit checked out."
  echo "  pushed: ${tip}"
  echo "  HEAD:   $(git rev-parse --verify HEAD 2>/dev/null || echo '<none>')"
  echo "  Every check that runs content reads the worktree, so this run would"
  echo "  report on the branch you are standing on and not on the one going"
  echo "  out — a pass here would vouch for a tree nobody is pushing."
  echo "  Check out the commit being pushed and push again, or push the branch"
  echo "  you have checked out."
}
