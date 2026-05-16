#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "ERROR: ship failed at line $LINENO" >&2' ERR

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

DRY_RUN=0
INCLUDE_UNTRACKED=0
AUTO_STAGE_TRACKED=0
AUTO_STAGE_CONFIRM=0
MESSAGE=""

usage() {
  echo "Usage: ./ci/ship.sh [--dry-run] [--include-untracked] [--auto-stage-tracked] [--yes] \"commit message\""
  echo ""
  echo "  --dry-run            Preview actions only."
  echo "  --include-untracked  Stage tracked + untracked files (git add -A)."
  echo "  --auto-stage-tracked Stage tracked changes (git add -u) after explicit confirmation."
  echo "  --yes                Skip confirmation prompt for --auto-stage-tracked."
  exit "${1:-1}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --include-untracked)
      INCLUDE_UNTRACKED=1
      shift
      ;;
    --auto-stage-tracked)
      AUTO_STAGE_TRACKED=1
      shift
      ;;
    --yes)
      AUTO_STAGE_CONFIRM=1
      shift
      ;;
    --help|-h)
      usage 0
      ;;
    *)
      if [ -n "$MESSAGE" ]; then
        echo "Unexpected argument: $1" >&2
        usage 1
      fi
      MESSAGE="$1"
      shift
      ;;
  esac
done

if [ -z "$MESSAGE" ]; then
  echo "Commit message is required."
  usage 1
fi

if [ "$INCLUDE_UNTRACKED" -eq 1 ] && [ "$AUTO_STAGE_TRACKED" -eq 1 ]; then
  echo "Use either --include-untracked or --auto-stage-tracked, not both." >&2
  usage 1
fi

if [ -z "$(printf '%s' "$MESSAGE" | tr -d '[:space:]')" ]; then
  echo "Commit message must not be empty or whitespace-only." >&2
  usage 1
fi

if printf '%s' "$MESSAGE" | grep -qE '[`;&|<>\\]'; then
  echo "Commit message contains forbidden shell characters." >&2
  usage 1
fi

# shellcheck disable=SC2016
if printf '%s' "$MESSAGE" | grep -q '\$('; then
  echo "Commit message contains forbidden shell characters." >&2
  usage 1
fi

# shellcheck disable=SC2016
if printf '%s' "$MESSAGE" | grep -q '\${'; then
  echo "Commit message contains forbidden shell characters." >&2
  usage 1
fi

if [ "$(printf '%s' "$MESSAGE" | wc -l | tr -d '[:space:]')" -gt 0 ]; then
  echo "Commit message must not contain newline characters." >&2
  usage 1
fi

BRANCH="$(git branch --show-current)"
if [ -z "$BRANCH" ]; then
  echo "Refusing to ship from detached HEAD."
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "Refusing to ship: remote 'origin' is not configured."
  exit 1
fi

echo "Running CI ship gate..."
echo "Pre-staging checks are informational only; the authoritative gate runs after staging."
echo ""
echo "Branch: $BRANCH"
echo "Remote: $(git remote get-url origin)"
echo ""
echo "Changed files:"
git diff --name-only
echo ""
echo "Current status:"
git status --short

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "=== DRY RUN ==="
  if [ "$INCLUDE_UNTRACKED" -eq 1 ]; then
    echo "Would run: git add -A"
  elif [ "$AUTO_STAGE_TRACKED" -eq 1 ]; then
    echo "Would run: git status --short"
    echo "Would run: git add -u"
  else
    echo "Would require pre-staged changes (no auto-stage by default)."
  fi
  echo "Would run: ./ci/preflight.sh --mode ship (post-staging)"
  echo "Would run: git commit -m \"$MESSAGE\""
  echo "Would run: git push -u origin $BRANCH"
  exit 0
fi

if [ "$INCLUDE_UNTRACKED" -eq 1 ]; then
  git add -A
elif [ "$AUTO_STAGE_TRACKED" -eq 1 ]; then
  echo ""
  echo "Auto-stage tracked mode requested."
  echo "Current status:"
  git status --short
  if [ "$AUTO_STAGE_CONFIRM" -ne 1 ]; then
    if [ ! -t 0 ]; then
      echo "Non-interactive shell detected. Use --yes to confirm auto-staging."
      exit 1
    fi
    printf "Proceed with 'git add -u'? [y/N]: "
    read -r confirm
    case "$confirm" in
      y|Y|yes|YES)
        ;;
      *)
        echo "Cancelled. Stage files manually or rerun with --yes."
        exit 1
        ;;
    esac
  fi
  git add -u
fi

echo ""
echo "Staged files:"
git diff --cached --name-only

if git diff --cached --quiet; then
  echo "No staged changes found."
  echo "Stage files explicitly (recommended: git add -p / git add <paths>)"
  echo "or rerun with --auto-stage-tracked (tracked only) or --include-untracked."
  exit 1
fi

echo "Re-running ship gate on staged snapshot..."
./ci/preflight.sh --mode ship

git commit -m "$MESSAGE"
git push -u origin "$BRANCH"

echo "Ship completed."
