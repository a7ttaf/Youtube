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

# What the destination holds *now*, asked of the destination.
#
# Two rules below read `refs/remotes/<remote>/*` as a record of what the
# destination contains. It is not: it records what this clone last *saw* it
# containing, and the two differ in the direction that matters. Stale-behind is
# harmless -- fewer refs contain the commit, so the answer is refuse -- but a
# force-push or a branch deletion by another actor leaves this clone holding a
# ref that still contains a commit the destination has dropped. Both rules then
# answer "already published" about a commit nothing on the remote carries, and
# a push republishes it while the content lanes validate the current checkout.
# The comment that used to sit on published_to_destination reasoned about the
# first direction and asserted it as the general property.
#
# So the tips are read from the remote. `git ls-remote --heads` is a query, not
# a fetch: it writes nothing and transfers no objects, and `git push` is about
# to contact the same remote anyway, so the round-trip is one this path can
# afford.
#
# Cached per remote for the length of the process, because published_to_destination
# is called once per tag and once per other-namespace tip.
#
# Failure is not an empty answer. An unreachable remote, a permission error, or
# no `git` at all leaves _CI_GIT_REMOTE_TIPS_RC non-zero, and both callers treat
# that as "cannot prove it is published" rather than as "the destination has
# nothing" -- which is the same rule the rest of this gate applies to a listing
# it could not obtain.
_CI_GIT_REMOTE_TIPS=""
_CI_GIT_REMOTE_TIPS_FOR=""
_CI_GIT_REMOTE_TIPS_RC=1
ci::git::_remote_tips() {
  local remote="$1" _rt_out="" _rt_rc=0
  [ -n "$remote" ] || return 1
  if [ "$_CI_GIT_REMOTE_TIPS_FOR" = "$remote" ]; then
    printf '%s' "$_CI_GIT_REMOTE_TIPS"
    return "$_CI_GIT_REMOTE_TIPS_RC"
  fi
  # Asked once per push, not once per check. Five processes in a ship run
  # resolve a push range -- git-safety, branch-protection, node, the changeset
  # scan and preflight itself -- and a per-process cache is no cache at all
  # across them.
  #
  # The saving is modest and worth stating honestly: `ls-remote` against this
  # repository's origin measures 0.8s, so this removes about 3s from a ship run,
  # not the minute a first guess suggested. The 15.3s that a full push_range
  # actually costs here is the merge-base loop in destination_base -- one
  # subprocess per destination ref, 135 of them -- and that cost is unchanged by
  # this commit: the previous code walked 136 local tracking refs the same way.
  # Measured on both trees rather than assumed.
  #
  # ci/hook-dispatch.sh performs the query once and exports the answer, the same
  # way it already exports the tips and the range. Set-and-empty is a real
  # answer here -- a destination with no refs at all -- so the presence of the
  # variable is what is tested, not its content.
  if [ -n "${CI_GATE_PUSH_REMOTE_TIPS+set}" ] \
    && [ "${CI_GATE_PUSH_REMOTE_TIPS_FOR:-}" = "$remote" ]; then
    _CI_GIT_REMOTE_TIPS_FOR="$remote"
    _CI_GIT_REMOTE_TIPS="$CI_GATE_PUSH_REMOTE_TIPS"
    _CI_GIT_REMOTE_TIPS_RC=0
    printf '%s' "$_CI_GIT_REMOTE_TIPS"
    return 0
  fi
  _CI_GIT_REMOTE_TIPS_FOR="$remote"
  _CI_GIT_REMOTE_TIPS=""
  _CI_GIT_REMOTE_TIPS_RC=1
  # An override for environments that have no network by design -- CI runners
  # behind a proxy, an air-gapped mirror -- so this cannot become an unfixable
  # refusal. Set-and-empty is not set: only an explicit `0` opts out.
  if [ "${CI_GATE_TRUST_TRACKING_REFS:-0}" = "1" ]; then
    _CI_GIT_REMOTE_TIPS="$(git for-each-ref --format='%(objectname)' "refs/remotes/${remote}/" 2>/dev/null || true)"
    _CI_GIT_REMOTE_TIPS_RC=0
    printf '%s' "$_CI_GIT_REMOTE_TIPS"
    return 0
  fi
  command -v git >/dev/null 2>&1 || return 1
  # Every ref on the destination: a commit is published if *any* of them reaches
  # it, and a release tag is as good a publication as a branch.
  #
  # `--heads --tags` was those two namespaces and nothing else, which git's own
  # help states plainly -- "limit to refs/heads" and "limit to refs/tags". A
  # destination that reaches a commit only through a namespace of its own, say
  # `refs/publish/prod`, therefore looked to this function as though it did not
  # have that commit at all, and a tag push on it was judged to be publishing an
  # unvalidated tree. ci::git::worktree_covers_push explicitly models other
  # namespaces, so the query is the half that was narrower than the model.
  #
  # Minus the forge's own pull-request mirrors, and that exclusion is the part
  # worth being careful about, because it is the only fail-open direction here.
  # `refs/pull/*` (GitHub), `refs/merge-requests/*` (GitLab) and
  # `refs/changes/*` (Gerrit) are refs the forge writes for *proposed* changes;
  # a commit reachable only from one of them has not been merged anywhere.
  # Gerrit is the sharpest of the three, and arrived a round after the other
  # two: there *every* change lives in that namespace until it is submitted, so
  # on a Gerrit remote the omission was not an edge case but the normal state of
  # unmerged work. Counting any of them as published would let
  # ci::git::push_is_label_only call a tag push on an unmerged fork head
  # content-free and skip every content lane. Merged work is unaffected -- it is
  # an ancestor of a branch tip, and the loop above tests ancestry, not
  # equality.
  #
  # Filtered on the ref name rather than after taking the object name, since the
  # object name alone cannot say which namespace it came from. Kept identical to
  # ci/hook-dispatch.sh's copy of this query; `hook: the destination tips query
  # matches the one git.sh falls back to` is the case that says so.
  #
  # And it costs nothing here, which was worth measuring rather than assuming:
  # this repository's origin answers 329 refs unfiltered against 136 for
  # `--heads --tags`, and after the exclusion above both produce the same 136
  # object names -- 193 of the extra refs are `refs/pull/*`. destination_base
  # runs one merge-base per tip, so an unfiltered widening would have tripled the
  # slowest part of a ship run; this one leaves it where it was.
  _rt_out="$(git ls-remote "$remote" 2>/dev/null)" || _rt_rc=$?
  if [ "$_rt_rc" -ne 0 ]; then
    return 1
  fi
  _CI_GIT_REMOTE_TIPS="$(printf '%s\n' "$_rt_out" | awk '$2 !~ /^refs\/(pull|merge-requests|changes)\// { print $1 }' | sed '/^$/d' | sort -u)"
  _CI_GIT_REMOTE_TIPS_RC=0
  printf '%s' "$_CI_GIT_REMOTE_TIPS"
  return 0
}

