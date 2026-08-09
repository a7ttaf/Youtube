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

# Everything under frontend/ except the declared tests/ tree is outside-tree, so
# the walk below starts at frontend/ rather than frontend/src — a test dropped in
# any other subdirectory is just as invisible to test.include. These prunes are
# the directories that hold no first-party source.
#
# Build output is pruned by exact path, not by name. An unanchored -name 'build'
# also prunes a first-party frontend/e2e/build/, and a test under it would be
# neither collected by vitest nor reported here — the precise hole this guard
# exists to close. node_modules stays unanchored because nested copies are real
# and are never first-party.
PRUNE=(
  '(' -path "$TESTS_DIR"
  -o -name 'node_modules'
  -o -path "$FRONTEND_DIR/dist"
  -o -path "$FRONTEND_DIR/build"
  -o -path "$FRONTEND_DIR/coverage"
  -o -path "$FRONTEND_DIR/.next"
  -o -path "$FRONTEND_DIR/.turbo"
  -o -path "$FRONTEND_DIR/.vite"
  ')' -prune -o
)

# ---------------------------------------------------------------------------
# 1. No test files may live outside the declared tests/ tree.
#    These would be silently skipped by test.include rather than run.
# ---------------------------------------------------------------------------
# The module suffixes are not padding: vitest's own default glob recognises the
# optional c/m forms, so a foo.test.mts reads as a real test to every tool
# except this config's include.
TEST_SUFFIXES=(
  '(' -name '*.test.ts' -o -name '*.test.tsx' -o -name '*.test.mts' -o -name '*.test.cts'
  -o -name '*.test.js' -o -name '*.test.jsx' -o -name '*.test.mjs' -o -name '*.test.cjs'
  -o -name '*.spec.ts' -o -name '*.spec.tsx' -o -name '*.spec.mts' -o -name '*.spec.cts'
  -o -name '*.spec.js' -o -name '*.spec.jsx' -o -name '*.spec.mjs' -o -name '*.spec.cjs'
  ')'
)

STRAY="$(find "$FRONTEND_DIR" "${PRUNE[@]}" -type f \
  "${TEST_SUFFIXES[@]}" -print 2>/dev/null | sort || true)"

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
# 2. No lingering __tests__ directories outside tests/ (the retired convention).
# ---------------------------------------------------------------------------
LEGACY_DIRS="$(find "$FRONTEND_DIR" "${PRUNE[@]}" -type d -name '__tests__' -print 2>/dev/null | sort || true)"

if [ -n "$LEGACY_DIRS" ]; then
  fail "Retired __tests__ directories still present under ${FRONTEND_DIR}/:"
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
#    Everything test-looking except the two suffixes the glob actually collects.
UNRUNNABLE=""
if [ -d "$TESTS_DIR" ]; then
  UNRUNNABLE="$(find "$TESTS_DIR" -type f \
    "${TEST_SUFFIXES[@]}" \
    ! -name '*.test.ts' ! -name '*.test.tsx' \
    -print 2>/dev/null | sort || true)"
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

# Drops // and /* */ comments so a commented-out include cannot satisfy the
# guard. A bare substring search would accept an include that vitest never sees,
# which is precisely the drift this section exists to catch.
#
# String literals are tracked because the declared glob contains "/*" itself
# (tests/**/*.test...), which a naive scanner reads as a block-comment opener.
strip_ts_comments() {
  awk '
    {
      line = $0
      out = ""
      i = 1
      n = length(line)
      while (i <= n) {
        c = substr(line, i, 1)
        two = substr(line, i, 2)
        if (in_block) {
          if (two == "*/") { in_block = 0; i += 2 } else { i++ }
          continue
        }
        if (in_str != "") {
          out = out c
          if (c == "\\") { out = out substr(line, i + 1, 1); i += 2; continue }
          if (c == in_str) { in_str = "" }
          i++
          continue
        }
        if (two == "//") { break }
        if (two == "/*") { in_block = 1; i += 2; continue }
        if (c == "\"" || c == "'"'"'" || c == "`") { in_str = c }
        out = out c
        i++
      }
      print out
    }
  ' "$1"
}

if [ ! -f "$VITEST_CONFIG" ]; then
  fail "Missing ${VITEST_CONFIG}; cannot confirm the test layout is declared."
else
  # The include must be live config on a single line: an active `include: [...]`
  # carrying the declared glob. Fail closed on anything else — a reformat that
  # this cannot read is drift the guard must surface rather than wave through.
  ACTIVE_INCLUDE="$(strip_ts_comments "$VITEST_CONFIG" | grep -E '^[[:space:]]*include:[[:space:]]*\[' || true)"
  if [ -z "$ACTIVE_INCLUDE" ] || ! printf '%s\n' "$ACTIVE_INCLUDE" | grep -qF "$DECLARED_GLOB"; then
    fail "${VITEST_CONFIG} no longer declares an active include '${DECLARED_GLOB}'."
    echo "  The layout must be declared in config, not left to vitest's default glob,"
    echo "  and it must be live code — a commented-out include does not count."
    echo "  Restore a single-line 'include: [...]' carrying the glob, or update"
    echo "  DECLARED_GLOB in this script to match."
  fi
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
