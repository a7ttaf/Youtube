#!/usr/bin/env bash
set -Eeuo pipefail

trap 'echo "ERROR: impact analysis failed at line $LINENO" >&2' ERR

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT_DIR"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/impact.sh
source "$ROOT_DIR/ci/lib/impact.sh"

rc=0
if ! ci::impact::generate "$@"; then
  rc=$?
fi

exit "$rc"
