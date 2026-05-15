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
            ticket="$ticket_candidate"
            ;;
        esac
        ;;
    esac

    if [ -n "$ticket" ]; then
      first_line="$(head -1 "$COMMIT_MSG_FILE")"
      case "$first_line" in
        "$ticket"*) ;;
        *)
          printf '%s: %s\n' "$ticket" "$first_line" > "${COMMIT_MSG_FILE}.tmp"
          tail -n +2 "$COMMIT_MSG_FILE" >> "${COMMIT_MSG_FILE}.tmp"
          mv "${COMMIT_MSG_FILE}.tmp" "$COMMIT_MSG_FILE"
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
