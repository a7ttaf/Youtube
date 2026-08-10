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

# Candidates are the union of the worktree and the git index. A partially staged
# move leaves the two disagreeing: `git mv`-ing a stray test without staging the
# move leaves frontend/src/probe.test.ts in the index while the worktree shows
# only the moved copy, so a filesystem-only scan blesses a commit that is not
# clean. Checking both is fail-closed in either direction — a stray file that is
# only staged, or only on disk, still fails.
# Note this deliberately does NOT prune the tests tree: section 3 has to see
# inside it to find files the include never collects. Only vendored packages and
# build output are pruned here; which tree a candidate belongs to is decided by
# the predicates below.
CANDIDATE_PRUNE=(
  '(' -name 'node_modules'
  -o -path "$FRONTEND_DIR/dist"
  -o -path "$FRONTEND_DIR/build"
  -o -path "$FRONTEND_DIR/coverage"
  -o -path "$FRONTEND_DIR/.next"
  -o -path "$FRONTEND_DIR/.turbo"
  -o -path "$FRONTEND_DIR/.vite"
  ')' -prune -o
)

candidate_files() {
  {
    find "$FRONTEND_DIR" "${CANDIDATE_PRUNE[@]}" -type f "${TEST_SUFFIXES[@]}" -print 2>/dev/null || true
    if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
      git ls-files -- "$FRONTEND_DIR" 2>/dev/null || true
    fi
  } | sort -u
}

# find applies PRUNE itself; git ls-files does not, so the predicates below are
# the single source of truth for both sources.
path_is_test_like() {
  case "$1" in
    *.test.ts | *.test.tsx | *.test.mts | *.test.cts \
      | *.test.js | *.test.jsx | *.test.mjs | *.test.cjs \
      | *.spec.ts | *.spec.tsx | *.spec.mts | *.spec.cts \
      | *.spec.js | *.spec.jsx | *.spec.mjs | *.spec.cjs) return 0 ;;
  esac
  return 1
}

