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

# Is there a frontend in the tree this run stands behind?
#
# `[ ! -d frontend ]` is a question about the worktree, and the answer here is
# the entire check not running. Everything below it already reads `HEAD:` in
# ship mode -- config_sources, workspace_files_present, the retired-convention
# scan, the file count -- and this gate above them all did not, so a frontend/
# missing from the worktree while HEAD still carries it switched the always-run
# layout guard off for a push that contains it. A stash, a partial checkout or
# an in-progress move is enough. Reproduced on a clean HEAD holding frontend/:
#
#   rm -rf frontend
#   CI_GATE_MODE=ship bash ci/checks/test-layout.sh
#   -> [INFO] skipped: no frontend/ directory        exit 0
#
# HEAD alone in ship mode, not HEAD plus the worktree: a frontend/ that exists
# only on disk is not what the push sends, and the checks below would then be
# reporting on files no destination receives. Same rule as every other reader of
# this question on the branch -- which tree a run vouches for chooses the
# sources, it does not add to them.
# 0 when the tree this run stands behind has a frontend, 1 when it does not, and
# 2 when that could not be established.
#
# Three answers, because two of them were being spelled the same way. The
# substitution here reduced a failed `git ls-tree` -- an unavailable tree object
# in a partial or damaged checkout, a repository that cannot be read -- to empty
# output, which is exactly what an absent frontend produces. The caller then
# logged "skipped: no frontend/ directory" and returned PASS, so the one guard
# standing between a misplaced test and a green build that never ran it was
# switched off by an unreadable object rather than by a decision. Could-not-ask
# is not found-nothing; that rule is applied at every other enumeration in this
# gate and this was the reader that had not been told.
#
# The status is taken through PIPESTATUS: `| head -n 1` answers for head, which
# succeeds whatever the producer did.
frontend_present() {
  if [ "${CI_GATE_MODE:-}" = "ship" ] \
    && command -v git >/dev/null 2>&1 \
    && git rev-parse --git-dir >/dev/null 2>&1 \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    local _fp_out _fp_rc=0
    _fp_out="$(git ls-tree --name-only "HEAD:${FRONTEND_DIR}" 2>/dev/null | head -n 1
               exit "${PIPESTATUS[0]}")" || _fp_rc=$?
    # git exits non-zero both for "that path is not in HEAD" and for "the tree
    # could not be read", and only the second is infrastructure. They are told
    # apart by asking whether HEAD carries the path at all, which is a question
    # about the index rather than about the object store.
    if [ "$_fp_rc" -ne 0 ]; then
      git cat-file -e "HEAD:${FRONTEND_DIR}" 2>/dev/null || return 1
      return 2
    fi
    [ -n "$_fp_out" ]
    return
  fi
  [ -d "$FRONTEND_DIR" ]
}

_FP_RC=0
frontend_present || _FP_RC=$?
if [ "$_FP_RC" -eq 2 ]; then
  ci::log::error "Cannot read HEAD:${FRONTEND_DIR} to find out whether this push has a frontend."
  ci::log::error "  Refusing to conclude there is none from a listing that failed to"
  ci::log::error "  produce one: that would skip the layout guard for the push."
  exit "$CI_RESULT_FAIL_INFRA"
fi
if [ "$_FP_RC" -ne 0 ]; then
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
#
# Named once, because it was written out three times in this file and the three
# disagreed. The legacy-directory prune below, the candidate-file prune, and the
# index-side predicate `path_is_pruned` are one question -- "does this path hold
# first-party source" -- asked of three different inputs, and an entry added to
# one copy did not reach the others. `.cache` was added to the candidate prune a
# round ago and to neither of the others, so a generator writing
# `frontend/.cache/__tests__/` was still reported as a retired directory the
# developer must delete: measured exit 20, "Retired __tests__ directories still
# present under frontend/: frontend/.cache/__tests__", blocking every quick and
# full validation on state nobody wrote and cannot usefully remove. `out`,
# `htmlcov` and `.nuxt` were missing from two of the three as well.
#
# The index-side copy is the one that mattered most and the one nobody looked
# at: `find` applies its prune itself, but `git ls-files` does not, so
# path_is_pruned answers for *both* scans on the index side. A staged
# `frontend/.cache/generated.test.ts` was therefore still failing the candidate
# scan after the round that supposedly fixed exactly that.
#
# Fixing the fifth instance of a list that has drifted five times is not a fix,
# so the list has one definition and the three readers are built from it. That
# is what makes "check the siblings when an entry moves" unnecessary here.
GENERATED_DIRS=(dist build out coverage htmlcov .next .nuxt .turbo .vite .cache)

# Two find prunes built from it. They differ in exactly one entry, deliberately:
# TESTS_DIR is pruned for the legacy scan because that scan looks for *retired*
# directories and the declared tree is not one, while the candidate scan has to
# walk it. node_modules stays unanchored in both because nested copies are real
# and are never first-party, whereas the generated names are anchored to
# FRONTEND_DIR -- an unanchored -name 'build' also prunes a first-party
# frontend/e2e/build/, and a test under it would be neither collected by vitest
# nor reported here, which is the precise hole this guard exists to close.
PRUNE=( '(' -path "$TESTS_DIR" -o -name 'node_modules' )
for _tl_gd in "${GENERATED_DIRS[@]}"; do
  PRUNE+=( -o -path "$FRONTEND_DIR/$_tl_gd" )
done
PRUNE+=( ')' -prune -o )

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
CANDIDATE_PRUNE=( '(' -name 'node_modules' )
for _tl_gd in "${GENERATED_DIRS[@]}"; do
  CANDIDATE_PRUNE+=( -o -path "$FRONTEND_DIR/$_tl_gd" )
done
CANDIDATE_PRUNE+=( ')' -prune -o )

