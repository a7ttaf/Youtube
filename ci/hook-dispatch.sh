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
      _push_old="" _push_new=""
      while read -r _lref _lsha _rref _rsha; do
        [ -n "${_lsha:-}" ] || continue
        # A deletion pushes the zero sha as the local one; there is no content.
        case "$_lsha" in *[!0]*) ;; *) continue ;; esac
        _push_new="$_lsha"
        case "${_rsha:-}" in
          *[!0]*)
            if [ -z "$_push_old" ] \
              || git merge-base --is-ancestor "$_rsha" "$_push_old" 2>/dev/null; then
              _push_old="$_rsha"
            fi
            ;;
          *) _push_old="" ; break ;;
        esac
      done
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
