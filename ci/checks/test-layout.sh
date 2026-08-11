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
      # And HEAD, in ship mode. The union above is worktree plus index, which
      # describes the pre-commit gate; the pre-push gate stands behind HEAD, and
      # this file had no notion of that at all. Remove a stray test from the
      # index and the worktree without committing the removal, and the guard
      # reported "Test layout OK" for a push whose tree still carries it —
      # while node.sh, on the identical state, reported drift. Two checks, one
      # file, opposite answers, and this is the one on the always-run list.
      if [ "${CI_GATE_MODE:-}" = "ship" ] && git rev-parse --verify HEAD >/dev/null 2>&1; then
        git ls-tree -r --name-only HEAD -- "$FRONTEND_DIR" 2>/dev/null || true
      fi
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
#
# String literals are opaque to the scan. A brace-and-token match that reads
# through them takes a decoy inside a template literal for configuration and
# stops there, and the declared glob itself contains braces
# (`tests/**/*.test.{ts,tsx}`), so an unbalanced one in any string would throw
# the depth count off. A quoted *key* is still a key, so a string is inspected
# for that before being skipped.
extract_test_block() {
  awk '
    # Index just past the closing quote of the string opening at i.
    function string_end(str, i, n,   q, c) {
      q = substr(str, i, 1)
      i++
      while (i <= n) {
        c = substr(str, i, 1)
        if (c == "\\") { i += 2; continue }
        if (c == q) { return i + 1 }
        i++
      }
      return n + 1
    }
    function is_quote(c) {
      return (c == "\"" || c == "'"'"'" || c == "`")
    }
    # The last character before i that is not whitespace.
    function prev_signif(str, i,   j, c) {
      j = i - 1
      while (j >= 1) {
        c = substr(str, j, 1)
        if (c !~ /[[:space:]]/) return c
        j--
      }
      return ""
    }
    # Whether the "/" at i opens a regex literal rather than dividing. Comments
    # are already stripped, so those are the only two possibilities, and the
    # usual test applies: a regex can only appear where a value can, so the
    # preceding token decides. `const mention = /export default/;` was read as
    # ordinary text, and the anchor landed inside it.
    # Whether the ")" immediately before i closes an `if (...)` / `while (...)` /
    # `for (...)` head. Walks back to the matching "(" and reads the word before
    # it. Anything else -- the arguments of a call, a parenthesised expression
    # -- is a value, so a slash after it divides.
    function _closes_control(str, i,   j, depth, c, w) {
      j = i - 1
      while (j >= 1 && substr(str, j, 1) ~ /[[:space:]]/) j--
      if (substr(str, j, 1) != ")") return 0
      depth = 0
      while (j >= 1) {
        c = substr(str, j, 1)
        if (c == ")") depth++
        else if (c == "(") { depth--; if (depth == 0) break }
        j--
      }
      if (j < 1) return 0
      j--
      while (j >= 1 && substr(str, j, 1) ~ /[[:space:]]/) j--
      w = ""
      while (j >= 1 && substr(str, j, 1) ~ /[A-Za-z0-9_$]/) {
        w = substr(str, j, 1) w
        j--
      }
      return (w ~ /^(if|while|for|switch|catch|with)$/) ? 1 : 0
    }
    function is_regex_start(str, i,   c, w, j, depth) {
      c = prev_signif(str, i)
      if (c == "") return 1
      # A closing parenthesis is the ambiguous one. `(a + b) / c` divides;
      # `if (cond) /re/.test(x)` does not, because that parenthesis closes a
      # condition of a control statement rather than an expression. Match it back
      # and look at the keyword in front of it.
      if (c == ")") return _closes_control(str, i)
      if (c !~ /[A-Za-z0-9_$\]]/) return 1
      # An identifier character does not settle it: `return /x/` ends in the "n"
      # of a keyword, and a keyword is a position where a value may start. Read
      # the whole word back and check it, or `function f() { return /export
      # default/ }` classifies the slash as division and the anchor lands inside
      # the regex.
      if (c !~ /[A-Za-z_$]/) return 0
      w = ""
      j = i - 1
      while (j >= 1 && substr(str, j, 1) ~ /[[:space:]]/) j--
      while (j >= 1 && substr(str, j, 1) ~ /[A-Za-z0-9_$]/) {
        w = substr(str, j, 1) w
        j--
      }
      return (w ~ /^(return|typeof|instanceof|in|of|new|delete|void|case|do|else|yield|await|throw)$/) ? 1 : 0
    }
    # Index just past the closing "/" of the regex opening at i. A "/" inside a
    # character class does not close it; an unterminated one is not a regex at
    # all, so the line ends the skip rather than swallowing the file.
    function regex_end(str, i, n,   c, incls) {
      i++
      incls = 0
      while (i <= n) {
        c = substr(str, i, 1)
        if (c == "\\") { i += 2; continue }
        if (c == sprintf("%c", 10)) { return i }
        if (c == "[") { incls = 1; i++; continue }
        if (c == "]") { incls = 0; i++; continue }
        if (c == "/" && !incls) { return i + 1 }
        i++
      }
      return n + 1
    }
    { all = all $0 "\n" }
    END {
      n = length(all)

      # The anchor has to be found the same way everything after it is read.
      # A raw index() matched `export default` inside a string literal, so a
      # decoy in any quoted text anchored the whole extraction there -- the same
      # class the scan below was already string-aware for, one step earlier.
      ed = 0
      for (i = 1; i <= n; i++) {
        c = substr(all, i, 1)
        if (is_quote(c)) { i = string_end(all, i, n) - 1; continue }
        if (c == "/" && is_regex_start(all, i)) { i = regex_end(all, i, n) - 1; continue }
        if (substr(all, i, 14) == "export default") { ed = i; break }
      }
      if (ed == 0) exit 1

      # First brace after `export default`, which skips a defineConfig( wrapper.
      p = ed + 14
      while (p <= n) {
        ch = substr(all, p, 1)
        if (is_quote(ch)) { p = string_end(all, p, n); continue }
        if (ch == "/" && is_regex_start(all, p)) { p = regex_end(all, p, n); continue }
        if (ch == "{") break
        p++
      }
      if (p > n) exit 1
      p++

      depth = 1
      while (p <= n && depth > 0) {
        ch = substr(all, p, 1)

        if (is_quote(ch)) {
          e = string_end(all, p, n)
          # `"test": { }` is the same key as `test: { }`. Everything else in a
          # string is data, however much it looks like config.
          if (depth == 1 && substr(all, p + 1, e - p - 2) == "test") {
            rest = substr(all, e)
            if (rest ~ /^[[:space:]]*:[[:space:]]*\{/) {
              start = e + index(rest, "{")
              print capture(all, start, n)
              exit 0
            }
          }
          p = e
          continue
        }

        if (ch == "/" && is_regex_start(all, p)) { p = regex_end(all, p, n); continue }

        if (ch == "{") { depth++; p++; continue }
        if (ch == "}") { depth--; p++; continue }

        # A computed key at the exported object own level. `["test"]: {...}` is
        # applied by JavaScript exactly like `test:`, and applied *later* if it
        # comes later -- but the quoted token is skipped by the string handling
        # above, so the scan counted only the plain block and reported a broad
        # include as live while vitest collected the narrowed one. This reader
        # works on text and cannot evaluate a computed key in general, so any
        # computed property at this level stops it rather than being guessed at.
        if (depth == 1 && ch == "[") {
          computed = 1
        }

        # Only a key at the exported object own level counts.
        if (depth == 1 && substr(all, p, 4) == "test") {
          before = (p > 1) ? substr(all, p - 1, 1) : " "
          rest = substr(all, p + 4)
          if (before !~ /[A-Za-z0-9_$]/ && rest ~ /^[[:space:]]*:[[:space:]]*\{/) {
            start = p + 4 + index(rest, "{")
            # Every one of them, not the first. Returning on the first meant a
            # broad `test.include` followed by a second `test: { include:
            # ["tests/only.test.ts"] }` was validated on the broad one, which
            # reported every file runnable and exited 0 -- while JavaScript
            # keeps the *later* property and vitest collected the one file.
            seen++
            block = capture(all, start, n)
          }
        }
        p++
      }

      # Duplicates are refused rather than resolved. Taking the last one would
      # match the language, but a config with two `test` keys is a mistake
      # whichever one wins, and guessing which was meant is how the first
      # reading of this got it backwards.
      if (seen > 1) exit 2
      # A computed property can be `["test"]`, and there is no way to tell from
      # the text which one it is. Reported as the duplicate case, because the
      # remedy is the same: say it plainly in one `test: { ... }` block.
      if (computed) exit 2
      if (seen == 1) { print block; exit 0 }

      # No test property on the exported config. Signalled by status, because an
      # empty block is a different failure from a missing one and both print
      # nothing.
      exit 1
    }
    # Body of the object whose opening brace sits just before `q`. Strings are
    # copied through verbatim -- the include patterns live in them -- but their
    # braces do not move the depth.
    function capture(str, q, n,   d2, out, c2, e2) {
      d2 = 1
      out = ""
      while (q <= n) {
        c2 = substr(str, q, 1)
        if (is_quote(c2)) {
          e2 = string_end(str, q, n)
          out = out substr(str, q, e2 - q)
          q = e2
          continue
        }
        if (c2 == "/" && is_regex_start(str, q)) {
          e2 = regex_end(str, q, n)
          out = out substr(str, q, e2 - q)
          q = e2
          continue
        }
        if (c2 == "{") d2++
        else if (c2 == "}") { d2--; if (d2 == 0) break }
        out = out c2
        q++
      }
      return out
    }
  '
}

# The config has to be read the way the file list is: from what git will commit.
# Staging a narrowed test.include and then restoring the correct config in the
# worktree leaves a commit that collects almost nothing while a worktree-only
# read reports the layout declared. Both copies are checked, so a bad include in
# either fails.
config_sources() {
  printf '%s\n' "$VITEST_CONFIG"
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    if git cat-file -e ":$VITEST_CONFIG" 2>/dev/null; then
      local staged
      staged="$(mktemp)"
      if git show ":$VITEST_CONFIG" > "$staged" 2>/dev/null; then
        printf '%s\n' "$staged"
      else
        rm -f "$staged"
      fi
    fi
  fi
}

# A config absent from the index is not "nothing to check" — it is a commit with
# no vitest config at all, whose layout nothing declares. Staging the deletion
# and restoring the file in the worktree used to read as clean, because only the
# worktree copy was validated.
config_missing_from_index() {
  command -v git >/dev/null 2>&1 || return 1
  git rev-parse --git-dir >/dev/null 2>&1 || return 1
  git cat-file -e ":$VITEST_CONFIG" 2>/dev/null && return 1
  return 0
}

# Splits a test block into its own top-level properties, one per line, as
# NAME<TAB>VALUE. A spread is reported as the name "...".
#
# Line-anchored greps read formatting, not structure. `test: { include: [...],
# exclude: [...] }` on one line is the same configuration as the same two
# properties on two lines, and vitest applies it identically -- but a pattern
# anchored at ^ saw only the first. Splitting on commas at the object's own
# depth, with strings opaque, makes the check independent of where the newlines
# happen to fall. Quoted names are unwrapped here too, so `"exclude"` and
# `exclude` arrive as the same property.
test_block_props() {
  awk '
    { all = all $0 "\n" }
    function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    function unquote(s,   q) {
      s = trim(s)
      if (length(s) >= 2) {
        q = substr(s, 1, 1)
        if ((q == "\"" || q == "'"'"'" || q == "`") && substr(s, length(s), 1) == q) {
          return substr(s, 2, length(s) - 2)
        }
      }
      return s
    }
    function emit(seg,   name, value, colon, i, n, c, depth, instr) {
      seg = trim(seg)
      if (seg == "") return
      gsub(/\n/, " ", seg)
      if (substr(seg, 1, 3) == "...") { print "..." "\t" seg; return }
      # First colon at the segment own depth separates name from value.
      n = length(seg); depth = 0; instr = ""; colon = 0
      for (i = 1; i <= n; i++) {
        c = substr(seg, i, 1)
        if (instr != "") {
          if (c == "\\") { i++; continue }
          if (c == instr) instr = ""
          continue
        }
        if (c == "\"" || c == "'"'"'" || c == "`") { instr = c; continue }
        if (c == "{" || c == "[" || c == "(") { depth++; continue }
        if (c == "}" || c == "]" || c == ")") { depth--; continue }
        if (c == ":" && depth == 0) { colon = i; break }
      }
      if (colon == 0) { print unquote(seg) "\t"; return }
      name = unquote(substr(seg, 1, colon - 1))
      value = trim(substr(seg, colon + 1))
      print name "\t" value
    }
    END {
      n = length(all); depth = 0; instr = ""; seg = ""
      for (i = 1; i <= n; i++) {
        c = substr(all, i, 1)
        if (instr != "") {
          seg = seg c
          if (c == "\\") { seg = seg substr(all, i + 1, 1); i++; continue }
          if (c == instr) instr = ""
          continue
        }
        if (c == "\"" || c == "'"'"'" || c == "`") { instr = c; seg = seg c; continue }
        if (c == "{" || c == "[" || c == "(") { depth++; seg = seg c; continue }
        if (c == "}" || c == "]" || c == ")") { depth--; seg = seg c; continue }
        if (c == "," && depth == 0) { emit(seg); seg = ""; continue }
        seg = seg c
      }
      emit(seg)
    }
  '
}

# value_is_literal_array – 0 when stdin is one bracketed array whose every
# element is a plain quoted string.
#
# Two separate ways this went wrong, and the second is why the element check
# exists rather than only the bracket check:
#
#   ["a", "b"].slice(1)                  -- starts with [ but is an expression
#   [...(cond ? ["a"] : ["b"])]          -- is an array, of a computed element
#
# Both put the declared glob in the text while handing vitest something else, so
# the value is only readable as a declaration when the outer bracket's match is
# the last character AND every element is a literal string.
value_is_literal_array() {
  awk '
    { v = v $0 " " }
    function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    # Index just past the closing quote of the string opening at i. Its own copy
    # rather than the one in extract_test_block: these are two separate awk
    # programs, and a function is only in scope in the one it is written in.
    function str_end(s, i, n,   q, c) {
      q = substr(s, i, 1)
      i++
      while (i <= n) {
        c = substr(s, i, 1)
        if (c == "\\") { i += 2; continue }
        if (c == q) { return i + 1 }
        i++
      }
      return n + 1
    }
    function element_is_literal(s,   q) {
      s = trim(s)
      if (s == "") return 1          # trailing comma
      q = substr(s, 1, 1)
      if (q != "\"" && q != "'"'"'" && q != "`") return 0
      # The closing quote must be the last character: "a" + x is not a literal.
      if (substr(s, length(s), 1) != q || length(s) < 2) return 0
      # Endpoints are not the element. `"a" && "b"` opens and closes with the
      # same quote and is a boolean expression, so the closing quote of the
      # FIRST string has to be the last character of the element -- one quoted
      # token, nothing after it.
      if (str_end(s, 1, length(s)) != length(s) + 1) return 0
      # A backtick-delimited value is only a literal while nothing inside it is
      # computed. `${false ? glob : "tests/one.test.ts"}` is a quoted value by
      # every test above and still hands vitest a string chosen at runtime, with
      # the declared glob sitting in the file as the branch never taken.
      if (q == "`" && index(s, "${") > 0) return 0
      return 1
    }
    END {
      v = trim(v)
      n = length(v)
      if (n < 2 || substr(v, 1, 1) != "[") { exit 1 }
      depth = 0; instr = ""; seg = ""; ok = 1
      for (i = 1; i <= n; i++) {
        c = substr(v, i, 1)
        if (instr != "") {
          seg = seg c
          if (c == "\\") { seg = seg substr(v, i + 1, 1); i++; continue }
          if (c == instr) instr = ""
          continue
        }
        if (c == "\"" || c == "'"'"'" || c == "`") { instr = c; seg = seg c; continue }
        if (c == "[" || c == "{" || c == "(") {
          depth++
          if (depth > 1) seg = seg c
          continue
        }
        if (c == "]" || c == "}" || c == ")") {
          depth--
          if (depth == 0) {
            if (!element_is_literal(seg)) ok = 0
            # Anything after the outer bracket makes this an expression.
            exit (i == n && ok) ? 0 : 1
          }
          seg = seg c
          continue
        }
        if (c == "," && depth == 1) {
          if (!element_is_literal(seg)) ok = 0
          seg = ""
          continue
        }
        seg = seg c
      }
      exit 1
    }
  '
}

# Runs the include/exclude assertions over one config file. Echoes nothing on
# success; on failure echoes a reason keyword the caller turns into a message.
check_one_config() {
  local cfg="$1"
  local block props have_block=1 active_include="" active_exclude="" active_spread=""
  local unknown_props=""

  # The status carries three answers, not two: found, absent, and "found more
  # than one", which is a different failure and needs a different message.
  local _xt_rc=0
  block="$(strip_ts_comments "$cfg" | extract_test_block)" || _xt_rc=$?
  if [ "$_xt_rc" -eq 2 ]; then
    printf 'duplicate-test-block'
    return 0
  fi
  [ "$_xt_rc" -eq 0 ] || have_block=0
  if [ -n "$block" ]; then
    props="$(printf '%s\n' "$block" | test_block_props)"
    active_include="$(printf '%s\n' "$props" | awk -F'\t' '$1 == "include" { print $2 }')"
    active_exclude="$(printf '%s\n' "$props" | awk -F'\t' '$1 == "exclude" { print $2 }')"
    # A spread carries in properties this guard never sees. `test: { ...hidden,
    # include: [...] }` passes every direct-property check above while vitest
    # applies whatever exclude `hidden` contains -- the same silent drop the
    # exclude rule exists to prevent, one indirection out of reach.
    active_spread="$(printf '%s\n' "$props" | awk -F'\t' '$1 == "..." { print $2 }')"
    # Anything this guard does not recognise. `exclude` and a spread each got
    # their own rule after each was found to drop tests silently, and then
    # `testNamePattern` did the same thing by a third route -- vitest 3.2 exits
    # 0 with every test reported skipped. Enumerating the dangerous options one
    # finding at a time is losing by construction, so the list is inverted: a
    # property that cannot reduce what is collected or run is named here, and
    # everything else stops the guard until someone decides which it is.
    unknown_props="$(printf '%s\n' "$props" | awk -F'\t' '
      $1 == "" || $1 == "..." { next }
      {
        ok = "|include|environment|environmentOptions|globals|setupFiles|globalSetup" \
             "|css|coverage|reporters|outputFile|restoreMocks|clearMocks|mockReset" \
             "|testTimeout|hookTimeout|teardownTimeout|alias|pool|poolOptions|isolate" \
             "|sequence|snapshotFormat|expect|fakeTimers|unstubEnvs|unstubGlobals" \
             "|server|deps|silent|logHeapUsage|slowTestThreshold|chaiConfig|name|"
        if (index(ok, "|" $1 "|") == 0) print $1
      }')"
  fi

  if [ "$have_block" = "0" ]; then
    printf 'no-test-block'
  elif [ -n "$active_include" ] && ! printf '%s\n' "$active_include" | value_is_literal_array; then
    # A non-literal include cannot be read by searching its text: in
    # `cond ? [glob] : ["tests/only.test.ts"]` the glob is present and inactive,
    # so a substring match passes while vitest collects the other branch.
    printf 'include-not-literal\t%s' "$active_include"
  elif [ -n "$active_include" ] && printf '%s\n' "$active_include" | grep -qF '!'; then
    # A negated entry subtracts inside include, where the exclude rule cannot
    # see it. `['tests/**/*.test.{ts,tsx}', '!tests/lib/**']` is a literal array
    # and does contain the declared glob, so every check above passed while
    # vitest collected no tests/lib file at all -- the same silent drop as
    # `exclude`, spelled one property over.
    #
    # Any `!`, not a leading one. Globs subtract in two shapes: `!pattern` at
    # the front of an entry, and picomatch's extglob `tests/**/!(lib)/*.ts`
    # in the middle of one. Matching only the first would be the same partial
    # cover that let this through, and an additive test glob has no other use
    # for the character.
    printf 'include-negated\t%s' "$active_include"
  elif [ -z "$active_include" ] || ! printf '%s\n' "$active_include" | grep -qF "$DECLARED_GLOB"; then
    printf 'no-include'
  elif [ -n "$active_exclude" ]; then
    printf 'has-exclude\t%s' "$active_exclude"
  elif [ -n "$active_spread" ]; then
    printf 'has-spread\t%s' "$active_spread"
  elif [ -n "$unknown_props" ]; then
    printf 'unknown-prop\t%s' "$(printf '%s' "$unknown_props" | tr '\n' ' ')"
  fi
}

if [ ! -f "$VITEST_CONFIG" ]; then
  fail "Missing ${VITEST_CONFIG}; cannot confirm the test layout is declared."
elif config_missing_from_index; then
  fail "${VITEST_CONFIG} exists on disk but is not in the git index."
  echo "  The commit being made would carry no vitest config, so nothing would"
  echo "  declare the layout and vitest would fall back to its default glob."
  echo "  Stage the config (git add ${VITEST_CONFIG}), or restore it if the"
  echo "  deletion was staged by mistake."
else
  # Each source is checked independently: a bad include in the staged copy or in
  # the worktree copy is drift either way.
  while IFS= read -r _cfg; do
    [ -z "$_cfg" ] && continue
    if [ "$_cfg" = "$VITEST_CONFIG" ]; then
      _label="$VITEST_CONFIG"
    else
      _label="${VITEST_CONFIG} (staged)"
    fi

    _verdict="$(check_one_config "$_cfg")"
    _reason="${_verdict%%$'\t'*}"
    _detail="${_verdict#*$'\t'}"

    case "$_reason" in
      "") ;;
      no-test-block)
        fail "${_label} has no readable 'test: { ... }' block; cannot confirm the layout is declared."
        echo "  The layout must be declared under test.include, where vitest reads it."
        ;;
      duplicate-test-block)
        fail "${_label} declares more than one top-level 'test: { ... }' block, or a computed one."
        echo "  JavaScript keeps the later property, so the first one is not the"
        echo "  config vitest uses: a broad include followed by"
        echo "  test: { include: [\"tests/only.test.ts\"] } reads as fully covered"
        echo "  while vitest collects one file. Which of the two was meant is not"
        echo "  something this guard should decide — merge them into one block."
        ;;
      no-include)
        fail "${_label} no longer declares an active test.include '${DECLARED_GLOB}'."
        echo "  The layout must be declared in config, not left to vitest's default glob;"
        echo "  it must be live code — a commented-out include does not count; and it"
        echo "  must sit inside test: { }, not another section that happens to have an"
        echo "  include field. Restore a single-line 'include: [...]' carrying the glob,"
        echo "  or update DECLARED_GLOB in this script to match."
        ;;
      has-exclude)
        fail "${_label} declares a test.exclude, which this guard cannot verify."
        echo "  exclude is applied on top of include, so a correct '${DECLARED_GLOB}'"
        echo "  can still collect nothing: exclude: [\"tests/lib/**\"] silently drops"
        echo "  every test under that path while this check keeps reporting them."
        echo "  Express the layout with include alone — vitest's default exclude already"
        echo "  covers node_modules and dist. If an exclude is genuinely needed, teach"
        echo "  this guard to evaluate it in the same commit."
        printf '%s\n' "$_detail" | sed 's/^/    /'
        ;;
      include-not-literal)
        fail "${_label} computes test.include instead of declaring it literally."
        echo "  The glob can then appear in a branch vitest never takes:"
        echo "  cond ? [\"${DECLARED_GLOB}\"] : [\"tests/only.test.ts\"] contains it and"
        echo "  collects one file. This guard reads the config as text, so an"
        echo "  expression is not something it can decide."
        echo "  Declare a literal array, or teach this guard to evaluate the"
        echo "  expression in the same commit."
        printf '%s\n' "$_detail" | sed 's/^/    /'
        ;;
      include-negated)
        fail "${_label} negates inside test.include, which subtracts tests silently."
        echo "  A '!' entry removes what an earlier entry added, so include can"
        echo "  carry '${DECLARED_GLOB}' and still collect none of a subtree:"
        echo "  [\"${DECLARED_GLOB}\", \"!tests/lib/**\"] reports as fully covered"
        echo "  here while vitest lists no tests/lib file at all. It is the same"
        echo "  silent drop as test.exclude, written one property over."
        echo "  State the layout additively, or move the subtraction into"
        echo "  exclude where it is at least visible as one."
        printf '%s\n' "$_detail" | sed 's/^/    /'
        ;;
      unknown-prop)
        fail "${_label} sets test properties this guard cannot evaluate."
        echo "  Any of them may reduce what vitest collects or runs while every"
        echo "  assertion here still passes -- testNamePattern, for one, makes"
        echo "  vitest exit 0 with every test reported skipped."
        echo "  If a property is genuinely safe, add it to the allow-list in"
        echo "  this script in the same commit that introduces it."
        echo "    ${_detail}"
        ;;
      has-spread)
        fail "${_label} spreads another object into test: { }, which this guard cannot evaluate."
        echo "  A spread carries in properties this check never sees. An exclude"
        echo "  reached that way drops tests exactly as a direct one would, while"
        echo "  every assertion here still passes."
        echo "  Write the test block out directly, or teach this guard to resolve"
        echo "  the spread in the same commit."
        printf '%s\n' "$_detail" | sed 's/^/    /'
        ;;
    esac

    # Drop the temp copy of the staged config.
    [ "$_cfg" = "$VITEST_CONFIG" ] || rm -f "$_cfg"
  done <<< "$(config_sources)"
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