# NUL-delimited, from every source, and read that way by the caller.
#
# A line-oriented list cannot represent a path containing a newline: one such
# path arrives as two, and the tail of it can be spelled to look like it sits
# under frontend/tests/. A file whose name embeds a newline followed by
# "frontend/tests/evil.test.ts" therefore reported "Test layout OK: 0 file(s)"
# while vitest collects nothing
# of the sort -- a fail-open on the guard whose entire job is to notice tests
# that will never run. git already emits its own quoted spelling for these
# unless told otherwise, so the two sources did not even agree about the path.
candidate_files() {
  # Which tree this run vouches for chooses the sources, rather than HEAD being
  # added to the other two.
  #
  # HEAD arrived here as a third source because the pre-push gate stands behind
  # it and this file had no notion of that: remove a stray test from the index
  # and the worktree without committing the removal, and the guard reported
  # "Test layout OK" for a push whose tree still carries it. That half was
  # right. Leaving the worktree in was not — ship mode then covered the pushed
  # tree *and* every scratch file on disk, so an untracked
  # frontend/src/probe.test.ts, in neither the index nor HEAD, failed a push
  # whose tree does not contain it, with no remedy but deleting the file.
  # Measured: exit 20 under CI_GATE_MODE=ship over a clean HEAD.
  #
  # ci/checks/node.sh:190-207 answers the same question about the same
  # directory's manifest by switching its reference, not by widening it, and
  # says why: "Which tree is 'the commit' depends on the gate."
  if [ "${CI_GATE_MODE:-}" = "ship" ]; then
    # Fail closed rather than falling back to the worktree. Without git, or
    # without a HEAD, this run cannot read the tree it is vouching for, and
    # "could not look" is not "found nothing" -- the reason the `|| true` that
    # used to be on the ls-tree below was removed.
    command -v git >/dev/null 2>&1 || return 1
    git rev-parse --git-dir >/dev/null 2>&1 || return 1
    git rev-parse --verify HEAD >/dev/null 2>&1 || return 1
    git ls-tree -r -z --name-only HEAD -- "$FRONTEND_DIR" 2>/dev/null || return 1
    return 0
  fi
  {
    find "$FRONTEND_DIR" "${CANDIDATE_PRUNE[@]}" -type f "${TEST_SUFFIXES[@]}" -print0 2>/dev/null || return 1
    if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
      git ls-files -z -- "$FRONTEND_DIR" 2>/dev/null || return 1
    fi
  }
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
  esac
  # From GENERATED_DIRS rather than a fourth spelling of it. This predicate is
  # the index side of *both* scans -- find applies its own prune, git ls-files
  # does not -- so it was the copy where a missing entry did the most damage and
  # the copy nobody updated: `out`, `htmlcov`, `.nuxt` and `.cache` were all
  # absent here after being added to the find prunes.
  local _pp_gd
  for _pp_gd in "${GENERATED_DIRS[@]}"; do
    case "$1" in "$FRONTEND_DIR/$_pp_gd"/*) return 0 ;; esac
  done
  return 1
}

# Held in an array, because a command substitution cannot carry NUL bytes --
# bash drops them -- which would put the newline problem straight back.
#
# Deduplicated here rather than with `sort -z -u`. That flag is a GNU extension,
# and this repository states a Bash 3.2 floor in ci/lib/runner.sh, which is the
# platform whose sort is least likely to have it -- a gate that dies on the
# machine it is meant to protect is not portable in any useful sense. The scan
# is quadratic in the number of candidates and the number is small: measured at
# 153 incoming paths deduplicating to 112 (`git ls-files -- frontend` gives 112,
# the test-suffix `find` another 41), so on the order of 8,500 string compares
# done by the shell itself, against three subprocesses and a pipe for the sort
# it replaces. An earlier version of this note said 219, which no longer matched
# the tree it described.
#
# The cost scales with every tracked file under frontend/, not only the test
# files, and that is deliberate rather than an oversight: the __tests__ scan
# below has to see paths this file would not call test-like -- a plain
# frontend/src/__tests__/helper.ts names a retired directory just as a
# .test.ts under it does -- so narrowing the candidate list to test suffixes
# here would blind that rule. If the frontend tree ever grows to where this
# shows up in the lane's runtime, the fix is an associative-array dedup behind
# a `BASH_VERSINFO[0] -ge 4` guard, not a narrower list.
ALL_CANDIDATES=()
# Through a temp file rather than `< <(candidate_files)`.
#
# A process substitution is a subshell, so a producer that fails there fails
# invisibly: the loop reads whatever reached the pipe and reports its own
# success, which is the identical defect this check just had at
# `done <<< "$(config_sources)"`. Writing the stream out first is what makes
# the producer status something the caller can act on -- and it has to be a
# file, because the stream is NUL-delimited and a command substitution cannot
# carry a NUL byte.
_CAND_TMP="$(ci::common::mktemp_file layout-cands 2>/dev/null)" || _CAND_TMP=""
if [ -z "$_CAND_TMP" ]; then
  echo "Cannot create a private temporary file to enumerate candidate tests." >&2
  exit "$CI_RESULT_FAIL_INFRA"
fi
if ! candidate_files > "$_CAND_TMP"; then
  rm -f "$_CAND_TMP"
  echo "Cannot enumerate the test files ${CI_GATE_MODE:-this gate} is reporting on." >&2
  echo "  Refusing to validate the sources that did answer: a tree that could" >&2
  echo "  not be read is not a tree with nothing in it." >&2
  exit "$CI_RESULT_FAIL_INFRA"
fi
while IFS= read -r -d '' _cand; do
  [ -n "$_cand" ] || continue
  _dup=0
  for _seen in ${ALL_CANDIDATES[@]+"${ALL_CANDIDATES[@]}"}; do
    if [ "$_seen" = "$_cand" ]; then
      _dup=1
      break
    fi
  done
  [ "$_dup" -eq 1 ] && continue
  ALL_CANDIDATES+=("$_cand")
done < "$_CAND_TMP"
rm -f "$_CAND_TMP"

# Arrays throughout, for the same reason the candidate list is one: a path
# holding a newline cannot survive being joined into a string and split again.
STRAY=()
for f in ${ALL_CANDIDATES[@]+"${ALL_CANDIDATES[@]}"}; do
  [ -z "$f" ] && continue
  path_is_test_like "$f" || continue
  path_is_pruned "$f" && continue
  case "$f" in "$TESTS_DIR"/*) continue ;; esac
  STRAY+=("$f")
done

if [ "${#STRAY[@]}" -gt 0 ]; then
  fail "Test files found outside ${TESTS_DIR}/. vitest include is '${DECLARED_GLOB}', so these would NEVER RUN:"
  for f in "${STRAY[@]}"; do
    echo "  $f"
  done
  echo ""
  echo "  Move each to ${TESTS_DIR}/, mirroring src/ without the __tests__ segment:"
  echo "    frontend/src/lib/api/__tests__/useThing.test.tsx"
  echo "      -> frontend/tests/lib/api/useThing.test.tsx"
fi

# ---------------------------------------------------------------------------
# 2. No lingering __tests__ directories outside tests/ (the retired convention).
# ---------------------------------------------------------------------------
LEGACY_DIRS="$( {
  # The filesystem walk is the pre-commit gate's half. In ship mode the run
  # stands behind HEAD, and ALL_CANDIDATES below is already derived from it --
  # so walking the worktree here reintroduced the very thing candidate_files
  # stopped doing: an untracked local `frontend/src/__tests__/` failed a push
  # whose tree does not contain it, and git will not send that directory.
  #
  # Third reader of "which tree" in this file. The other two were fixed one
  # commit ago and this one was not looked at, which is the shape this whole
  # series is about, committed by the person fixing it.
  if [ "${CI_GATE_MODE:-}" != "ship" ]; then
    find "$FRONTEND_DIR" "${PRUNE[@]}" -type d -name '__tests__' -print 2>/dev/null || true
  fi
  # Index side: a staged file under a __tests__/ path names the directory even
  # when the directory no longer exists on disk. In ship mode these candidates
  # come from HEAD, so the same loop answers for the pushed tree.
  for f in ${ALL_CANDIDATES[@]+"${ALL_CANDIDATES[@]}"}; do
    [ -z "$f" ] && continue
    path_is_pruned "$f" && continue
    case "$f" in
      */__tests__/*) printf '%s\n' "${f%%/__tests__/*}/__tests__" ;;
    esac
  done
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
UNRUNNABLE=()
for f in ${ALL_CANDIDATES[@]+"${ALL_CANDIDATES[@]}"}; do
  [ -z "$f" ] && continue
  case "$f" in "$TESTS_DIR"/*) ;; *) continue ;; esac
  path_is_test_like "$f" || continue
  case "$f" in *.test.ts | *.test.tsx) continue ;; esac
  UNRUNNABLE+=("$f")
done

if [ "${#UNRUNNABLE[@]}" -gt 0 ]; then
  fail "Files under ${TESTS_DIR}/ that do not match '${DECLARED_GLOB}' and are therefore silently skipped:"
  for f in "${UNRUNNABLE[@]}"; do
    echo "  $f"
  done
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
    # Where a `/` opens a regex literal rather than divides.
    #
    # A vitest config that aliases with a regex -- `find: /^@\//` is the shape
    # the reviewer reproduced -- put an escaped slash next to the closing one,
    # and the scanner read that `//` as a line comment and dropped the rest of
    # the line before the brace matcher ran. `resolve: { alias: [{ find: /^@\//,
    # replacement: ... }] }` lost its closing braces, so the extractor could not
    # find the `test` block in a config that has one, and the check reported a
    # missing include glob for a workspace that declares it.
    #
    # Told apart the way a JavaScript tokenizer tells them apart: by what comes
    # before. After a value -- a name, a number, `)`, `]` -- a slash divides;
    # everywhere a value is still expected it opens a regex. Line start counts as
    # expecting a value. This is a heuristic in the language itself, not a
    # shortcut taken here, and it is why prevc tracks the last non-blank
    # character rather than the last character.
    function re_allowed(pc) {
      return (pc == "" || pc ~ /[(,=:\[!&|?{};+\-*%~^<>]/)
    }
    {
      line = $0
      in_re = 0
      prevc = ""
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
        if (in_re) {
          out = out c
          if (c == "\\") { out = out substr(line, i + 1, 1); i += 2; continue }
          if (c == "/") { in_re = 0 }
          i++
          continue
        }
        if (two == "//") { break }
        if (two == "/*") { in_block = 1; i += 2; continue }
        # Below the two comment openers, not above them, and the first draft had
        # it above: a line whose first non-blank characters are `//` starts at
        # prevc == "" -- a position where a value is expected -- so the comment
        # opener was read as a regex opener and no comment was stripped at all.
        # Nothing is lost by testing the openers first. `//` is an empty regex,
        # which JavaScript does not have precisely because it spells a comment,
        # and a regex cannot begin with an unescaped `*` either.
        if (c == "/" && re_allowed(prevc)) { in_re = 1; out = out c; prevc = c; i++; continue }
        if (c == "\"" || c == "'"'"'" || c == "`") { in_str = c }
        out = out c
        if (c !~ /[ \t]/) { prevc = c }
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
      #
      # What sits between the two is an allow-list, not "anything, then take the
      # first object". `defineConfig(Object.assign({ test: { include: [broad] } },
      # { test: { include: ["one.test.ts"] } }))` put the *broad* object first, so
      # this scan validated it and exited 0 while Object.assign let the second
      # object replace `test` and vitest collected one file. Composition is
      # invisible to a reader that only ever looks at one object literal.
      #
      # So: nothing, or exactly `defineConfig(`. A wrapper this guard has not
      # been taught -- Object.assign, mergeConfig, a helper of your own -- stops
      # it rather than being assumed transparent. Same inversion as the flags and
      # the config properties: name what is known to be safe, and let everything
      # else ask a human.
      pre = ""
      p = ed + 14
      while (p <= n) {
        ch = substr(all, p, 1)
        if (is_quote(ch)) { p = string_end(all, p, n); continue }
        if (ch == "/" && is_regex_start(all, p)) { p = regex_end(all, p, n); continue }
        if (ch == "{") break
        if (ch !~ /[[:space:]]/) pre = pre ch
        p++
      }
      if (p > n) exit 1
      if (pre != "" && pre != "defineConfig(") { exit 3 }
      p++

      depth = 1
      bdepth = 0
      while (p <= n && depth > 0) {
        ch = substr(all, p, 1)

        if (is_quote(ch)) {
          e = string_end(all, p, n)
          # A key spelled with an escape is a key this reader cannot see.
          # JavaScript processes the escape in the string literal, so
          # `"test"` IS `test` -- one object, one key, and the narrow block
          # under it replaces the broad one this scan validated. Both key arms
          # here compare source text, so the escaped spelling was not seen as a
          # second `test` at all and the guard reported the layout covered while
          # vitest collected one file. Refused rather than decoded, like every
          # other form this reader cannot evaluate.
          #
          # Only in key position -- a string followed by a colon. A backslash in
          # a *value* at this level is ordinary data and says nothing about
          # which properties the object has.
          if (depth == 1 && bdepth == 0 && index(substr(all, p, e - p), "\\") > 0 \
              && substr(all, e) ~ /^[[:space:]]*:/) {
            computed = 1
          }
          # `"test": { }` is the same key as `test: { }`. Everything else in a
          # string is data, however much it looks like config.
          if (depth == 1 && bdepth == 0 && substr(all, p + 1, e - p - 2) == "test") {
            rest = substr(all, e)
            # A value that is not a literal object is refused here for the same
            # reason it is refused for the unquoted spelling below: JavaScript
            # keeps the later property whatever shape it has, and this reader
            # can see into an object literal and nothing else. Written on both
            # arms in the same commit, because a rule that exists for one
            # spelling of a key and not the other is the defect this file keeps
            # being handed.
            if (rest ~ /^[[:space:]]*:/ && rest !~ /^[[:space:]]*:[[:space:]]*\{/) {
              computed = 1
            }
            if (rest ~ /^[[:space:]]*:[[:space:]]*\{/) {
              start = e + index(rest, "{")
              # Recorded and scanning continues, exactly as the unquoted spelling
              # below does. Printing here and leaving ended the scan at the first
              # quoted key, so every rule that comes *after* it -- a duplicate, a
              # computed key, a spread, a shorthand override -- was never applied
              # to a config that spells the key `"test"`. `defineConfig({ "test":
              # { include: [broad] }, ...narrow })` reported the broad include
              # live and exited 0 while vitest collected the narrowed one. One
              # rule, two spellings, and only one of them had it.
              seen++
              block = capture(all, start, n)
            }
          }
          p = e
          continue
        }

        if (ch == "/" && is_regex_start(all, p)) { p = regex_end(all, p, n); continue }

        if (ch == "{") { depth++; p++; continue }
        if (ch == "}") { depth--; p++; continue }

        # Brackets move a depth of their own, so what is inside an array is not
        # read as a property of the object around it. Nothing counted them: an
        # element after a comma inside `plugins: [react(), ["vite-plugin-x", {
        # a: 1 }]]` was still `depth == 1` preceded by `,`, which is exactly the
        # shape the computed-key rule below was narrowed to, and a config with
        # one `test` block was refused as declaring two. The same miscount
        # reached the three rules after it: `plugins: [...base, react()]` is a
        # spread at `depth == 1`, and `plugins: [test, other]` is a bare `test`
        # followed by a comma.
        #
        # A computed key is judged here rather than below, because `[` is the
        # character that opens both and only the one at the exported object own
        # level is a key.
        if (ch == "[") {
          if (depth == 1 && bdepth == 0) {
            pc = prev_signif(all, p)
            if (pc == "{" || pc == "," || pc == "") computed = 1
          }
          bdepth++
          p++
          continue
        }
        if (ch == "]") { if (bdepth > 0) bdepth--; p++; continue }

        # An identifier may carry the same escape a quoted key can, and it is
        # the same problem: a property whose name this reader cannot spell out.
        # Strings and regular expressions are consumed above, so a backslash
        # reaching here at the exported object own level is one of those.
        if (depth == 1 && bdepth == 0 && ch == "\\") { computed = 1; p++; continue }

        # A computed key at the exported object own level. `["test"]: {...}` is
        # applied by JavaScript exactly like `test:`, and applied *later* if it
        # comes later -- but the quoted token is skipped by the string handling
        # above, so the scan counted only the plain block and reported a broad
        # include as live while vitest collected the narrowed one. This reader
        # works on text and cannot evaluate a computed key in general, so any
        # computed property at this level stops it rather than being guessed at.
        # Only where a *key* would be, which is right after the opening brace
        # or a comma. `[` at this level is far more often an ordinary array
        # value -- `plugins: [react()]` is in most vitest configs there is --
        # and treating every one of them as a computed key failed the guard on
        # a config that is entirely correct. Proven, not reasoned: adding a
        # bare `plugins: []` to the vitest.config.ts in this repository made the
        # check exit 20 with "more than one top-level test block". A guard that
        # blocks correct work gets switched off, and then it guards nothing.
        #
        # The narrowing keeps the case it was written for: `["test"]: {...}`
        # follows `{` or `,` because that is where a property starts.
        # A spread at this level is the same problem as a computed key, and was
        # the next one along: `{ test: {broad}, ...narrow }` applies the spread
        # `test` last, so the block this scan captured is not the one vitest
        # uses. The test-block reader already refuses a spread *inside* the
        # block for exactly this reason; the exported object own level had
        # never been asked.
        # Only a spread that comes *after* the test key. JavaScript keeps the
        # later property, so `{ ...shared, test: {...} }` cannot be overridden
        # by the spread -- the explicit key wins -- while `{ test: {...},
        # ...narrow }` can be. Flagging both failed a config that is entirely
        # correct and is the ordinary way to reuse a base config, which is the
        # same false failure this round has already fixed twice.
        if (depth == 1 && bdepth == 0 && seen > 0 && substr(all, p, 3) == "...") {
          computed = 1
        }

        # A shorthand, method or getter `test` at this level overrides the
        # colon-form block, and only the colon form was ever recognised.
        # `const test = { include: ["one.test.ts"] }` followed by
        # `defineConfig({ test: { include: [broad] }, test })` reported all 38
        # files runnable while vitest listed four. `test()` and `get test()`
        # reach the same place by two more spellings, so the test is what
        # *follows* the identifier: anything other than a colon is a form this
        # reader cannot evaluate, and it stops rather than guessing.
        if (depth == 1 && bdepth == 0 && substr(all, p, 4) == "test") {
          before = (p > 1) ? substr(all, p - 1, 1) : " "
          rest = substr(all, p + 4)
          if (before !~ /[A-Za-z0-9_$]/ && rest !~ /^[[:space:]]*:/ \
              && rest ~ /^[[:space:]]*[,}(]/) {
            computed = 1
          }
        }

        # A `test` key whose value is not a literal object is a value this
        # reader cannot see into, and it is applied all the same. `test: narrow`
        # or `test: makeTestConfig()` after the validated block is what
        # JavaScript keeps, and it can hold `include: ["tests/only.test.ts"]`
        # while the broad literal above it was reported as active. Only the
        # `test: {` spelling was recognised at all, so every other value was
        # simply not seen. Refused as a duplicate, because that is what it is:
        # a second top-level `test`, and this one cannot even be read.
        if (depth == 1 && bdepth == 0 && substr(all, p, 4) == "test") {
          before = (p > 1) ? substr(all, p - 1, 1) : " "
          rest = substr(all, p + 4)
          if (before !~ /[A-Za-z0-9_$]/ && rest ~ /^[[:space:]]*:/ \
              && rest !~ /^[[:space:]]*:[[:space:]]*\{/) {
            computed = 1
          }
        }

        # Only a key at the exported object own level counts.
        if (depth == 1 && bdepth == 0 && substr(all, p, 4) == "test") {
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

      # What follows the exported object, if anything. The loop above ends when
      # the object closes, and everything after that was simply not looked at:
      # `export default defineConfig({ test: { include: [broad] } }) &&
      # defineConfig({ test: { include: ["tests/only.test.ts"] } })` exports the
      # right-hand config, because the left one is truthy, while this scan
      # validated the left one and exited 0. A `defineConfig(` wrapper closes
      # with its own `)`, and a statement may end with `;` -- everything else is
      # an expression this reader cannot evaluate, so it stops rather than
      # assuming it is transparent, exactly as the wrapper allow-list above the
      # object does.
      tail = ""
      while (p <= n) {
        ch = substr(all, p, 1)
        if (is_quote(ch)) { p = string_end(all, p, n); tail = tail "\"" ; continue }
        if (ch == "/" && is_regex_start(all, p)) { p = regex_end(all, p, n); tail = tail "/" ; continue }
        if (ch !~ /[[:space:]]/) tail = tail ch
        p++
      }
      # `)` closes the defineConfig wrapper the scan stepped into; `;` ends the
      # statement. A type assertion and a `satisfies` clause are erased before
      # anything evaluates this file, so a bare identifier tail is admitted only
      # in those forms.
      #
      # The closing `)` is stripped from the end as well as the front, and this
      # is what the rule was missing. `defineConfig({ ... } as UserConfig)` --
      # the ordinary way to write a typed Vitest config -- left `asUserConfig)`
      # after the old strips and was refused as composition, and so was
      # `defineConfig({ ... } as const)`, which the comment above claimed to
      # allow: the allow-list only ever matched a config written *without* the
      # wrapper, because with it the tail always ends in that paren. A rule
      # that never matched the example its own comment gives.
      #
      # Stripping the paren does not admit a composition. `defineConfig({...})
      # && defineConfig({...})` leaves `&&defineConfig({` behind, which matches
      # nothing here and is still refused -- the strips remove only the
      # punctuation of the statement itself, from its two ends.
      #
      # No apostrophes in this comment on purpose: it sits inside a
      # single-quoted awk program, and one closes the program. My first draft
      # of this paragraph did, and the whole file stopped parsing as shell.
      #
      # `as <Type>` in general rather than `as const` alone, for the reason
      # `satisfies` is already general: the assertion is compile-time, and which
      # type it names cannot change what vitest loads.
      if (tail != "") {
        tailrest = tail
        sub(/^\)*/, "", tailrest)
        sub(/[);]*$/, "", tailrest)
        if (tailrest != "" \
            && tailrest !~ /^as[A-Za-z0-9_$.<>,\[\]|]+$/ \
            && tailrest !~ /^satisfies[A-Za-z0-9_$.<>,\[\]|]*$/) exit 4
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
# Emit a temporary copy of the config as one revision spells it, or refuse.
#
# `_rev` is a git object prefix -- `:` for the index, `HEAD:` for the pushed
# tree. The failure arms are the point: an object the revision lists and git
# cannot produce is infrastructure, because dropping it silently leaves the
# caller validating the remaining copies and reporting PASS over a config that
# was never inspected.
_emit_config_copy() {
  local _rev="$1" _what="$2" _tmp
  if ! git cat-file -e "${_rev}${VITEST_CONFIG}" 2>/dev/null; then
    # Absent is not "nothing to check" for the pushed tree, for the same reason
    # it is not for the index: a commit with no vitest config declares no
    # layout at all. Returning 0 here validated the staged and worktree
    # repairs and reported PASS for a push whose tree carries neither the
    # config nor anything that defines what vitest collects.
    case "$_rev" in
      'HEAD:')
        echo "The pushed commit does not carry ${VITEST_CONFIG}." >&2
        echo "  A repair present only in the index or the worktree is not in" >&2
        echo "  the tree being pushed, which would then declare no test layout" >&2
        echo "  at all. Commit the config with it." >&2
        exit "$CI_RESULT_FAIL_NEW_ISSUE"
        ;;
    esac
    return 0
  fi
  _tmp="$(ci::common::mktemp_file layout-scan 2>/dev/null)" || _tmp=""
  if [ -z "$_tmp" ]; then
    echo "Cannot create a private temporary file to read the ${_what} ${VITEST_CONFIG}." >&2
    exit "$CI_RESULT_FAIL_INFRA"
  fi
  if git show "${_rev}${VITEST_CONFIG}" > "$_tmp" 2>/dev/null; then
    printf '%s\n' "$_tmp"
    return 0
  fi
  rm -f "$_tmp"
  echo "The ${_what} lists ${VITEST_CONFIG} but git cannot read that blob." >&2
  echo "  Refusing to validate the remaining copies alone: the config this" >&2
  echo "  commit would carry has not been inspected." >&2
  exit "$CI_RESULT_FAIL_INFRA"
}

