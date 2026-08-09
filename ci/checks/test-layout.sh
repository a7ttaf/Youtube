#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"

cd "$ROOT_DIR"

ci::common::section "Check: frontend test layout"

FRONTEND_DIR="frontend"
TESTS_DIR="$FRONTEND_DIR/tests"
SRC_DIR="$FRONTEND_DIR/src"
VITEST_CONFIG="$FRONTEND_DIR/vitest.config.ts"

# The single declared layout. Kept in lockstep with test.include in
# frontend/vitest.config.ts; the drift check below fails if they diverge.
DECLARED_GLOB='tests/**/*.test.{ts,tsx}'

if [ ! -d "$FRONTEND_DIR" ]; then
  ci::log::info "skipped: no frontend/ directory"
  exit "$CI_RESULT_PASS"
fi

RESULT="$CI_RESULT_PASS"

fail() {
  RESULT="$CI_RESULT_FAIL_NEW_ISSUE"
  ci::log::error "$1"
}

# ---------------------------------------------------------------------------
# 1. No test files may live outside the declared tests/ tree.
#    These would be silently skipped by test.include rather than run.
# ---------------------------------------------------------------------------
STRAY=""
if [ -d "$SRC_DIR" ]; then
  STRAY="$(find "$SRC_DIR" -type f \
    \( -name '*.test.ts' -o -name '*.test.tsx' \
    -o -name '*.test.js' -o -name '*.test.jsx' \
    -o -name '*.spec.ts' -o -name '*.spec.tsx' \
    -o -name '*.spec.js' -o -name '*.spec.jsx' \) 2>/dev/null | sort || true)"
fi

if [ -n "$STRAY" ]; then
  fail "Test files found outside ${TESTS_DIR}/. vitest include is '${DECLARED_GLOB}', so these would NEVER RUN:"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    echo "  $f"
  done <<< "$STRAY"
  echo ""
  echo "  Move each to ${TESTS_DIR}/, mirroring src/ without the __tests__ segment:"
  echo "    frontend/src/lib/api/__tests__/useThing.test.tsx"
  echo "      -> frontend/tests/lib/api/useThing.test.tsx"
fi

# ---------------------------------------------------------------------------
# 2. No lingering __tests__ directories under src/ (the retired convention).
# ---------------------------------------------------------------------------
LEGACY_DIRS=""
if [ -d "$SRC_DIR" ]; then
  LEGACY_DIRS="$(find "$SRC_DIR" -type d -name '__tests__' 2>/dev/null | sort || true)"
fi

if [ -n "$LEGACY_DIRS" ]; then
  fail "Retired __tests__ directories still present under ${SRC_DIR}/:"
  while IFS= read -r d; do
    [ -z "$d" ] && continue
    echo "  $d"
  done <<< "$LEGACY_DIRS"
fi

# ---------------------------------------------------------------------------
# 3. Inside tests/, every test-looking file must actually match the declared
#    glob. A tests/foo.spec.tsx sits in the right tree but is never collected —
#    this is the silent-skip failure mode the guard exists to catch.
# ---------------------------------------------------------------------------
UNRUNNABLE=""
if [ -d "$TESTS_DIR" ]; then
  UNRUNNABLE="$(find "$TESTS_DIR" -type f \
    \( -name '*.test.js' -o -name '*.test.jsx' \
    -o -name '*.spec.ts' -o -name '*.spec.tsx' \
    -o -name '*.spec.js' -o -name '*.spec.jsx' \) 2>/dev/null | sort || true)"
fi

if [ -n "$UNRUNNABLE" ]; then
  fail "Files under ${TESTS_DIR}/ that do not match '${DECLARED_GLOB}' and are therefore silently skipped:"
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    echo "  $f"
  done <<< "$UNRUNNABLE"
  echo ""
  echo "  Rename to *.test.ts / *.test.tsx, or widen test.include in ${VITEST_CONFIG}"
  echo "  and DECLARED_GLOB in this script together."
fi

# ---------------------------------------------------------------------------
# 4. Drift guard: the layout must stay declared in vitest.config.ts. If the
#    include is dropped, vitest silently falls back to its default glob and
#    this whole check stops meaning anything.
# ---------------------------------------------------------------------------
if [ ! -f "$VITEST_CONFIG" ]; then
  fail "Missing ${VITEST_CONFIG}; cannot confirm the test layout is declared."
elif ! grep -qF "$DECLARED_GLOB" "$VITEST_CONFIG"; then
  fail "${VITEST_CONFIG} no longer declares include '${DECLARED_GLOB}'."
  echo "  The layout must be declared in config, not left to vitest's default glob."
  echo "  Restore the include, or update DECLARED_GLOB in this script to match."
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if [ "$RESULT" = "$CI_RESULT_PASS" ]; then
  TEST_COUNT=0
  if [ -d "$TESTS_DIR" ]; then
    TEST_COUNT="$(find "$TESTS_DIR" -type f \( -name '*.test.ts' -o -name '*.test.tsx' \) 2>/dev/null | wc -l | tr -d '[:space:]')"
  fi
  ci::log::info "Test layout OK: ${TEST_COUNT} file(s), all under ${TESTS_DIR}/ matching '${DECLARED_GLOB}'."
  exit "$CI_RESULT_PASS"
fi

ci::log::error "Frontend test layout check failed."
exit "$RESULT"
