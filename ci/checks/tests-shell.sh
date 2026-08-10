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
