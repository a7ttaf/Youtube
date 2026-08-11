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
      _push_old="" _push_new="" _push_nobase=0 _push_unrelated=""
      while read -r _lref _lsha _rref _rsha; do
        [ -n "${_lsha:-}" ] || continue
        # A deletion pushes the zero sha as the local one; there is no content.
        case "$_lsha" in *[!0]*) ;; *) continue ;; esac

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
            if [ -z "$_push_old" ]; then
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