path_is_pruned() {
  case "$1" in
    */node_modules/*) return 0 ;;
    "$FRONTEND_DIR"/dist/* | "$FRONTEND_DIR"/build/* | "$FRONTEND_DIR"/coverage/* \
      | "$FRONTEND_DIR"/.next/* | "$FRONTEND_DIR"/.turbo/* | "$FRONTEND_DIR"/.vite/*) return 0 ;;
  esac
  return 1
}

ALL_CANDIDATES="$(candidate_files)"

STRAY=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  path_is_test_like "$f" || continue
  path_is_pruned "$f" && continue
  case "$f" in "$TESTS_DIR"/*) continue ;; esac
  STRAY="${STRAY}${f}"$'\n'
done <<< "$ALL_CANDIDATES"
STRAY="$(printf '%s' "$STRAY" | sed '/^$/d')"

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
LEGACY_DIRS="$( {
  find "$FRONTEND_DIR" "${PRUNE[@]}" -type d -name '__tests__' -print 2>/dev/null || true
  # Index side: a staged file under a __tests__/ path names the directory even
  # when the directory no longer exists on disk.
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    path_is_pruned "$f" && continue
    case "$f" in
      */__tests__/*) printf '%s\n' "${f%%/__tests__/*}/__tests__" ;;
    esac
  done <<< "$ALL_CANDIDATES"
} | sort -u | sed '/^$/d')"

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
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in "$TESTS_DIR"/*) ;; *) continue ;; esac
  path_is_test_like "$f" || continue
  case "$f" in *.test.ts | *.test.tsx) continue ;; esac
  UNRUNNABLE="${UNRUNNABLE}${f}"$'\n'
done <<< "$ALL_CANDIDATES"
UNRUNNABLE="$(printf '%s' "$UNRUNNABLE" | sed '/^$/d')"

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

# Extracts the body of the exported config's `test: { ... }` object.
#
# Two things have to be true for the glob to mean anything, and each was a
# separate way this guard failed open. It must be *this* property, not any
# `include:` in the file — an optimizeDeps.include carrying the glob would
# satisfy a file-wide search while test.include ran one file. And it must be the
# *exported* config's property, not the first `test: {` token in the file — a
# helper object declared above defineConfig would shadow the real one.
#
# So: anchor at `export default`, brace-match the object it exports (skipping a
# defineConfig( wrapper), then take the `test` key at that object's top level.
extract_test_block() {
  awk '
    { all = all $0 "\n" }
    END {
      n = length(all)

      ed = index(all, "export default")
      if (ed == 0) exit 1

      # First brace after `export default`, which skips a defineConfig( wrapper.
      p = ed + 14
      while (p <= n && substr(all, p, 1) != "{") p++
      if (p > n) exit 1
      p++

      depth = 1
      while (p <= n && depth > 0) {
        ch = substr(all, p, 1)
        if (ch == "{") { depth++; p++; continue }
        if (ch == "}") { depth--; p++; continue }

        # Only a key at the exported object own level counts.
        if (depth == 1 && substr(all, p, 4) == "test") {
          before = (p > 1) ? substr(all, p - 1, 1) : " "
          rest = substr(all, p + 4)
          if (before !~ /[A-Za-z0-9_$]/ && rest ~ /^[[:space:]]*:[[:space:]]*\{/) {
            q = p + 4 + index(rest, "{")
            d2 = 1
            out = ""
            while (q <= n) {
              c2 = substr(all, q, 1)
              if (c2 == "{") d2++
              else if (c2 == "}") { d2--; if (d2 == 0) break }
              out = out c2
              q++
            }
            print out
            exit 0
          }
        }
        p++
      }

      # No test property on the exported config. Signalled by status, because an
      # empty block is a different failure from a missing one and both print
      # nothing.
      exit 1
    }
  '
}

if [ ! -f "$VITEST_CONFIG" ]; then
  fail "Missing ${VITEST_CONFIG}; cannot confirm the test layout is declared."
else
  # The include must be live config inside the test block, on a single line:
  # an active `include: [...]` carrying the declared glob. Fail closed on
  # anything else — a reformat this cannot read is drift the guard must surface
  # rather than wave through.
  TEST_BLOCK=""
  HAVE_TEST_BLOCK=1
  TEST_BLOCK="$(strip_ts_comments "$VITEST_CONFIG" | extract_test_block)" || HAVE_TEST_BLOCK=0
  ACTIVE_INCLUDE=""
  if [ -n "$TEST_BLOCK" ]; then
    ACTIVE_INCLUDE="$(printf '%s\n' "$TEST_BLOCK" | grep -E '^[[:space:]]*include:[[:space:]]*\[' || true)"
  fi
  # A correct include is not sufficient: test.exclude is applied on top of it,
  # so `exclude: ["tests/lib/**"]` drops 23 of this tree's 38 files while every
  # include assertion above still passes. The guard cannot evaluate an arbitrary
  # exclude pattern against the tree, so it refuses to bless one — vitest's own
  # default exclude already covers node_modules and dist, and an explicit one
  # here means the declared layout is no longer the whole story.
  ACTIVE_EXCLUDE=""
  if [ -n "$TEST_BLOCK" ]; then
    ACTIVE_EXCLUDE="$(printf '%s\n' "$TEST_BLOCK" | grep -E '^[[:space:]]*exclude:' || true)"
  fi

  if [ "$HAVE_TEST_BLOCK" = "0" ]; then
    fail "${VITEST_CONFIG} has no readable 'test: { ... }' block; cannot confirm the layout is declared."
    echo "  The layout must be declared under test.include, where vitest reads it."
  elif [ -z "$ACTIVE_INCLUDE" ] || ! printf '%s\n' "$ACTIVE_INCLUDE" | grep -qF "$DECLARED_GLOB"; then
    fail "${VITEST_CONFIG} no longer declares an active test.include '${DECLARED_GLOB}'."
    echo "  The layout must be declared in config, not left to vitest's default glob;"
    echo "  it must be live code — a commented-out include does not count; and it"
    echo "  must sit inside test: { }, not another section that happens to have an"
    echo "  include field. Restore a single-line 'include: [...]' carrying the glob,"
    echo "  or update DECLARED_GLOB in this script to match."
  elif [ -n "$ACTIVE_EXCLUDE" ]; then
    fail "${VITEST_CONFIG} declares a test.exclude, which this guard cannot verify."
    echo "  exclude is applied on top of include, so a correct '${DECLARED_GLOB}'"
    echo "  can still collect nothing: exclude: [\"tests/lib/**\"] silently drops"
    echo "  every test under that path while this check keeps reporting them."
    echo "  Express the layout with include alone — vitest's default exclude already"
    echo "  covers node_modules and dist. If an exclude is genuinely needed, teach"
    echo "  this guard to evaluate it in the same commit."
    printf '%s\n' "$ACTIVE_EXCLUDE" | sed 's/^/    /'
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
