#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$ROOT_DIR/ci/lib/common.sh"

ci::common::section "Check: flaky test detection"

FLAKY_RUNS="${CI_GATE_FLAKY_RUNS:-3}"
echo "Running affected tests ${FLAKY_RUNS} times to detect flakes..."

# Run tests multiple times, capture failing test names
# TODO: Wire this to each project's real test runner before treating it as pass.
echo "Flaky detection is not implemented; reporting known debt instead of PASS."
exit "$CI_RESULT_PASS_WITH_KNOWN_DEBT"
