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
# The newest commit on the way to <tip> that the destination remote is already
# known to hold, or nothing when there is no such record.
#
# "Already on the destination" is not "already gated somewhere": the answer is
# read only from refs/remotes/<destination>/, never from every remote at once.
#
# The newest of the per-ref merge bases, because that is the one that excludes
# the most while excluding only commits the remote has. Two refs whose bases are
# incomparable -- neither an ancestor of the other -- leave the first in place,
# which scans more than strictly necessary and never less.
#
# Remote-tracking refs can be stale, and stale here means the record is behind
# reality: the base is older than the truth, so the range is wider. That is the
# safe direction, and it is the same direction the rest of this file accepts
# from the same source.
ci::git::destination_base() {
  local tip="$1" remote="${CI_GATE_PUSH_REMOTE:-}" _db_ref _db_mb _db_best=""
  [ -n "$remote" ] || return 0
  [ -n "$tip" ] || return 0
  while IFS= read -r _db_ref; do
    [ -n "$_db_ref" ] || continue
    _db_mb="$(git merge-base "$tip" "$_db_ref" 2>/dev/null || true)"
    [ -n "$_db_mb" ] || continue
    if [ -z "$_db_best" ]; then
      _db_best="$_db_mb"
    elif git merge-base --is-ancestor "$_db_best" "$_db_mb" 2>/dev/null; then
      _db_best="$_db_mb"
    fi
  done <<< "$(git for-each-ref --format='%(refname)' "refs/remotes/${remote}/" 2>/dev/null || true)"
  printf '%s' "$_db_best"
}

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

  # Past that point, a caller that supplied a tip gets the tip and nothing
  # guessed around it.
  #
  # The fallbacks below exist for callers who said nothing at all -- CI, a
  # direct invocation -- and they were being applied to the pre-push hook as
  # well, which does say. The hook leaves the base unset precisely when the
  # destination has none of this history: a new branch, or a remote sha this
  # clone does not carry. Filling that in from `@{upstream}` or a local `main`
  # then produced `main..tip` for a first push to an empty destination, and
  # git-safety, the signature check and the changeset scan all skipped every
  # commit up to `main`. A secret in the omitted history uploads past a gate
  # reporting on the tail of the branch.
  #
  # Which is the same reasoning as the `HEAD~1` fallback that was removed from
  # this list: a wrong base is worse than no base, because it produces a
  # confident green over commits nobody looked at.
  #
  # With one exception, and it is the difference between a guess and a record.
  # `@{upstream}` and a bare local `main` are guesses about where this branch
  # belongs. A remote-tracking ref of the *destination* is a record of what that
  # destination was last seen holding, which is the same source
  # ci::git::published_to_destination is trusted with one function down. Without
  # it the first push of any branch walked its whole history: in this repository
  # that is 439 commits instead of the handful being uploaded, and twelve
  # long-published commits fail the whitespace scan -- so `git push -u origin
  # <new-branch>` was refused, with no remedy available to the person pushing.
  #
  # Scoped to the named destination for the reason published_to_destination is:
  # a commit that exists only on `upstream` says nothing about a push to
  # `origin`. No name means no record, and no record means the whole tip.
  if [ -n "${CI_GATE_PUSH_NEW_SHA:-}" ]; then
    git rev-parse --verify "${new_sha}^{commit}" >/dev/null 2>&1 || return 1
    base="$(ci::git::destination_base "$new_sha")"
    if [ -n "$base" ]; then
      printf '%s..%s' "$base" "$new_sha"
    else
      printf '%s' "$new_sha"
    fi
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

