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

exec env CI_GATE_CHECK_ID=tests-shell bash "$SCRIPT_DIR/tests.sh" "$@"