# Whether the destination, as it is right now, reaches this commit.
#
# A tip this clone does not carry cannot be walked, and `merge-base
# --is-ancestor` says so by failing -- which is the fail-closed answer and the
# right one: a commit whose relationship to the destination cannot be computed
# is not a commit that has been proven to be on it.
ci::git::_reachable_from_destination() {
  local sha="$1" remote="$2" _rf_tips _rf_tip
  [ -n "$sha" ] || return 1
  _rf_tips="$(ci::git::_remote_tips "$remote")" || return 1
  [ -n "$_rf_tips" ] || return 1
  while IFS= read -r _rf_tip; do
    [ -n "$_rf_tip" ] || continue
    [ "$_rf_tip" = "$sha" ] && return 0
    git merge-base --is-ancestor "$sha" "$_rf_tip" 2>/dev/null && return 0
  done <<< "$_rf_tips"
  return 1
}

# The newest commit on the way to <tip> that the destination is known to hold,
# or nothing when that cannot be established.
#
# "Already on the destination" is not "already gated somewhere": the answer is
# read only from the named destination, never from every remote at once.
#
# The newest of the per-ref merge bases, because that is the one that excludes
# the most while excluding only commits the destination has. Two refs whose
# bases are incomparable -- neither an ancestor of the other -- leave the first
# in place, which scans more than strictly necessary and never less.
#
# The paragraph that used to end this comment claimed staleness was safe here
# because "the record is behind reality, so the range is wider". That holds for
# one direction of staleness and was written as though it held for both: a
# force-push or a deletion leaves the record *ahead* of reality, and the range
# is then narrower than the truth. The source is the remote itself now, and the
# claim is gone rather than qualified.
ci::git::destination_base() {
  local tip="$1" remote="${CI_GATE_PUSH_REMOTE:-}" _db_ref _db_mb _db_best=""
  [ -n "$remote" ] || return 0
  [ -n "$tip" ] || return 0
  # The destination's current tips, not this clone's memory of them. A base
  # taken from a stale `refs/remotes/<remote>/*` narrows the range past commits
  # the destination no longer has, and every history check -- git-safety, the
  # signature walk, the changeset scan -- then skips exactly the commits this
  # push is about to make reachable again. Empty on failure, and empty here
  # means "no base", which sends push_range to the whole tip: more work, and
  # the safe direction.
  local _db_tips
  _db_tips="$(ci::git::_remote_tips "$remote")" || return 0
  [ -n "$_db_tips" ] || return 0
  while IFS= read -r _db_ref; do
    [ -n "$_db_ref" ] || continue
    _db_mb="$(git merge-base "$tip" "$_db_ref" 2>/dev/null || true)"
    [ -n "$_db_mb" ] || continue
    if [ -z "$_db_best" ]; then
      _db_best="$_db_mb"
    elif git merge-base --is-ancestor "$_db_best" "$_db_mb" 2>/dev/null; then
      _db_best="$_db_mb"
    fi
  done <<< "$_db_tips"
  printf '%s' "$_db_best"
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

  # A tag can be the only thing carrying a commit out.
  #
  # ci/hook-dispatch.sh leaves the scalar tip unset for a push that names no
  # branch, deliberately: a tag on an unrelated lineage must not displace the
  # branch tip, and what may go out under a tag is decided per ref by
  # ci::git::worktree_covers_push. It exports the per-ref tip lists instead.
  # This function read neither of them, so a tag-only push fell through to the
  # `@{upstream}` guesses below -- and for a tag at a HEAD the checkout's
  # upstream already contains, `merge-base HEAD @{upstream}` is HEAD and the
  # range is `HEAD..HEAD`. Zero commits walked by git-safety, the signature
  # check and the linear-history check, while the tag is publishing that commit
  # to a destination that may not have it.
  #
  # Here rather than in the dispatcher, which was the first attempt and was
  # wrong: seeding the scalar there broke `hook: only a branch record moves the
  # collapsed history tip` and `hook: only a branch destination has to fit the
  # one range`, and turned two unpublished tags on separate lineages into a hard
  # refusal of `git push origin v1.0 v2.0`. The dispatcher is answering the
  # branch question correctly; this function is the one that owes the history
  # checks a range, and it is the only reader of that question.
  #
  # Collapsed by ancestry, the same rule the dispatcher applies to branch tips.
  # Tips containing neither each other cannot be written as one `A..B`, because
  # every caller passes this string to `git log` or `git rev-list` as a single
  # argument and git has no one-token spelling for the union of two disjoint
  # ranges.
  #
  # That case used to fall through to the `@{upstream}` guesses below, which is
  # the worst of the three things it could do: `new_sha` is unset for a tag-only
  # push, so `tip` became HEAD and the history checks were handed a range with
  # no relationship to the refs being pushed -- non-empty, so git-safety's
  # "cannot determine the range" guard did not fire, and it reported on the
  # checked-out branch instead. A confident answer about the wrong commits.
  #
  # Refused now, and the refusal is narrowed to the pushes that actually need
  # it: a tip the destination already carries publishes no commits, so it can be
  # neither the range nor a reason there is no single range. Dropping those
  # first is what keeps `git push origin v1.0 v2.0` -- two tags on commits that
  # are already upstream, the ordinary release shape -- from being refused for a
  # split it does not have. What is left over is two or more lineages of
  # genuinely unpublished commits, and there is no honest single range for that.
  if [ -z "${CI_GATE_PUSH_NEW_SHA:-}" ] \
    && [ -n "${CI_GATE_PUSH_TAG_TIPS:-}${CI_GATE_PUSH_OTHER_TIPS:-}" ]; then
    # Two questions over the same list, and they are kept apart because the
    # answers come from different subsets. `_pr_best` is the tip to measure and
    # is collapsed over every tip, published or not -- a push of nothing but
    # already-published tags still has to resolve to that tag's empty range
    # rather than falling through to HEAD. `_pr_split` is whether a single range
    # can express the push at all, and only unpublished tips can make it false.
    local _pr_tip _pr_best="" _pr_split=0 _pr_res _pr_db _pr_new=""
    # Word splitting is the point: these are space-separated object names.
    # shellcheck disable=SC2086
    for _pr_tip in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
      [ -n "$_pr_tip" ] || continue
      _pr_res="$(git rev-parse --verify "${_pr_tip}^{commit}" 2>/dev/null)" || continue
      if [ -z "$_pr_best" ]; then
        _pr_best="$_pr_tip"
      elif git merge-base --is-ancestor "$_pr_best" "$_pr_tip" 2>/dev/null; then
        _pr_best="$_pr_tip"
      fi

      _pr_db="$(ci::git::destination_base "$_pr_tip")"
      # Already published: nothing goes out under this one, so it is not a
      # lineage this push has to scan and cannot be a reason there is no range.
      [ -n "$_pr_db" ] && [ "$_pr_db" = "$_pr_res" ] && continue
      if [ -z "$_pr_new" ]; then
        _pr_new="$_pr_tip"
      elif git merge-base --is-ancestor "$_pr_new" "$_pr_tip" 2>/dev/null; then
        _pr_new="$_pr_tip"
      elif git merge-base --is-ancestor "$_pr_tip" "$_pr_new" 2>/dev/null; then
        : # already the descendant; keep it
      else
        _pr_split=1
      fi
    done
    if [ "$_pr_split" -eq 1 ]; then
      # Nothing on stdout, so no caller can mistake this for a range, and a
      # status of its own so "there is no push to measure" stays distinguishable
      # from "this push cannot be measured". Those two were one condition, and
      # the checks that read it draw opposite conclusions from them.
      echo "ci::git::push_range: this push publishes commits on more than one" >&2
      echo "  lineage, and they cannot be expressed as a single commit range." >&2
      echo "  Push the refs separately so each one can be scanned." >&2
      return 3
    fi
    # Measured over the tip that is publishing something, and `_pr_best` only
    # when nothing is.
    #
    # `_pr_best` collapses over *every* tip, published or not, and it collapses
    # by ancestry -- so a push listing an already-published tag before an
    # unpublished one on an unrelated lineage kept the published tag, because
    # neither is the other's ancestor. The range came out `published..published`:
    # empty, and non-empty as a *string*, so git-safety's "cannot determine the
    # range" guard did not fire and it walked zero commits. The signature and
    # linear-history checks read the same range and walked zero too, while the
    # unpublished tagged history went out unexamined. The content lanes still
    # ran against HEAD, which is what kept it from being visible.
    #
    # `_pr_new` is already the right tip and was already computed: it is the
    # collapse over the *unpublished* tips alone, and `_pr_split` above has
    # refused the case where there is more than one lineage of them. Falling
    # back to `_pr_best` keeps a push of nothing but published tags resolving to
    # that tag's empty range -- the true answer for it, and the one
    # ci::git::push_is_label_only routes past the content lanes separately.
    local _pr_pick="${_pr_new:-$_pr_best}"
    if [ -n "$_pr_pick" ]; then
      # Same base as a branch tip gets: what the destination actually holds,
      # asked of the destination.
      base="$(ci::git::destination_base "$_pr_pick")"
      if [ -n "$base" ]; then
        printf '%s..%s' "$base" "$_pr_pick"
      else
        printf '%s' "$_pr_pick"
      fi
      return 0
    fi
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
# Asked of the destination, not of this clone's memory of it.
#
# This walked `git branch -r --contains`, whose `-r` filters local
# remote-tracking records. Those are a snapshot from the last fetch, and the way
# they go wrong is not symmetric: stale-behind refuses, which is safe, but a
# force-push or a branch deletion by another actor leaves a tracking ref still
# containing a commit the destination has dropped -- and this then reports it as
# already gated. A tag pushed on that commit republishes it while the content
# lanes validate the current checkout, so the one tree nothing ever looked at is
# the one being made reachable.
#
# ci::git::_reachable_from_destination asks the remote instead, and fails closed
# when it cannot: no remote name, no answer from `ls-remote`, or a tip this
# clone cannot walk all mean "not proven published", which is a refusal with a
# followable remedy rather than a silent pass.
ci::git::published_to_destination() {
  local sha="$1" remote="${CI_GATE_PUSH_REMOTE:-}"
  [ -n "$remote" ] || return 1
  [ -n "$sha" ] || return 1
  ci::git::_reachable_from_destination "$sha" "$remote"
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
# Whether this push adds a label and no content.
#
# A push whose every record is a tag (or another non-branch ref) pointing at a
# commit the destination already carries publishes a name, nothing else: there
# is no tree going out that a content lane could report on. ci/preflight.sh had
# one content-free path and it was keyed on deletions alone, so an ordinary
# `git tag v1.2 <published commit>; git push origin v1.2` ran test-layout, the
# node lane, python, the build and the shell suites against whatever happened to
# be checked out -- and any pre-existing failure there blocked a release that
# sends none of that tree. The half-hour the shell suites take was spent on it
# too.
#
# Deliberately narrower than worktree_covers_push, which also returns 0 for an
# ordinary branch push whose tip is the checkout. That push *is* content and its
# lanes must run. The two conditions here are what make this one different:
#
#   - the push names no branch destination at all. Set-and-empty is the hook
#     saying it read the records and none targets refs/heads/*; unset means
#     nobody told us, and then this cannot be concluded.
#   - every tag and other-namespace tip is already on the destination. A tag on
#     an *unpublished* commit -- including one on HEAD -- carries that commit out
#     with it, and the lanes over HEAD are exactly the right thing to run.
#
# published_to_destination asks the remote and fails closed, so an unreachable
# destination yields "not label-only" and the full plan runs. More work, and the
# safe direction.
ci::git::push_is_label_only() {
  [ -n "${CI_GATE_PUSH_REMOTE_REFS+set}" ] || return 1
  [ -z "${CI_GATE_PUSH_REMOTE_REFS}" ] || return 1
  # A deletion-only push is content-free for its own reason and has its own
  # path; this rule is about what a push *publishes*.
  [ "${CI_GATE_PUSH_DELETIONS_ONLY:-0}" = "1" ] && return 1

  local _lo_tip _lo_res _lo_seen=0
  # Word-splitting is how the lists are carried; the shas cannot hold whitespace.
  # shellcheck disable=SC2086
  for _lo_tip in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
    _lo_seen=1
    _lo_res="$(git rev-parse --verify "${_lo_tip}^{commit}" 2>/dev/null || true)"
    # An unreadable tip is not evidence of anything, as everywhere else here.
    [ -n "$_lo_res" ] || return 1
    ci::git::published_to_destination "$_lo_res" || return 1
  done
  # No records at all is not a label-only push; it is a push this function
  # cannot describe, and the caller's full plan is the right answer.
  [ "$_lo_seen" -eq 1 ] || return 1
  return 0
}

ci::git::worktree_covers_push() {
  local tip="${CI_GATE_PUSH_NEW_SHA:-}"
  # "Nobody said" is every list being empty, not the scalar being empty.
  #
  # The scalar is the collapsed *branch* tip, and the dispatcher deliberately
  # leaves it unset for a push that names no branch -- a tag-only push, or one
  # into a namespace this gate has no model of -- while exporting the tip lists
  # instead. Returning on the scalar alone therefore skipped both per-ref loops
  # below for exactly the pushes they exist to judge: `git push origin v1`, with
  # `v1` on an older failing commit and its repair checked out, was covered by a
  # function that never looked at the tag. That hole opened when the tip collapse
  # was restricted to branch records, which was itself the right fix -- one range
  # is the branch question -- so this is the other half of it.
  if [ -z "$tip" ] \
     && [ -z "${CI_GATE_PUSH_BRANCH_TIPS:-}" ] \
     && [ -z "${CI_GATE_PUSH_TAG_TIPS:-}" ] \
     && [ -z "${CI_GATE_PUSH_OTHER_TIPS:-}" ]; then
    return 0
  fi
  local pushed head
  head="$(git rev-parse --verify "HEAD^{commit}" 2>/dev/null || true)"
  [ -n "$head" ] || return 1
  pushed=""
  if [ -n "$tip" ]; then
    pushed="$(git rev-parse --verify "${tip}^{commit}" 2>/dev/null || true)"
    # A tip that cannot be resolved is not evidence that it matches. Refusing on
    # an unreadable sha is the same fail-closed direction as everywhere else.
    [ -n "$pushed" ] || return 1
  fi

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
  # For a while there was a third answer here -- carried by a branch in this
  # same push -- and it is gone; the paragraph below the loop says why, and the
  # short version is that it assumed the branch update lands.
  #
  # And every destination in a namespace this gate has no model of, by the same
  # test. Gerrit's refs/for/*, a refs/publish/* deployment pointer,
  # refs/meta/config: the hook recognised refs/heads/* and refs/tags/* and
  # nothing else, so such a record contributed to no list at all. `git push
  # origin feature <older>:refs/publish/prod` had the content lanes vouch for
  # HEAD while the second ref moved to a tree nothing in the run had read.
  # refs/notes/* is the one namespace deliberately left out of this, upstream in
  # the hook: a notes commit carries annotations rather than project source.
  #
  # The "carried by a branch in this same push" arm is gone, and the argument
  # for it is why. It said the objects under the tag go to the remote as part of
  # the branch's history whether the tag exists or not -- true only if the
  # branch update actually lands. `git push -h` documents `--atomic` as
  # requesting "atomic transaction on remote side", which means a push is *not*
  # atomic by default: the server can reject the branch and accept the tag, and
  # then a release pointer sits on a tree nothing validated with no branch
  # having carried it anywhere. Nothing tells a pre-push hook whether `--atomic`
  # was passed, so the condition the arm depended on cannot be checked here, and
  # a rule that cannot be checked is not a rule this gate keeps.
  #
  # The cost is real and worth stating: `git push origin main v1.0`, with the
  # tag on a commit main is itself sending, now needs two commands -- push the
  # branch, then push the tag, at which point the commit is published and the
  # tag carries no content. That is a followable remedy, which is the test this
  # gate applies to a refusal.
  local _wc_tag
  # shellcheck disable=SC2086
  for _wc_tag in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
    _wc_res="$(git rev-parse --verify "${_wc_tag}^{commit}" 2>/dev/null || true)"
    [ -n "$_wc_res" ] || return 1
    [ "$_wc_res" = "$head" ] && continue
    ci::git::published_to_destination "$_wc_res" || return 1
  done

  # A push that names no branch at all has been answered entirely by the loops
  # above: every tag and every other-namespace tip is the checkout or is already
  # on the destination. There is no scalar tip to compare, and comparing an
  # empty one against HEAD refused a push that had just passed every test that
  # applies to it.
  [ -n "$pushed" ] || return 0

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
  # commit the destination already carries went out as part of a branch push and
  # was gated then, so the tag adds a label and no content -- there is nothing
  # here for a content lane to vouch for. A tag on a commit the destination does
  # not have is carrying that commit out with it, and that is a tree nothing has
  # ever checked.
  #
  # "The destination carries it" is asked of the destination, not of this
  # clone's remote-tracking refs. The sentence that used to sit here said
  # staleness only made the test stricter; that is true of one direction of
  # staleness and false of the other, and the false one is the direction a
  # force-push produces. published_to_destination now queries the remote and
  # fails closed when it cannot.
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
  # The same test the rule applies, and only that test. It carried a
  # "carried by a branch in this push" arm while the rule did, and both are gone
  # for the same reason: a push is not atomic unless `--atomic` is asked for, a
  # pre-push hook cannot see whether it was, and a branch the server rejects
  # carries nothing. A message that keeps a condition the rule has dropped
  # explains a refusal that did not happen.
  local _d_head _d_tag _d_res _d_bad=""
  _d_head="$(git rev-parse --verify "HEAD^{commit}" 2>/dev/null || true)"
  # shellcheck disable=SC2086
  for _d_tag in ${CI_GATE_PUSH_TAG_TIPS:-} ${CI_GATE_PUSH_OTHER_TIPS:-}; do
    _d_res="$(git rev-parse --verify "${_d_tag}^{commit}" 2>/dev/null || true)"
    if [ -z "$_d_res" ]; then _d_bad="${_d_bad} ${_d_tag}"; continue; fi
    [ "$_d_res" = "$_d_head" ] && continue
    ci::git::published_to_destination "$_d_res" && continue
    _d_bad="${_d_bad} ${_d_res}"
  done
  if [ -n "$_d_bad" ]; then
    echo "This push publishes a tag on a commit nothing here has vouched for:"
    # shellcheck disable=SC2086
    for _d_tag in ${_d_bad}; do
      echo "    ${_d_tag}"
    done
    echo "  It is not the checkout and the destination does not already have it,"
    echo "  so the tag is what takes that commit out, and its tree has never been"
    echo "  run by a content lane. A branch in this same push does not settle it:"
    echo "  a push is not atomic unless you ask for it, so the server can reject"
    echo "  the branch and accept the tag."
    echo "  Push the branch that contains it first — the commit is then already"
    echo "  on the destination and the tag carries no content — or check that"
    echo "  commit out and push the tag from there."
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