# Whether a commit is already on the remote this push names.
#
# Its own function because two rules ask it now: a tag pushed on its own, and a
# tag pushed alongside a branch. The second was answered by not asking.
#
# Published *to the destination*, not published anywhere: `git branch -r
# --contains` walks every remote-tracking branch, so a commit that exists only
# on `upstream` counted as already gated when the push is going to `origin`.
# Without a remote name there is nothing to scope to, and answering from every
# remote is the bug -- so no name is a refusal.
#
# Compared as text, not as a pattern. `grep "^${remote}/"` interpolates the
# remote name into an expression, so an ordinary name containing a `.` --
# `release.prod` -- matched `releaseXprod/main`. A `-`, `+` or `[` does the same
# thing in its own way.
#
# Remote-tracking refs can be stale, and stale makes this stricter rather than
# looser: fewer refs contain the commit, so the answer is refuse.
ci::git::published_to_destination() {
  local sha="$1" remote="${CI_GATE_PUSH_REMOTE:-}" _pd_line _pd_pfx
  [ -n "$remote" ] || return 1
  [ -n "$sha" ] || return 1
  _pd_pfx="${remote}/"
  while IFS= read -r _pd_line; do
    _pd_line="${_pd_line#"${_pd_line%%[![:space:]]*}"}"
    [ -n "$_pd_line" ] || continue
    if [ "${_pd_line:0:${#_pd_pfx}}" = "$_pd_pfx" ]; then
      return 0
    fi
  done <<< "$(git branch -r --contains "$sha" 2>/dev/null || true)"
  return 1
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

  # And every tag tip is the checkout or is already on the destination.
  #
  # Tags were excluded from this on the grounds that a tag in a mixed push names
  # a commit inside the history being pushed. True, and not the whole of it:
  # `git push origin main v1`, where `v1` labels a failing commit and `main` is
  # its repair, had the branch tip equal HEAD and returned before looking. The
  # content lanes then validated the repaired tree while a release pointer went
  # out at the broken one. It is the same question the tag-only rule below asks,
  # and there was no reason for the mixed push to be exempt from it.
  #
  # Or carried by a branch in this same push, which is the third answer and the
  # one the rule above was missing. `git push origin main v1.0` with the tag on
  # a commit main is itself sending was refused: not the checkout, not yet on
  # the destination, and the drift message then printed the pushed sha and HEAD
  # as the same value with a remedy that was already true. The objects under
  # that tag are going to the remote as part of main's history whether the tag
  # exists or not, so the tag adds a label and no content -- which is the same
  # thing the "already published" arm says about a commit that went out earlier.
  #
  # It is not the same as "an ancestor of HEAD", and that difference is the
  # whole of the protection: a tag-only push carries no branch tips, so this arm
  # is empty for it and the tag must still meet the published test below. A tag
  # on a commit no pushed branch contains is carrying that commit out with it.
  #
  # And every destination in a namespace this gate has no model of, by the same
  # test. Gerrit's refs/for/*, a refs/publish/* deployment pointer,
  # refs/meta/config: the hook recognised refs/heads/* and refs/tags/* and
  # nothing else, so such a record contributed to no list at all. `git push
  # origin feature <older>:refs/publish/prod` had the content lanes vouch for
  # HEAD while the second ref moved to a tree nothing in the run had read.
  # refs/notes/* is the one namespace deliberately left out of this, upstream in
  # the hook: a notes commit carries annotations rather than project source.
  local _wc_tag _wc_carried
  # shellcheck disable=SC2086
  for _wc_tag in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
    _wc_res="$(git rev-parse --verify "${_wc_tag}^{commit}" 2>/dev/null || true)"
    [ -n "$_wc_res" ] || return 1
    [ "$_wc_res" = "$head" ] && continue
    _wc_carried=0
    # shellcheck disable=SC2086
    for _wc_tip in ${CI_GATE_PUSH_BRANCH_TIPS:-}; do
      if git merge-base --is-ancestor "$_wc_res" "$_wc_tip" 2>/dev/null; then
        _wc_carried=1
        break
      fi
    done
    [ "$_wc_carried" -eq 1 ] && continue
    ci::git::published_to_destination "$_wc_res" || return 1
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
    ci::git::published_to_destination "$pushed" && return 0
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
  # A tag is its own sentence too. The generic message below compares the
  # collapsed tip against HEAD, and in a mixed push those are the same commit --
  # so a refusal caused by a tag printed two identical shas and told the reader
  # to check out the commit they already had.
  local _d_head _d_tag _d_res _d_bad="" _d_carried _d_btip
  _d_head="$(git rev-parse --verify "HEAD^{commit}" 2>/dev/null || true)"
  # shellcheck disable=SC2086
  for _d_tag in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
    _d_res="$(git rev-parse --verify "${_d_tag}^{commit}" 2>/dev/null || true)"
    if [ -z "$_d_res" ]; then _d_bad="${_d_bad} ${_d_tag}"; continue; fi
    [ "$_d_res" = "$_d_head" ] && continue
    _d_carried=0
    # shellcheck disable=SC2086
    for _d_btip in ${CI_GATE_PUSH_BRANCH_TIPS:-}; do
      git merge-base --is-ancestor "$_d_res" "$_d_btip" 2>/dev/null && { _d_carried=1; break; }
    done
    [ "$_d_carried" -eq 1 ] && continue
    ci::git::published_to_destination "$_d_res" && continue
    _d_bad="${_d_bad} ${_d_res}"
  done
  if [ -n "$_d_bad" ]; then
    echo "This push publishes a tag on a commit nothing here has vouched for:"
    # shellcheck disable=SC2086
    for _d_tag in ${_d_bad}; do
      echo "    ${_d_tag}"
    done
    echo "  It is not the checkout, no branch in this push carries it, and the"
    echo "  destination does not already have it — so the tag is what takes that"
    echo "  commit out, and its tree has never been run by a content lane."
    echo "  Push the branch that contains it first, or check that commit out and"
    echo "  push the tag from there."
    return 0
  fi
  echo "The commit being pushed is not the commit checked out."
  echo "  pushed: ${tip}"
  echo "  HEAD:   $(git rev-parse --verify HEAD 2>/dev/null || echo '<none>')"
  echo "  Every check that runs content reads the worktree, so this run would"
  echo "  report on the branch you are standing on and not on the one going"
  echo "  out — a pass here would vouch for a tree nobody is pushing."
  echo "  Check out the commit being pushed and push again, or push the branch"
  echo "  you have checked out."
}
