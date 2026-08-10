#!/usr/bin/env bash
# Runs the bats suites under ci/tests/ as a scheduled gate lane.
#
# ci/checks/tests.sh dispatches on CI_GATE_CHECK_ID, but preflight's run_phase
# executes a script path directly ("$script"), so a phase entry cannot carry an
# environment variable of its own. This wrapper is that variable.
#
# It exists because the suites guarding this gate were themselves unscheduled:
# a change to ci/checks/*.sh emitted only lint-shell and format-shell, so the
# tests covering the layout guard and the node lane ran only when invoked by
# hand — the same "registered but never runs" failure those suites exist to
# catch.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

# The suites run from the worktree, which is the tree this lane is entitled to
# report on only in the pre-commit gate. In ship mode the commit already exists,
# so a gate script, a bats file or .gitignore broken in an outgoing commit and
# repaired only on disk is validated in its repaired form while the broken
# version is pushed. The node lane had the same defect against its own inputs;
# this is the same rule over the inputs these suites actually read.
#
# Scoped to those inputs rather than the whole tree: an unrelated dirty file is
# not something this lane's result claims anything about.
if [ "${CI_GATE_MODE:-}" = "ship" ] \
  && command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
  && git rev-parse --verify HEAD >/dev/null 2>&1; then
  SHELL_DRIFT="$( {
    git diff --name-only HEAD -- ci .githooks .gitignore frontend/README.md 2>/dev/null || true
    git ls-files --others --exclude-standard -- ci .githooks 2>/dev/null || true
    # And the ignored ones. A commit that deletes a bats file and adds its path
    # to .gitignore leaves the worktree replacement invisible to both lists
    # above — HEAD does not carry the path, and --exclude-standard drops
    # exactly this file — so the suites ran the replacement for a commit that
    # removes their own coverage. Nothing under ci/ or .githooks/ is
    # legitimately ignored except the gate's own generated cache.
    git ls-files --others --ignored --exclude-standard -- ci .githooks 2>/dev/null \
      | grep -Ev '(^|/)(\.ci-gate|node_modules)/' || true
  } | sort -u | sed '/^$/d')"
  if [ -n "$SHELL_DRIFT" ]; then
    echo "Gate inputs differ between HEAD and the worktree:"
    while IFS= read -r _p; do
      [ -n "$_p" ] || continue
      echo "    $_p"
    done <<< "$SHELL_DRIFT"
    echo "  These suites read the worktree, so the run would report on files the"
    echo "  pushed commits do not contain. Commit the rest, stash it, or discard it."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
fi

# tests.sh logs "skipped: bats not installed" and returns 0, which is right for
# a generic lane that may run in a repo with no shell tests. It is wrong here:
# this lane exists *because* these suites must run, and `uv sync` does not
# provision bats, so a fresh environment would report PASS having executed
# nothing. An enabled blocker that cannot find its runner is broken
# infrastructure, not a pass.
if ! ci::common::command_exists bats; then
  echo "bats is not installed, so the ci/tests/ suites cannot run."
  echo "  This lane is scheduled as a blocker precisely so those suites execute;"
  echo "  reporting PASS here would mean the layout and node gates are unguarded."
  echo "  Install bats (e.g. 'npm i -g bats', or your platform's package manager)."
  exit "$CI_RESULT_FAIL_INFRA"
fi

exec env CI_GATE_CHECK_ID=tests-shell bash "$SCRIPT_DIR/tests.sh" "$@"
