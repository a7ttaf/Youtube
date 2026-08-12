#!/usr/bin/env bash
# Single dispatcher for all git hooks.
# All hooks exec this script with the hook type as first argument.
set -Eeuo pipefail

HOOK_NAME="${1:-unknown}"
shift || true

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ci/lib/common.sh" 2>/dev/null || true
source "$ROOT_DIR/ci/lib/log.sh" 2>/dev/null || true

case "$HOOK_NAME" in
  pre-commit)
    export CI_GATE_HOOK=pre-commit
    export CI_GATE_CHANGED_FILES
    CI_GATE_CHANGED_FILES="$(git diff --cached --name-only 2>/dev/null || true)"
    exec "$ROOT_DIR/ci/preflight.sh" --mode quick "$@"
    ;;
  commit-msg)
    COMMIT_MSG_FILE="${1:-}"
    if [ -z "$COMMIT_MSG_FILE" ] || [ ! -f "$COMMIT_MSG_FILE" ]; then
      exit 0
    fi
    export CI_GATE_HOOK=commit-msg
    exec "$ROOT_DIR/ci/checks/commit-hygiene.sh" "$COMMIT_MSG_FILE"
    ;;
  pre-push)
    export CI_GATE_HOOK=pre-push
    # The destination remote *name*, which is what scopes the tag-publication
    # check: without it that check asked whether any remote-tracking branch
    # contained the commit, so a commit published only to `upstream` counted as
    # published when pushing to `origin`.
    #
    # `$1`, and the offset is worth stating because I got it wrong once here.
    # git hands the pre-push hook `<remote-name> <remote-url>` and
    # .githooks/pre-push prepends the hook name, but this script consumes that
    # with `shift` above -- so by here `$1` is the remote name and `$2` is the
    # URL. Exporting `$2` put the URL in, `${remote}/` then matched no
    # remote-tracking branch, and every tag push was refused. Fail-closed, and
    # still wrong. Asserted by `hooks: the pre-push dispatcher exports the
    # remote name, not the URL`, which drives the real hook rather than reading
    # this comment.
    export CI_GATE_PUSH_REMOTE="${1:-}"
    # git feeds a pre-push hook one `<local ref> <local sha> <remote ref>
    # <remote sha>` record per ref on stdin. Nothing read it, so every ship-mode
    # check fell back to deriving a range from the checked-out HEAD — and
    # `git push origin some-other-branch` scanned the branch you happen to be
    # standing on instead of the one being pushed. Arbitrary commits went out
    # past a gate that reported on something else entirely.
    #
    # The gate validates one range per run, so the records are collapsed to the
    # widest one: the oldest remote sha still known locally as the base, the
    # newest local sha as the tip. A ref whose remote sha is all zeros is a new
    # branch with no base, which push_range already handles by walking all of
    # HEAD — and here that means leaving the base empty so it can.
    if [ ! -t 0 ]; then
      _push_old="" _push_new="" _push_nobase=0 _push_unrelated="" _push_dests="" _push_any_content=0
      _push_btips="" _push_ttips="" _push_records=0
      while read -r _lref _lsha _rref _rsha; do
        [ -n "${_lsha:-}" ] || continue
        # Counted separately from content, because "every record was a deletion"
        # and "there were no records" are different statements and only one of
        # them describes a push. See where the flag is exported below.
        _push_records=$((_push_records + 1))

        # The *destination* is collected before anything else, because a push
        # names where it is going and the gate was reading where we happen to
        # be standing. `git push origin feature:main` sends this hook
        # `refs/heads/main` as the remote ref while HEAD is still `feature`,
        # and branch-protection.sh -- asking `git rev-parse --abbrev-ref HEAD`
        # -- approved it as an ordinary feature-branch push. `git push origin
        # HEAD:main` does the same. The refspec is documented in `git push -h`
        # and is not exotic.
        #
        # Collected for deletions too, above the `continue` below: `git push
        # origin :main` deletes a protected branch outright, and that is the
        # one push that must not be waved through for having no content.
        case "${_rref:-}" in
          refs/heads/*) _push_dests="${_push_dests} ${_rref#refs/heads/}" ;;
        esac

        # A deletion pushes the zero sha as the local one; there is no content.
        #
        # Counted, not merely skipped. `git push --delete origin feature` is
        # every record being a deletion, so nothing set _push_new -- and
        # ci::git::push_range defaults a missing new sha to HEAD, which handed
        # git-safety and the signature and linear-history checks the branch that
        # happens to be checked out. Deleting an unrelated ref could then be
        # blocked by commits that are not being pushed anywhere.
        _push_has_content=1
        case "$_lsha" in *[!0]*) ;; *) _push_has_content=0 ;; esac
        if [ "$_push_has_content" -eq 0 ]; then
          continue
        fi
        _push_any_content=1

        # Every commit a branch destination is being moved to, distinctly.
        #
        # Ancestry is the right relation for the *history* checks -- one A..B
        # range covers a chain -- and the wrong one for everything that reads
        # content. The node lane, the build and test-layout run against the
        # worktree, once, so they can vouch for exactly one tree; collapsing an
        # ancestor/descendant pair to the descendant left the other branch
        # receiving a tree nothing validated. `git push origin broken:staging
        # fixed:main`, where the repair descends from the break, gated `fixed`
        # and moved `staging` to the commit whose suite fails.
        #
        # Collected here and judged by ci::git::worktree_covers_push, which is
        # where "can this worktree stand in for what is being pushed" already
        # lives. The range this hook builds still has to be widest-chain and
        # order-independent for the history checks, so the collector does not
        # refuse anything.
        #
        # Only content-bearing refs: a deletion has no tip to validate.
        case "${_rref:-}" in
          refs/heads/*)
            case " ${_push_btips} " in
              *" ${_lsha} "*) ;;
              *) _push_btips="${_push_btips} ${_lsha}" ;;
            esac
            ;;
          # Tags are collected too, and separately, because the question asked
          # of them is a different one.
          #
          # They were left out on the grounds that a tag in a mixed push names a
          # commit inside the history being pushed. That is true and it is not
          # the whole of it: `git push origin main v1`, where `v1` labels a
          # failing commit and `main` is its repair, has the content lanes
          # validate the repaired tree and publishes a release pointer at the
          # broken one. The branch tip equalling HEAD made the check return
          # before it ever looked. What a tag needs is the rule this gate
          # already applies to a tag pushed on its own -- it is the checkout, or
          # its commit is already on the destination.
          refs/tags/*)
            case " ${_push_ttips} " in
              *" ${_lsha} "*) ;;
              *) _push_ttips="${_push_ttips} ${_lsha}" ;;
            esac
            ;;
        esac

        # The tip is chosen by ancestry, not by arrival. `_push_new="$_lsha"`
        # on every record meant "whichever ref git happened to list last",
        # while the base beside it was already being widened properly — so the
        # two halves of the same range disagreed about which push they
        # described, and the answer changed when git reordered its input.
        if [ -z "$_push_new" ]; then
          _push_new="$_lsha"
        elif git merge-base --is-ancestor "$_push_new" "$_lsha" 2>/dev/null; then
          _push_new="$_lsha"
        elif git merge-base --is-ancestor "$_lsha" "$_push_new" 2>/dev/null; then
          : # already the descendant; keep it
        else
          # Neither contains the other. One `A..B` range cannot describe two
          # unrelated histories, and picking either one leaves the other
          # unscanned — which is the fail-open this gate exists to remove.
          _push_unrelated="${_push_unrelated} ${_lref:-<ref>}"
        fi

        case "${_rsha:-}" in
          *[!0]*)
            # The base is widened only along one chain. Two remote tips that are
            # each other's ancestors collapse to the older; two that are not do
            # not collapse at all, and keeping whichever arrived first makes the
            # range order-dependent again — `A0..tip` then re-walks everything
            # reachable from the discarded `B0`, so the gate can block a push on
            # a secret, an unsigned commit or a merge the remote already has.
            # Reported rather than picked, for the same reason as the tip.
            #
            # A remote sha this repository does not have is not a divergence.
            # `merge-base --is-ancestor` exits 128 for an unknown object and 1
            # for "genuinely not contained", and both were read as the latter,
            # so a base that had merely never been fetched -- the remote moved,
            # or this is a shallow or partial clone -- collapsed into
            # _push_unrelated and hard-refused the push. It is the same
            # statement as a new branch: no base is available locally, so
            # nothing narrows the range and the run walks all of HEAD.
            if ! git rev-parse --verify --quiet "${_rsha}^{commit}" >/dev/null 2>&1; then
              _push_nobase=1
            elif [ -z "$_push_old" ]; then
              _push_old="$_rsha"
            elif git merge-base --is-ancestor "$_rsha" "$_push_old" 2>/dev/null; then
              _push_old="$_rsha"
            elif git merge-base --is-ancestor "$_push_old" "$_rsha" 2>/dev/null; then
              : # already the older of the two; keep it
            else
              _push_unrelated="${_push_unrelated} ${_rref:-<remote ref>}"
            fi
            ;;
          # A new branch has no base. Recorded rather than `break`-ed: breaking
          # stopped reading stdin, so any ref listed after it was never seen at
          # all and the tip was decided by however git ordered the records.
          *) _push_nobase=1 ;;
        esac
      done

      if [ -n "$_push_unrelated" ]; then
        # "Unrelated histories" was the wrong words for the test performed. What
        # is checked is whether one ref contains the other, and two branches
        # forked from a shared base fail that while having a perfectly good
        # merge base — so the old message sent people looking for a rootless
        # history that is not there. Refusing is still right; the diagnostic has
        # to describe the actual condition.
        echo "pre-push: refusing to gate a push of refs that do not form one chain." >&2
        echo "  Not contained by the ref already selected:${_push_unrelated}" >&2
        echo "  These may well share a merge base — the point is that neither" >&2
        echo "  contains the other, so no single A..B range covers both, and" >&2
        echo "  collapsing them would either skip commits or re-scan commits the" >&2
        echo "  remote already has. Push the refs separately so each is gated." >&2
        exit 1
      fi
      # Any ref without a base means the push carries history the remote has
      # never seen, so there is no common base for the run as a whole.
      [ "$_push_nobase" -eq 1 ] && _push_old=""
      # Exported even when empty, because empty and unset are different
      # statements and only one of them may fall back to HEAD. Set-and-empty
      # means the hook read stdin and this push names no branch destination at
      # all -- pushing a tag, say -- and there is then no branch to protect;
      # falling back to the checkout there would refuse `git push origin v1.2`
      # for the sole reason that you were standing on main. Unset means nobody
      # said, which is every caller that is not this hook.
      export CI_GATE_PUSH_REMOTE_REFS="${_push_dests# }"
      export CI_GATE_PUSH_BRANCH_TIPS="${_push_btips# }"
      export CI_GATE_PUSH_TAG_TIPS="${_push_ttips# }"
      # A push whose every record is a deletion carries no content, and there is
      # nothing for a content or history check to report on. Stated explicitly
      # rather than left as "no new sha", because that is indistinguishable from
      # "nobody told us" and resolves to HEAD -- the checked-out branch, which
      # is not being pushed anywhere. Destination protection still runs off
      # CI_GATE_PUSH_REMOTE_REFS above: deleting `main` is exactly the push that
      # must still be refused.
      #
      # `_push_records -gt 0` is the whole of the difference between a statement
      # and a shrug. `_push_any_content` starts at 0 and only rises inside the
      # loop, so reading no records at all produced the identical flag -- and
      # since that flag now selects the entire ship plan, a hook invoked with
      # nothing on stdin reported PASS having run one lane, which then announced
      # that it did not apply. git really does run this hook with zero records
      # (`git push` with nothing to send: "Everything up-to-date"), so failing
      # here would refuse an ordinary no-op push; but a hook harness that does
      # not forward the ref list gives the same silence while git still sends
      # the refs. The two cannot be told apart from here, so the empty case does
      # not narrow anything: it is "I could not look", and the full ship set is
      # what this gate does when it does not know.
      if [ "$_push_records" -gt 0 ] && [ "$_push_any_content" -eq 0 ]; then
        export CI_GATE_PUSH_DELETIONS_ONLY=1
      elif [ "$_push_records" -eq 0 ]; then
        echo "pre-push: no ref records on stdin." >&2
        echo "  git sends none when there is nothing to push, and a hook runner" >&2
        echo "  that does not forward them sends none either. Gating the tree" >&2
        echo "  rather than assuming the first." >&2
      fi
      if [ -n "$_push_new" ]; then
        export CI_GATE_PUSH_NEW_SHA="$_push_new"
        [ -n "$_push_old" ] && export CI_GATE_PUSH_OLD_SHA="$_push_old"
      fi
    fi
    exec "$ROOT_DIR/ci/preflight.sh" --mode ship
    ;;
  prepare-commit-msg)
    COMMIT_MSG_FILE="${1:-}"
    COMMIT_SOURCE="${2:-}"
    BRANCH="$(git branch --show-current 2>/dev/null || true)"

    if [ -z "$COMMIT_MSG_FILE" ] || [ ! -f "$COMMIT_MSG_FILE" ]; then
      exit 0
    fi

    case "$COMMIT_SOURCE" in
      merge|squash|commit) exit 0 ;;
    esac

    ticket=""
    case "$BRANCH" in
      feat/*|fix/*|chore/*)
        ticket_candidate="${BRANCH#*/}"
        case "$ticket_candidate" in
          [A-Z]*-[0-9]*)
            ticket="$(printf '%s\n' "$ticket_candidate" | grep -Eo '^[A-Z][A-Z0-9]*-[0-9]+' || true)"
            ;;
        esac
        ;;
    esac

    if [ -n "$ticket" ]; then
      first_line="$(head -1 "$COMMIT_MSG_FILE")"
      conventional_subject_re='^([a-z]+(\([^)]*\))?):[[:space:]]*(.*)$'
      case "$first_line" in
        *"$ticket"*) ;;
        *)
          if printf '%s\n' "$first_line" | grep -qE "$conventional_subject_re"; then
            subject_type="$(printf '%s\n' "$first_line" | sed -E "s/$conventional_subject_re/\\1/")"
            subject_text="$(printf '%s\n' "$first_line" | sed -E "s/$conventional_subject_re/\\3/")"
            printf '%s: %s %s\n' "$subject_type" "$ticket" "$subject_text" > "${COMMIT_MSG_FILE}.tmp"
            tail -n +2 "$COMMIT_MSG_FILE" >> "${COMMIT_MSG_FILE}.tmp"
            mv "${COMMIT_MSG_FILE}.tmp" "$COMMIT_MSG_FILE"
          fi
          ;;
      esac
    fi
    exit 0
    ;;
  *)
    echo "hook-dispatch: unknown hook: $HOOK_NAME" >&2
    exit 0
    ;;
esac
