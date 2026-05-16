#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "ERROR: test plan generation failed at line $LINENO" >&2' ERR

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/impact.sh
source "$ROOT_DIR/ci/lib/impact.sh"
# shellcheck source=ci/lib/test-plan.sh
source "$ROOT_DIR/ci/lib/test-plan.sh"

if ci::test_plan::generate "$@"; then
  rc=0
else
  rc=$?
fi

exit "$rc"
