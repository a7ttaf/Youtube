#!/usr/bin/env bash
# Runs the JavaScript half of ci/checks/typecheck.sh as a scheduled gate lane.
#
# ci/checks/typecheck.sh dispatches on CI_GATE_CHECK_ID, and preflight's
# run_phase executes a script path directly ("$script") -- a phase entry is
# split on the first `:` into a label and a command, and nothing runs it through
# a shell, so it cannot carry an environment variable of its own. This wrapper
# is that variable. ci/checks/tests-shell.sh exists for the same reason and says
# so in the same words.
#
# Without it the lane falls through to CI_GATE_CHECK_ID's `all` default and runs
# the Python, Go and Rust typecheckers as well -- work the `python` lane beside
# it already owns, scheduled under a label that says `-js`. A lane whose name
# does not describe what it runs is how a result ends up attributed to the wrong
# change.
#
# It exists because typecheck.sh was registered in ci/checks/manifest.yml with
# `severity: blocker` and scheduled by nothing: `typecheck-js` survived only as
# a changeset id that preflight's reverse mapping folded back into the combined
# `node` lane. Registered at one layer and run at none -- the same shape as the
# suites this gate guards, which were themselves unscheduled.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$ROOT_DIR"

export CI_GATE_CHECK_ID=typecheck-js
exec "$ROOT_DIR/ci/checks/typecheck.sh" "$@"