config_sources() {
  # In ship mode the commit already exists, so the index and the worktree can
  # both hold a repair the pushed tree does not. `git show :path` reads the
  # index, so a HEAD whose include is narrowed -- or absent -- was never looked
  # at, and with the configurable Node lanes disabled nothing else looks at it
  # either. The pushed tree is what the gate is vouching for, so it is *the*
  # source.
  #
  # The source, and not a third one beside the other two. Adding HEAD while
  # leaving the worktree and index in made ship validate every copy, so a local
  # edit to frontend/vitest.config.ts -- narrowing the include while debugging,
  # or deleting the file on disk -- failed a push whose commit carries a
  # perfectly good config, and git will not send that edit. Which tree a run
  # vouches for chooses the sources; it does not add to them. Same correction as
  # candidate_files, workspace_files_present and the __tests__ scan, which is
  # four readers of one question in this file.
  local _cs_ship=0
  if [ "${CI_GATE_MODE:-}" = "ship" ] \
    && command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    _cs_ship=1
  fi
  if [ "$_cs_ship" -eq 1 ]; then
    _emit_config_copy "HEAD:" "pushed commit"
    return 0
  fi
  printf '%s\n' "$VITEST_CONFIG"
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    if git cat-file -e ":$VITEST_CONFIG" 2>/dev/null; then
      local staged
      staged="$(ci::common::mktemp_file layout-staged 2>/dev/null)" || staged=""
      if [ -z "$staged" ]; then
        echo "Cannot create a private temporary file to read the staged ${VITEST_CONFIG}." >&2
        exit "$CI_RESULT_FAIL_INFRA"
      fi
      if git show ":$VITEST_CONFIG" > "$staged" 2>/dev/null; then
        printf '%s\n' "$staged"
      else
        # The index says the blob is there and git cannot produce it. Dropping
        # it silently left the caller validating the worktree copy alone and
        # reporting PASS -- approving a commit whose active include was never
        # inspected, which is the same "could not look" read as "found nothing"
        # this whole check exists to remove. A corrupt or transiently
        # unavailable object is infrastructure, not a clean tree.
        rm -f "$staged"
        echo "The index lists ${VITEST_CONFIG} but git cannot read that blob." >&2
        echo "  Refusing to validate the worktree copy alone: the config this" >&2
        echo "  commit would carry has not been inspected." >&2
        exit "$CI_RESULT_FAIL_INFRA"
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

# Whether the tree this run stands behind carries the config at all.
#
# One of several readers of "which tree" in this file, and the one that was
# never asked. config_sources and workspace_files_present both read `HEAD:` in
# ship mode; the two arms that gate the whole config section read the worktree
# and the index in every mode. So in ship mode `git rm --cached
# frontend/vitest.config.ts` -- the worktree copy untouched, the deletion staged
# for a later commit -- refused a docs-only push with "exists on disk but is not
# in the git index", prescribing `git add` for a commit that is not being made.
# The changeset filter cannot take this check out of the way either: it is on
# preflight's always-run list, so it is the one check a docs-only push cannot
# avoid. ci/checks/node.sh removed the identical false red for the same
# directory's package.json, and gave the reason there.
config_in_vouched_tree() {
  if [ "${CI_GATE_MODE:-}" = "ship" ]; then
    command -v git >/dev/null 2>&1 || return 1
    git rev-parse --git-dir >/dev/null 2>&1 || return 1
    git rev-parse --verify HEAD >/dev/null 2>&1 || return 1
    git cat-file -e "HEAD:$VITEST_CONFIG" 2>/dev/null
    return
  fi
  [ -f "$VITEST_CONFIG" ]
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
      # No colon at this depth means the segment is not `name: value`. A
      # shorthand property -- `{ include }` -- binds the property to the
      # variable of that name, and that value is nowhere in this config text; a
      # method or getter is the same statement one shape over.
      #
      # Reported under a sentinel rather than under its own name with an empty
      # value, which is what it used to do. `include: [broad], include` emitted
      # a second `include` line carrying nothing: JavaScript applies the later
      # property, so vitest collected whatever the *variable* holds, while the
      # reader below still saw the earlier broad array in the list and passed
      # the config. A value this scan cannot read is not an empty value, and the
      # allow-list cannot judge a property whose value it never saw either -- so
      # the whole block is answered as unevaluable, the way a spread already is.
      if (colon == 0) { print "!shorthand" "\t" unquote(seg); return }
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
      # Whether this scan came out balanced, which is not the same question as
      # what it found. This program has no notion of a regular expression, and
      # extract_test_block hands it whole blocks: `alias: [{ find: /'"'"'/ }]` opens
      # a string at the apostrophe inside the regex and swallows every property
      # after it, and `find: /^\(/` leaves depth stuck above zero so no further
      # comma is ever at segment level. Either one hides a live `exclude` or a
      # live `testNamePattern` from the rule that exists to catch it, and hides
      # it from the unknown-property backstop behind that -- so the guard
      # reported "Test layout OK" over a config that collects almost nothing.
      #
      # Reported rather than parsed. Teaching a second reader about regular
      # expressions is how the two readers drift apart; what the caller needs to
      # know is that this one could not finish, and "I could not read it" is a
      # different answer from "I read it and there was nothing there".
      if (instr != "" || depth != 0) print "!unbalanced" "\t"
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
            # Anything after the outer bracket makes this an expression -- with
            # the exception of a type assertion, which is not one. `as const` and
            # `satisfies string[]` are erased before vite or esbuild evaluates
            # this file, so the value vitest receives is the literal array that
            # was just checked. Refusing them failed a correct config in a .ts
            # file with "computes test.include instead of declaring it", which is
            # the mode that gets a hook switched off.
            #
            # Named, not permitted by shape: the tail must begin with one of the
            # two keywords and may then carry only what a type expression is made
            # of. A call, a quote, a template or a ternary in there is an
            # expression again, and this returns to refusing it.
            rest = trim(substr(v, i + 1))
            if (rest != "" \
                && (rest !~ /^(as|satisfies)[[:space:]]/ \
                    || rest ~ /[("'"'"'`?+!&:;]/)) ok = 0
            exit ok ? 0 : 1
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

# literal_array_elements – one unquoted element per line, for a value
# value_is_literal_array has already accepted.
#
# The declared glob was looked for with `grep -F` over the joined text of the
# value, and a substring is not an element. `["src/tests/**/*.test.{ts,tsx}",
# "tests/lib/api/one.test.ts"]` contains the declared glob as text while
# collecting one file: the first element is dead -- nothing lives under
# frontend/src/tests -- and it exists only to carry the string this check greps
# for. The guard reported "38 file(s), all under frontend/tests/" while vitest
# listed one, and 37 suites were skipped behind a green lane. `noop/tests/...`,
# `tests/**/*.test.{ts,tsx}.bak` and any other element with the glob inside it
# do the same thing.
#
# The elements are already parsed one value up; this prints them so the caller
# can ask for equality instead of for containment.
literal_array_elements() {
  awk '
    { v = v $0 " " }
    function trim(s) { sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s); return s }
    function unq(s,   q) {
      s = trim(s)
      if (length(s) < 2) return s
      q = substr(s, 1, 1)
      if ((q == "\"" || q == "'"'"'" || q == "`") && substr(s, length(s), 1) == q) {
        return substr(s, 2, length(s) - 2)
      }
      return s
    }
    END {
      v = trim(v)
      n = length(v)
      if (n < 2 || substr(v, 1, 1) != "[") exit 0
      depth = 0; instr = ""; seg = ""
      for (i = 1; i <= n; i++) {
        c = substr(v, i, 1)
        if (instr != "") {
          seg = seg c
          if (c == "\\") { seg = seg substr(v, i + 1, 1); i++; continue }
          if (c == instr) instr = ""
          continue
        }
        if (c == "\"" || c == "'"'"'" || c == "`") { instr = c; seg = seg c; continue }
        if (c == "[" || c == "{" || c == "(") { depth++; if (depth > 1) seg = seg c; continue }
        if (c == "]" || c == "}" || c == ")") {
          depth--
          if (depth == 0) { if (trim(seg) != "") print unq(seg); exit 0 }
          seg = seg c
          continue
        }
        if (c == "," && depth == 1) { if (trim(seg) != "") print unq(seg); seg = ""; continue }
        seg = seg c
      }
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
  # Four answers now. A composed export -- Object.assign, mergeConfig, any
  # wrapper this reader has not been taught -- is not "no test block": the block
  # is right there, and the point is that it may not be the one that survives
  # composition. Saying "no readable test block" would send someone looking for
  # a missing block that is not missing.
  if [ "$_xt_rc" -eq 3 ]; then
    printf 'composed-config'
    return 0
  fi
  # An expression after the exported object composes it just as a wrapper in
  # front of it does, and the same answer fits: the block this guard read may
  # not be the one vitest receives.
  if [ "$_xt_rc" -eq 4 ]; then
    printf 'composed-config'
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
      $1 == "" || $1 == "..." || $1 == "!shorthand" || $1 == "!unbalanced" { next }
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
  elif printf '%s\n' "$props" | grep -q '^!unbalanced'; then
    # The property scan did not come out balanced, so the properties it did
    # report are the ones before the point it lost track -- and everything after
    # that point was never offered to the exclude rule, the spread rule or the
    # allow-list. Answered before any of them, because each of those is a
    # statement about a complete reading.
    printf 'props-unreadable'
  elif printf '%s\n' "$props" | grep -q '^!shorthand'; then
    # A property whose value is a name rather than a literal. JavaScript applies
    # the last property of a given name, so `include: [broad], include` runs
    # whatever the *variable* holds -- and the reader saw the broad array,
    # because a shorthand used to be emitted under its own name with an empty
    # value and an empty value loses to the earlier one. Answered before the
    # include rules for the same reason props-unreadable is: each of them is a
    # statement about a value this scan actually read.
    printf 'shorthand-prop\t%s' \
      "$(printf '%s\n' "$props" | awk -F'\t' '$1 == "!shorthand" { print $2 }' | tr '\n' ' ')"
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
  elif [ -z "$active_include" ] \
    || ! printf '%s\n' "$active_include" | literal_array_elements | grep -qxF "$DECLARED_GLOB"; then
    # An *element* equal to the declared glob, not the glob somewhere in the
    # text. include entries are unioned, so one element carrying the declared
    # glob is what makes every declared file collected; an element that merely
    # contains it as a substring collects something else entirely, and the dead
    # element carrying the text was invisible to a containment test.
    printf 'no-include'
  elif [ -n "$active_exclude" ]; then
    printf 'has-exclude\t%s' "$active_exclude"
  elif [ -n "$active_spread" ]; then
    printf 'has-spread\t%s' "$active_spread"
  elif [ -n "$unknown_props" ]; then
    printf 'unknown-prop\t%s' "$(printf '%s' "$unknown_props" | tr '\n' ' ')"
  fi
}

# A workspace file beside the config takes collection away from it entirely.
#
# vitest resolves vitest.workspace.{ts,mts,cts,js,mjs,cjs,json} and
# vitest.projects.* before it reads the root config's test.include, and from
# then on the projects decide what is collected. So a
# frontend/vitest.workspace.ts naming one file made this guard report "38
# file(s), all under frontend/tests/" while vitest listed one -- with
# frontend/vitest.config.ts entirely correct and entirely unused.
#
# The same narrowing spelled *inside* the config, as `projects: [...]`, is
# already refused by the allow-list. The rule existed and was absent on the
# equivalent spelling one file over, which is the shape this branch keeps
# finding. Nothing in ci/ mentioned these filenames at all, and they are not
# test files, so no other section of this check would ever look at them.
#
# Refused rather than read: this guard validates one declared include, and a
# workspace is a list of configs each with its own. The index is consulted as
# well as the worktree, for the same reason the config check is -- a file that
# is only staged is still in the commit being made.
WORKSPACE_NAMES=(
  vitest.workspace.ts vitest.workspace.mts vitest.workspace.cts
  vitest.workspace.js vitest.workspace.mjs vitest.workspace.cjs
  vitest.workspace.json
  vitest.projects.ts vitest.projects.mts vitest.projects.cts
  vitest.projects.js vitest.projects.mjs vitest.projects.cjs
  vitest.projects.json
)

workspace_files_present() {
  local _wf_name _wf_path _wf_found="" _wf_git=0
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    _wf_git=1
  fi
  # In ship mode the pushed tree is the only source, for the reason
  # candidate_files reads it alone: the worktree and the index are the commit
  # being built, and an untracked local vitest.workspace.ts is not in the tree
  # being sent. HEAD was added here as a *third* source and the other two were
  # left in, which is the same half-fix candidate_files had.
  local _wf_ship=0
  if [ "${CI_GATE_MODE:-}" = "ship" ] && [ "$_wf_git" -eq 1 ] \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    _wf_ship=1
  fi
  for _wf_name in "${WORKSPACE_NAMES[@]}"; do
    _wf_path="${FRONTEND_DIR}/${_wf_name}"
    if [ "$_wf_ship" -eq 0 ] && [ -f "$_wf_path" ]; then
      _wf_found="${_wf_found}${_wf_path}"$'\n'
      continue
    fi
    if [ "$_wf_ship" -eq 0 ] && [ "$_wf_git" -eq 1 ] \
      && git ls-files --error-unmatch -- "$_wf_path" >/dev/null 2>&1; then
      _wf_found="${_wf_found}${_wf_path} (staged)"$'\n'
      continue
    fi
    # And the pushed commit, for the reason config_sources reads it: in ship
    # mode the commit already exists, so the worktree and the index can both
    # hold a repair the pushed tree does not. A HEAD carrying a narrowing
    # vitest.workspace.ts, with its deletion staged locally, was reported as
    # having no workspace file at all -- and vitest resolves a workspace file
    # before vitest.config.ts, so the tree going out collects whatever that file
    # says while this guard validated a config it does not reach.
    #
    # One rule, two trees, again: the main config had this fixed and the file
    # that overrides it did not.
    if [ "$_wf_git" -eq 1 ] && [ "${CI_GATE_MODE:-}" = "ship" ] \
      && git rev-parse --verify HEAD >/dev/null 2>&1 \
      && git cat-file -e "HEAD:${_wf_path}" 2>/dev/null; then
      _wf_found="${_wf_found}${_wf_path} (in the pushed commit)"$'\n'
    fi
  done
  printf '%s' "$_wf_found"
}

WORKSPACE_FOUND="$(workspace_files_present)"
if [ -n "$WORKSPACE_FOUND" ]; then
  fail "A vitest workspace file sits beside ${VITEST_CONFIG}:"
  printf '%s' "$WORKSPACE_FOUND" | while IFS= read -r _wf_line; do
    [ -n "$_wf_line" ] || continue
    echo "    ${_wf_line}"
  done
  echo "  vitest resolves a workspace before the root config, and from then on"
  echo "  the projects it lists decide what is collected — so the include this"
  echo "  guard validates in ${VITEST_CONFIG} is not the one in force."
  echo "  The same narrowing written as 'projects: [...]' inside the config is"
  echo "  already refused here; this is that spelling one file over."
  echo "  Remove the workspace file and declare the layout in"
  echo "  ${VITEST_CONFIG}, or teach this guard to read it in the same commit."
fi

if ! config_in_vouched_tree; then
  if [ "${CI_GATE_MODE:-}" = "ship" ]; then
    fail "The commit being pushed carries no ${VITEST_CONFIG}."
    echo "  Nothing in the pushed tree declares the layout, so vitest would fall"
    echo "  back to its default glob and collect whatever it finds. Commit the"
    echo "  config, or restore it if its deletion was committed by mistake."
  else
    fail "Missing ${VITEST_CONFIG}; cannot confirm the test layout is declared."
  fi
# The index is the pre-commit gate's question and only its question. In ship
# mode the commit already exists and the arm above has read it; asking the index
# as well is how a staged local repair failed a push that does not carry it.
elif [ "${CI_GATE_MODE:-}" != "ship" ] && config_missing_from_index; then
  fail "${VITEST_CONFIG} exists on disk but is not in the git index."
  echo "  The commit being made would carry no vitest config, so nothing would"
  echo "  declare the layout and vitest would fall back to its default glob."
  echo "  Stage the config (git add ${VITEST_CONFIG}), or restore it if the"
  echo "  deletion was staged by mistake."
else
  # The producer's status is taken before its output is used.
  #
  # `done <<< "$(config_sources)"` discarded it: the function's `exit
  # FAIL_INFRA` ends only the command substitution, the here-string then
  # carries whatever reached stdout first, and the loop's own success became
  # the script's. So the arm added for an unreadable staged config printed its
  # diagnostic and the check went on to report "Test layout OK" and exit 0 --
  # the guard against a config nobody could read, reporting PASS.
  #
  # The case covering that arm called `config_sources` directly, so it proved
  # the function and not the path through it. There is a case for the path now.
  _CFG_SOURCES=""
  _CFG_RC=0
  _CFG_SOURCES="$(config_sources)" || _CFG_RC=$?
  if [ "$_CFG_RC" -ne 0 ]; then
    exit "$_CFG_RC"
  fi

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
      props-unreadable)
        fail "${_label} has a test block this guard cannot read to the end."
        echo "  The property scan ran out of the block with a string still open or"
        echo "  a bracket still unclosed, which is what a regular expression does"
        echo "  to it: a quote inside the pattern opens a string, and an"
        echo "  unbalanced bracket leaves the depth counted and never closed."
        echo "  Everything after that point was never offered to the exclude"
        echo "  rule, the spread rule or the allow-list, so a live test.exclude"
        echo "  or testNamePattern sitting there would have been reported as"
        echo "  absent. This is 'could not read', not 'nothing there'."
        echo "  Move the regular expression out of the test block — into a"
        echo "  named const above the config — or teach this reader about"
        echo "  regular expressions in the same commit."
        ;;
      shorthand-prop)
        fail "${_label} has a test property whose value this guard cannot read: ${_detail}"
        echo "  A shorthand property -- { include } rather than include: [...] --"
        echo "  takes its value from a variable of that name, and that value is"
        echo "  nowhere in this file. JavaScript applies the last property of a"
        echo "  given name, so"
        echo "    test: { include: [\"${DECLARED_GLOB}\"], include }"
        echo "  runs whatever the variable holds while the declared glob sits"
        echo "  above it, unused and satisfying every check here. A method or a"
        echo "  getter of an allowed name does the same thing."
        echo "  Write the property out -- include: [\"${DECLARED_GLOB}\"] -- so what"
        echo "  vitest collects is what this file says it collects."
        ;;
      composed-config)
        fail "${_label} composes its exported config, so the block read here may not be the one vitest uses."
        echo "  defineConfig(Object.assign({ test: { include: [\"${DECLARED_GLOB}\"] } },"
        echo "  { test: { include: [\"tests/only.test.ts\"] } })) validates the first"
        echo "  object and runs the second: Object.assign replaces test wholesale."
        echo "  This guard reads one object literal, so a wrapper that merges"
        echo "  several is something it cannot follow -- and neither is an"
        echo "  expression *after* it: 'defineConfig({...}) && defineConfig({...})'"
        echo "  exports the right-hand config because the left one is truthy."
        echo "  Export a single literal config — 'export default defineConfig({ ... })'"
        echo "  or 'export default { ... }' — or teach this guard the wrapper in"
        echo "  the same commit."
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
  done <<< "$_CFG_SOURCES"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if [ "$RESULT" = "$CI_RESULT_PASS" ]; then
  # Counted from the candidate list, which is the tree this run stands behind.
  #
  # Fourth reader of "which tree" in this file, found by sweeping for the other
  # three rather than waiting for the next review to report it. This one decides
  # nothing -- RESULT is already PASS here -- but the number it prints is read as
  # "this is what the push contains", and walking the worktree in ship mode
  # counts a developer's untracked scratch tests into a figure describing the
  # pushed commit, and omits the ones only that commit carries.
  TEST_COUNT=0
  for _rc_f in ${ALL_CANDIDATES[@]+"${ALL_CANDIDATES[@]}"}; do
    [ -n "$_rc_f" ] || continue
    case "$_rc_f" in
      "$TESTS_DIR"/*.test.ts|"$TESTS_DIR"/*.test.tsx) ;;
      *) continue ;;
    esac
    path_is_pruned "$_rc_f" && continue
    TEST_COUNT=$((TEST_COUNT + 1))
  done
  ci::log::info "Test layout OK: ${TEST_COUNT} file(s), all under ${TESTS_DIR}/ matching '${DECLARED_GLOB}'."
  exit "$CI_RESULT_PASS"
fi

ci::log::error "Frontend test layout check failed."
exit "$RESULT"
