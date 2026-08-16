#!/usr/bin/env bash
# ci/checks/tests.sh – Multi-language test runner.
# Outputs JUnit XML to ci/reports/junit/<lang>.xml.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/log.sh
source "$ROOT_DIR/ci/lib/log.sh"
# shellcheck source=ci/lib/junit.sh
source "$ROOT_DIR/ci/lib/junit.sh"
# shellcheck source=ci/lib/affected.sh
source "$ROOT_DIR/ci/lib/affected.sh" 2>/dev/null || true
# shellcheck source=ci/lib/git.sh
# For ci::git::push_range: in ship mode the affected-test narrowing below has to
# come from the commits being pushed, and this is the one place that answers
# which those are. Sourced rather than reimplemented, for the reason git.sh's
# own header gives -- a second copy is how the last duplicated range in this
# gate drifted out of step with the first.
#
# Unguarded, like branch-protection.sh, changed-files.sh, git-safety.sh and
# node.sh source it: a missing ci/lib/git.sh is a broken checkout, not an
# optional feature. affected.sh above is guarded because the narrowing really is
# optional; the range this run stands behind is not.
source "$ROOT_DIR/ci/lib/git.sh"

cd "$ROOT_DIR"

JUNIT_DIR="$ROOT_DIR/ci/reports/junit"
OVERALL_RESULT=$CI_RESULT_PASS

# ---------------------------------------------------------------------------
# Affected test selection
# ---------------------------------------------------------------------------
AFFECTED_TESTS=""
if type ci::affected::get_affected_tests >/dev/null 2>&1 && [ -f "ci/config/affected.yml" ]; then
  # Which tree this narrowing is derived from. The index and the worktree are
  # the right answer for quick, and the wrong one for ship: a push carries
  # *committed* changes, which appear in neither. So a branch whose frontend
  # work is committed and clean, with one unrelated Python edit staged, produced
  # an AFFECTED_TESTS that was non-empty and contained only Python patterns --
  # and a non-empty list narrows. `_tests_filter_affected javascript` then came
  # back empty and tests-js reported "skipped: no affected JavaScript tests"
  # over a push whose JS the gate never ran.
  #
  # Non-empty is what narrows, so being unable to ask has to leave this empty
  # rather than partly filled: no narrowing runs every suite, which is the
  # direction that cannot skip a pushed change. That covers a bare tip too --
  # push_range returns one when it finds no base, and `git diff --name-only
  # <tip>` would compare the tip against the worktree rather than listing what
  # the push adds.
  # Read with `-z`, because git quotes what it cannot print. By default
  # `git diff --name-only` renders a path with a non-ASCII byte in it as a
  # C-quoted string -- `frontend/src/café.ts` arrives as
  # `"frontend/src/caf\303\251.ts"` -- and that string matches no rule in
  # ci/config/affected.yml. On its own that means one file contributes no
  # pattern; combined with any mapped Python file in the same range it is a
  # skip, because AFFECTED_TESTS is then non-empty, the JavaScript slice is
  # empty, and a non-empty list narrows. `skipped: no affected JavaScript tests`
  # over a push that changed the frontend.
  #
  # ci/lib/changeset.sh already reads its list this way, and ci/lib/common.sh's
  # ls_tree_paths does the same for a tree listing -- so this is the third
  # reader of a git path list in this gate and the last one still taking the
  # quoted form.
  #
  # A path carrying a newline cannot survive the newline-joined shape this
  # feeds, and a list silently short by an entry is the same skip in another
  # spelling. So it is refused, and a refusal here leaves the list empty --
  # which does not narrow, and running every suite is the direction that cannot
  # skip a pushed change.
  _tests_diff_paths() {
    local _dp_out
    _dp_out="$(git diff -z --name-only "$@" 2>/dev/null | tr '\000\012' '\012\001'
               exit "${PIPESTATUS[0]}")" || return 1
    case "$_dp_out" in
      *$'\001'*) return 2 ;;
    esac
    printf '%s' "$_dp_out"
  }

  _changed_files=""
  if [ "${CI_GATE_MODE:-}" = "ship" ]; then
    _tests_range=""
    if type ci::git::push_range >/dev/null 2>&1; then
      _tests_range="$(ci::git::push_range 2>/dev/null || true)"
    fi
    case "$_tests_range" in
      *..*) _changed_files="$(_tests_diff_paths "$_tests_range" || true)" ;;
      *)    ci::log::info "No push range to narrow by; running every suite." ;;
    esac
  else
    _changed_files="$(_tests_diff_paths --cached || true)"
    if [ -z "$_changed_files" ]; then
      ci::log::info "No staged files found; falling back to unstaged changes for ad-hoc test selection."
      _changed_files="$(_tests_diff_paths || true)"
    fi
  fi
  if [ -n "$_changed_files" ]; then
    AFFECTED_TESTS="$(while IFS= read -r f; do
      [ -n "$f" ] && ci::affected::get_affected_tests "$f"
    done <<< "$_changed_files" | sort -u || true)"
  fi
fi

# A glob from ci/config/affected.yml as the regex Jest actually wants.
#
# `--testPathPattern` is documented as a regexp, and the values in affected.yml
# are globs -- `frontend/tests/**/*.test.ts`. Passed through unchanged they are
# not that pattern in a stricter dialect; they are a different pattern. `**` is
# a quantifier applied to a quantifier, which JavaScript's RegExp rejects
# outright ("Nothing to repeat"), and where it does parse, `.` matches any
# character so `a.test.ts` would also match `axtestxts`.
#
# Vitest is unaffected -- it takes its filter positionally as a substring -- so
# this only ever bit a Jest workspace, which is why nothing here caught it.
#
# Order matters: the regex metacharacters are escaped first, so `.` is already
# `\.` and the `*` handling below cannot be confused by one. Then `**\/` before
# `**` before `*`, longest first, or the shorter rule eats the longer one's
# first character.
_tests_glob_to_regex() {
  local _g="$1"
  # `$` sits last in the bracket expression rather than beside `(`. A POSIX
  # bracket expression is an unordered set, so the escaped characters are
  # exactly the same either way -- but spelled `$()` the literal text reads as a
  # command substitution that single quotes will never expand, which is a real
  # mistake in most contexts and one ShellCheck is right to flag on sight
  # (SH-2016). Ordering the set around the linter is cheaper than teaching every
  # future reader that this one `$(` is inert.
  _g="$(printf '%s' "$_g" | sed -e 's/[].[^(){}+|\\$]/\\&/g')"
  _g="${_g//\*\*\//<<ANY_DIRS>>}"
  _g="${_g//\*\*/<<ANY>>}"
  _g="${_g//\*/[^\/]*}"
  _g="${_g//<<ANY_DIRS>>/(.*\/)?}"
  _g="${_g//<<ANY>>/.*}"
  printf '%s' "$_g"
}

# The files a configured test glob from ci/config/affected.yml actually names,
# one per line, or nothing.
#
# The values there are globs -- `tests/**/test_*.py` -- and a glob is not a path.
# Which runners can take one is a property of the runner: Vitest reads a
# positional as a substring filter, Jest takes a regex (which is what
# _tests_glob_to_regex above exists to produce), and pytest takes neither. It
# documents its positionals as `file_or_dir`, and hands anything else back as a
# usage error before collecting a single test.
#
# Walked with `find` rather than left to the shell, because `**` is only a
# distinct pattern under `shopt -s globstar` -- a Bash 4 feature, and this gate
# still targets the 3.2 that ships on macOS. Without it `tests/**/test_*.py`
# expands as `tests/*/test_*.py`, which silently drops every test nested deeper
# than one level: a narrowing that runs some of the affected suite and reports
# on all of it.
_tests_expand_glob() {
  local _g="$1" _root _name
  case "$_g" in
    *'**/'*)
      _root="${_g%%\*\**}"
      _name="${_g##*\*\*/}"
      ;;
    *)
      # No `**` to walk: it is either a path that exists or it is nothing.
      [ -e "$_g" ] && printf '%s\n' "$_g"
      return 0 ;;
  esac
  _root="${_root%/}"
  [ -n "$_root" ] || _root="."
  [ -d "$_root" ] || return 0
  # A `/` surviving in the tail means the pattern names directories after the
  # `**` -- `tests/**/unit/test_*.py` -- and `-name` matches a basename only.
  case "$_name" in
    */*) find "$_root" -type f -path "*/$_name" 2>/dev/null | sed 's|^\./||' ;;
    *)   find "$_root" -type f -name "$_name" 2>/dev/null | sed 's|^\./||' ;;
  esac
}

_tests_filter_affected() {
  local lang="$1"
  local pattern
  [ -n "$AFFECTED_TESTS" ] || return 0

  while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    case "$lang:$pattern" in
      javascript:*.js|javascript:*.jsx|javascript:*.ts|javascript:*.tsx|javascript:*.mjs|javascript:*.cjs)
        printf '%s\n' "$pattern"
        ;;
      python:*.py)
        printf '%s\n' "$pattern"
        ;;
      go:*_test.go)
        printf '%s\n' "$pattern"
        ;;
      rust:*.rs)
        printf '%s\n' "$pattern"
        ;;
    esac
  done <<< "$AFFECTED_TESTS"
}

_tests_tool_missing() {
  ci::log::info "skipped: ${1} not installed"
}

_tests_record_failure() {
  local lang="$1" msg="$2"
  OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_NEW_ISSUE")"
  ci::log::error "${lang} tests failed: ${msg}"
}

# ---------------------------------------------------------------------------
# Per-language test functions
# ---------------------------------------------------------------------------

# Resolve and run the test runner for one workspace.
#
# Workspace-local binaries are tried before anything on PATH. This is not a
# preference — npx walks up out of the workspace when the local bin directory
# lacks the shim name it expects, and will happily run a different major
# version of vitest than the lockfile pins, against a different vite. That
# produces mass phantom failures that look like real test breakage.
#
# Returns 127 when no runner is reachable, and 30 when the workspace does not
# say which of several installed runners owns its suite.
#
# _tests_js_declared_runner – the runner the workspace's own `test` script
# names, or the empty string when it names none this lane can run.
#
# The manifest is the workspace's statement about what its suite is; an
# installed binary is only evidence that something depends on one. A workspace
# migrating from Jest to Vitest has both, and picking by a fixed order ran
# Vitest against a Jest suite: Vitest collects nothing it recognises, exits 0,
# and `tests-js` reports PASS while every Jest test stays uncollected. A
# transitive dependency pulling Vitest in does the same thing with nobody having
# chosen anything at all.
#
# Read with node, the same way ci/checks/node.sh reads this file, because a
# manifest is JSON and a grep for a runner's name finds it in a dependency entry
# or a comment-shaped string just as readily as in the script that runs it.
# --- BEGIN declared-runner reader ---------------------------------------------
#
# Cut out as a block by ci/tests/test_js_lane.bats, which drives this reader
# directly rather than through the lane -- reaching it through the lane means
# installing and running a real suite for a question that is one pass of
# parsing. Three cases there used to cut on `/^_tests_js_declared_runner()/,/^}/`
# and so could only ever see one function; the markers are what they cut on now,
# so a helper added here arrives in all three without a fourth extraction site
# having to be found.

# How far a `<pm> run <script>` chain is followed before the reader gives up.
# Two scripts that name each other, or one that names itself, would otherwise
# not terminate. Giving up declares nothing, and nothing is the safe answer
# here: the caller refuses a workspace that installs both runners and declares
# neither, so a chain this reader cannot follow is reported rather than guessed.
_TESTS_JS_RUNNER_MAX_DEPTH=8

# The body of a named script in ./package.json, or nothing.
#
# Read with node, for the reason the `test` script is read with node: a manifest
# is JSON, and a script name is a key, not a line.
_tests_js_script_body() {
  ci::common::command_exists node || return 1
  node -e "try{const p=require('./package.json');const s=(p.scripts||{})[process.argv[1]];if(typeof s!=='string')process.exit(4);process.stdout.write(s)}catch(e){process.exit(3)}" "$1" 2>/dev/null
}

# The command whose contract this lane reproduces: the workspace's `test`
# script, or `test:unit` when there is no `test`.
#
# Both are a test contract as far as this lane is concerned. The branch that
# decides a workspace is not test-free accepts either, and ci/checks/node.sh
# runs both -- so a reader that only ever looked at `test` was answering about a
# script that need not exist. A workspace whose whole suite is
# `"test:unit": "RUN_INTEGRATION=1 vitest run"` had its runner resolved from an
# empty string and its environment read as none, so the pinned runner was
# invoked with RUN_INTEGRATION unset, every case guarded on that value skipped
# itself, and the lane reported PASS over a suite that ran its cheap half.
#
# `test` first when both exist, because that is the script the node lane's
# umbrella rule treats as the entry point and the one a delegation would go
# through.
#
# One reader for both questions -- which runner, and which environment -- since
# they are two projections of one command and the whole point of the scanner
# below is that they come from a single parse. Asking them of different scripts
# is the same drift one layer up.
# Non-zero when the manifest could not be read at all, which is not the same
# answer as "it declares no test script" and must not be spelled the same way.
# `_tests_js_script_body` says which happened: 4 is "no such script", while 3 --
# the manifest does not parse -- and 1 -- no node to parse it with -- are the
# reader failing. Swallowing those let a workspace with `{ invalid json` and one
# installed runner reach the loop below and run that runner directly.
_tests_js_contract_command() {
  ci::common::command_exists node || return 1
  local _cc="" _cc_rc=0
  _cc="$(_tests_js_script_body test)" || _cc_rc=$?
  case "$_cc_rc" in
    0) [ -z "$_cc" ] || { printf '%s' "$_cc" ; return 0 ; } ;;
    4) ;;
    *) return 1 ;;
  esac
  _cc="" ; _cc_rc=0
  _cc="$(_tests_js_script_body test:unit)" || _cc_rc=$?
  case "$_cc_rc" in
    0|4) printf '%s' "$_cc" ; return 0 ;;
    *) return 1 ;;
  esac
}

_tests_js_declared_runner() {
  local _cmd
  _cmd="$(_tests_js_contract_command)" || return 1
  _tests_js_scan_runner "$_cmd" 0
}

# Which runner a command string names, following the delegations a manifest is
# allowed to use to reach one.
# _tests_js_take_payload – consume the words of a `-c`-style command payload
# and leave it in `_pl`, one layer of surrounding quoting removed.
#
# Gathered word by word so a separator *outside* the payload ends it: in
# `bash -c 'echo hi' && vitest run` the payload is `echo hi` and vitest is the
# command after it, while in `bash -c 'jest --ci && echo done'` the `&&` is
# inside the quotes and the payload runs to the end. Quote state is what tells
# those apart, and it is tracked here only -- the scan at large deliberately
# keeps quotes attached to their words, which is what stops `"a&&vitest"` from
# promoting an argument to a runner.
#
# Reads and advances `_words`/`_i`/`_n` from _tests_js_scan_runner, its only
# caller, rather than taking them as arguments: an array cannot be passed by
# value in the Bash 3.2 this suite still targets, and `local -n` arrived in 4.3.
# Shared by the shell `-c` arm and npm's `--call`, which are the same grammar
# under two spellings -- one copy so a fix to one cannot miss the other.
_tests_js_take_payload() {
  _pl="" ; _q=""
  while [ "$_i" -lt "$_n" ]; do
    _tok="${_words[$_i]}"
    if [ -z "$_q" ]; then
      case "$_tok" in
        ';'|'&&'|'||'|'|'|'&') break ;;
      esac
    fi
    if [ -n "$_pl" ]; then _pl="$_pl $_tok" ; else _pl="$_tok" ; fi
    _k=0
    while [ "$_k" -lt "${#_tok}" ]; do
      _ch="${_tok:$_k:1}"
      if [ -z "$_q" ]; then
        case "$_ch" in "'"|'"') _q="$_ch" ;; esac
      elif [ "$_ch" = "$_q" ]; then
        _q=""
      fi
      _k=$((_k + 1))
    done
    _i=$((_i + 1))
  done
  # One layer of surrounding quoting comes off. The payload is shell, and what
  # the shell strips before running it has to come off before it is read as
  # shell.
  if [ "${#_pl}" -ge 2 ]; then
    case "$_pl" in
      "'"*"'"|'"'*'"') _pl="${_pl:1:$((${#_pl} - 2))}" ;;
    esac
  fi
}

_tests_js_scan_runner() {
  # `_mode` selects the projection, not the parse: `runner` (the default, and
  # what every caller but _tests_js_declared_env passes) yields the runner's
  # name, `env` the assignment prefix the shell would apply to it. Both walk
  # the identical grammar, which is the point -- see the comment on
  # _tests_js_declared_env.
  local _cmd="$1" _depth="${2:-0}" _mode="${3:-runner}"
  local _w _sub _pl _q _tok _k _ch _env=""
  [ "$_depth" -lt "$_TESTS_JS_RUNNER_MAX_DEPTH" ] || return 0
  # In command position, not anywhere in the string.
  #
  # This scanned every word, so a runner named as an *argument* declared the
  # suite: `echo vitest && jest` reported vitest, this lane ran Vitest over a
  # Jest suite, Vitest collected nothing it recognised and exited 0, and
  # tests-js reported PASS with every Jest test uncollected. That is the exact
  # failure the declared-runner rule was written to prevent, reached by the
  # spelling it does not read.
  #
  # ci/checks/node.sh answers the same question from command position, and the
  # comment above its scanner says the rule has been fixed five times there --
  # every previous attempt losing for asking whether a string *contains* a name
  # rather than whether the shell would *run* it. Presence is not execution.
  #
  # A smaller reader than node.sh's _command_runner on purpose: that one has to
  # identify any checker in any script, and this one only has to say which of
  # two runners a `test` script names. Where it cannot tell it says nothing, and
  # nothing is safe here -- the caller refuses a workspace that installs both
  # runners and declares neither, so silence fails closed rather than guessing.
  # `ci/tests/test_js_lane.bats` pins the two readers against the same spellings
  # so they cannot drift apart about a wrapper.
  # `&&` spaced out before the split, for the reason ci/checks/node.sh spaces
  # it: the loop below splits on whitespace, so `true&&vitest run` arrives as
  # the single token `true&&vitest`, never matches the separator arm, and the
  # scan ends with nothing declared. The caller reads "nothing declared" as
  # ambiguous and returns FAIL_INFRA for a workspace whose test script the shell
  # would run perfectly well.
  #
  # No quote mask here, unlike node.sh, and the boundary is worth stating: a
  # `&&` inside a quoted argument is spaced too. It cannot promote an argument
  # to a runner, because the quote characters stay attached to the token --
  # `"a&&vitest"` becomes `"a` and `vitest"`, and `vitest"` matches no arm. The
  # case below asserts that rather than leaving it to be re-derived.
  # `;` gets the same treatment, and it was not enough to do `&&` alone: the
  # cross-reader case caught `true;vitest run` one spelling after it caught
  # `true&&vitest run`, in the same function, on the same run.
  local _norm="" _ci=0 _cn=${#_cmd}
  while [ "$_ci" -lt "$_cn" ]; do
    if [ "${_cmd:$_ci:2}" = "&&" ]; then
      _norm="$_norm && "
      _ci=$((_ci + 2))
      continue
    fi
    if [ "${_cmd:$_ci:1}" = ";" ]; then
      _norm="$_norm ; "
      _ci=$((_ci + 1))
      continue
    fi
    _norm="$_norm${_cmd:$_ci:1}"
    _ci=$((_ci + 1))
  done
  # Indexed, rather than `for _w in $_norm`, because the shell `-c` arm below
  # needs the words that come *after* the one in hand and a for-loop cannot look
  # at them. Globbing off while the array is built: this string is a script, and
  # a `*.test.ts` in it would otherwise be expanded against whatever directory
  # the lane happens to be standing in -- the same exposure the for-loop had,
  # carried over rather than introduced, and `set -f` is the fix for both.
  local -a _words=()
  local _reset_f=1
  case "$-" in *f*) ;; *) _reset_f=0 ;; esac
  set -f
  # shellcheck disable=SC2206
  _words=( $_norm )
  [ "$_reset_f" -eq 1 ] || set +f

  local _n=${#_words[@]} _i=0
  local _expect=1 _tp=0 _ep=0 _pp=0 _pm_name="" _sh=0
  while [ "$_i" -lt "$_n" ]; do
    _w="${_words[$_i]}"
    _i=$((_i + 1))
    case "$_w" in
      ';'|'&&'|'||'|'|'|'&'|'('|')'|'{'|'}')
        # `_pm_name` and `_sh` are cleared here too. They are only meaningful
        # for the command they were set by, and a manager's name outliving its
        # own command lets `npm ci && run x` read `x` as a delegated script.
        #
        # `_env` for the same reason and it matters more: an assignment prefix
        # belongs to the one command it precedes, so in
        # `SEED=1 node seed.js && vitest run` it reaches seed.js and the runner
        # never sees it. Carrying it forward would hand the runner an
        # environment the shell would not.
        _expect=1 ; _tp=0 ; _ep=0 ; _pp=0 ; _pm_name="" ; _sh=0 ; _env="" ; continue ;;
    esac
    [ "$_expect" -eq 1 ] || continue
    # A wrapper's own option can take its value as a separate word, and that
    # word is not the command. The three grammars node.sh models: timeout's
    # duration, env's -u/-C/-S, and a package manager's --filter/--cwd/--prefix.
    if [ "$_tp" -eq 1 ]; then
      case "$_w" in
        -k|--kill-after|-s|--signal) _tp=2 ; continue ;;
        -*) continue ;;
        *) _tp=0 ; continue ;;
      esac
    elif [ "$_tp" -eq 2 ]; then _tp=1 ; continue
    fi
    if [ "$_ep" -eq 1 ]; then
      case "$_w" in
        -u|--unset|-C|--chdir|-S|--split-string) _ep=2 ; continue ;;
        -*) continue ;;
        [A-Za-z_]*=*) _tests_js_env_take "$_w" ; continue ;;
        *) _ep=0 ;;
      esac
    elif [ "$_ep" -eq 2 ]; then _ep=1 ; continue
    fi
    # npm and npx spell a shell's `-c` as their own: `npm exec --package jest
    # -c 'jest --ci'` runs that string with the package's binaries on PATH, so
    # the value is the command, not an inert option payload. Consumed as an
    # ordinary option value it declared nothing -- and nothing is what sends a
    # workspace carrying a local Vitest to the installed-runner fallback, so
    # Vitest ran over the Jest suite the manifest names, collected nothing it
    # recognised, exited 0, and tests-js reported PASS. The same fail-open the
    # `--package` case above was written to close, reached by the next spelling.
    #
    # Ahead of the package-manager block because `exec` ends that state while
    # leaving `_pm_name` set, so `npm exec -c` arrives here with `_pp` back at
    # 0 and would otherwise be skipped by the generic `-*` option arm. `_pp` of
    # 2 is excluded: the word is then some other option's value, not a flag.
    if [ "$_pp" -ne 2 ]; then
      case "${_pm_name}|$_w" in
        'npm|-c'|'npm|--call'|'npx|-c'|'npx|--call')
          _tests_js_take_payload
          _sub="$(_tests_js_scan_runner "$_pl" "$((_depth + 1))")"
          if [ -n "$_sub" ]; then
            _tests_js_scan_yield "$_sub" "$_pl" "$((_depth + 1))" ; return 0
          fi
          # The payload is what ran and it named no runner; what trails it is
          # an argument to the payload, exactly as in the shell `-c` arm.
          _pp=0 ; _expect=0 ; continue ;;
      esac
    fi
    if [ "$_pp" -eq 1 ]; then
      # Keyed by which manager is in front, which is how ci/checks/node.sh's
      # _pm_advance reads the same grammar. One list cannot describe five
      # vocabularies: `-p` is `--package` to npx and `--parseable` to pnpm, so a
      # shared list either misses npx's value or eats pnpm's command.
      #
      # npx's own value-takers were missing entirely, and that is a fail-open in
      # the sharpest place this file has. `npx --package vitest jest --ci` --
      # the documented way to run a tool without installing it -- read
      # `--package` as valueless and declared `vitest` as the command, so in a
      # workspace carrying both runners this lane ran Vitest while the
      # manifest's Jest suite never ran and the lane reported PASS.
      # `bun x` is the two-word spelling of `bunx`, and `npm x` is the
      # documented alias of `npm exec`. Both hand the next word to a package
      # runner, so both have to stay in package-manager position rather than
      # ending it: read as an ordinary command, `x` made `bun x jest --ci`
      # declare nothing, and nothing sends a workspace carrying a local Vitest
      # to the installed-runner fallback -- Vitest over a Jest suite, nothing
      # collected, exit 0, PASS.
      #
      # Keyed by manager, not added to the transparent list below, because a
      # bare `x` is not a package runner: it is as good a name for a project's
      # own script as any other, and `x vitest` must keep meaning the script.
      #
      # `exec` and `dlx` are the same kind of word and were not treated as one.
      # They reached the transparent list further down, and the arm that sends
      # them there runs `_pp=0` on the way past -- so the option grammar ended
      # at the very subcommand that introduces it. `--package` is not an option
      # of `npm`; it is an option of `npm exec`, which makes
      # `npm exec --package vitest -c 'jest --ci'` -- the documented spelling --
      # the one shape the `--package` case above could never see. `--package`
      # was skipped as an ordinary flag and `vitest` was read as the command, so
      # a workspace carrying both runners ran Vitest while the payload's Jest
      # suite never ran and the lane reported PASS. `npm x` was right only
      # because it was listed here.
      case "${_pm_name}|$_w" in
        'bun|x'|'npm|x' \
          |'npm|exec'|'pnpm|exec'|'yarn|exec'|'bun|exec' \
          |'pnpm|dlx'|'yarn|dlx') continue ;;
      esac
      #
      # `pnpm dlx` and `yarn dlx` take `--package` too, and only in its long
      # spelling: pnpm's `-p` is `--parseable`, which takes no value, so keying
      # the short form to them would eat the command instead of an argument.
      # Without the long form `pnpm dlx --package vitest jest --ci` declared
      # vitest, which is the package to fetch and not the command that runs.
      case "${_pm_name}|$_w" in
        'npx|-p'|'npx|--package' \
          |'npm|-p'|'npm|--package' \
          |'pnpm|--package'|'yarn|--package' \
          |'npm|--prefix'|'npm|-w'|'npm|--workspace'|'npm|-C' \
          |'pnpm|--filter'|'pnpm|-F'|'pnpm|--dir'|'pnpm|-C'|'pnpm|--workspace-dir' \
          |'yarn|--cwd' \
          |'bun|--cwd')
          _pp=2 ; continue ;;
      esac
      case "$_w" in
        -*) continue ;;
        *) _pp=0 ;;
      esac
    elif [ "$_pp" -eq 2 ]; then _pp=1 ; continue
    fi
    # A shell's `-c` payload is the command that runs, so it is read as one.
    # `"test": "bash -c 'jest --ci'"` left the reader with `bash` -- an unknown
    # command, nothing declared -- and a workspace carrying a local Vitest then
    # ran Vitest over the Jest suite the manifest names and reported PASS.
    if [ "$_sh" -eq 1 ]; then
      case "$_w" in
        -c|-[a-zA-Z]*c)
          # The payload, gathered word by word so a separator *outside* it ends
          # it: in `bash -c 'echo hi' && vitest run` the payload is `echo hi`
          # and vitest is the command after it, while in
          # `bash -c 'jest --ci && echo done'` the `&&` is inside the quotes and
          # the payload runs to the end. Quote state is what tells those apart,
          # and it is tracked here only -- the scan at large deliberately keeps
          # quotes attached to their words, which is what stops `"a&&vitest"`
          # from promoting an argument to a runner.
          _tests_js_take_payload
          _sub="$(_tests_js_scan_runner "$_pl" "$((_depth + 1))")"
          if [ -n "$_sub" ]; then
            _tests_js_scan_yield "$_sub" "$_pl" "$((_depth + 1))" ; return 0
          fi
          # The payload is what ran, and it named no runner. Whatever trails it
          # is an argument to the payload -- `bash -c 'true' tsc` passes `tsc`
          # to the payload as a positional, it does not run it.
          _sh=0 ; _expect=0 ; continue ;;
        -*) continue ;;
      esac
      _sh=0
    fi
    case "${_w##*/}" in
      vitest|vitest.cmd|vitest.exe) _tests_js_scan_yield 'vitest' ; return 0 ;;
      jest|jest.cmd|jest.exe) _tests_js_scan_yield 'jest' ; return 0 ;;
      timeout) _tp=1 ; continue ;;
      env|cross-env) _ep=1 ; continue ;;
      npx|bunx|pnpx|pnpm|yarn|npm|bun) _pm_name="${_w##*/}" ; _pp=1 ; continue ;;
      sh|bash|zsh|dash|ksh|ash) _sh=1 ; continue ;;
      run|run-script)
        # `npm run test:unit` does not run a command called `test:unit`; it runs
        # the script of that name, and what *that* names is what this workspace
        # runs. Unfollowed, a manifest that delegates declared nothing, and the
        # caller reads nothing as "fall back to whichever runner is installed"
        # -- so `"test": "npm run test:unit"` over a Jest suite ran Vitest and
        # reported PASS.
        #
        # Only where a manager is in front, and only for a name the manifest
        # actually defines. Where it defines no such script the word is left to
        # be read in command position exactly as before, which is what keeps
        # `bun run vitest` resolving to vitest.
        if [ -n "$_pm_name" ] && [ "$_i" -lt "$_n" ]; then
          _sub="$(_tests_js_script_body "${_words[$_i]}" || true)"
          if [ -n "$_sub" ]; then
            _sub="$(_tests_js_scan_runner "$_sub" "$((_depth + 1))")"
            if [ -n "$_sub" ]; then
              _tests_js_scan_yield "$_sub" \
                "$(_tests_js_script_body "${_words[$_i]}" || true)" "$((_depth + 1))"
              return 0
            fi
            _i=$((_i + 1)) ; _expect=0 ; continue
          fi
        fi
        continue ;;
      nohup|command|exec|time|dlx|--) continue ;;
    esac
    case "$_w" in
      -*) continue ;;
      [A-Za-z_]*=*) _tests_js_env_take "$_w" ; continue ;;
    esac
    # Some other command. What follows are its arguments, and a runner named
    # among them is not the one this script runs.
    #
    # Whatever prefix was collected went to *this* command, so it is dropped
    # here rather than left standing: `SEED=1 node seed.js && vitest run` gives
    # seed.js the variable and the runner nothing.
    _expect=0 ; _env=""
  done
  return 0
}

# _tests_js_declared_env – the environment the workspace's own `test` script
# establishes for the runner it names, one `NAME=value` per line.
#
# This lane runs the pinned runner binary rather than the declared script, for
# the reason the no-PATH-fallback comment further down gives. What that skipped
# was not only the script's wording but the environment it sets:
# `"test": "RUN_INTEGRATION=1 vitest run"` over a suite whose integration cases
# are guarded on that variable ran with it unset, collected the unit cases
# only, and reported PASS -- fewer tests than the declared contract, with
# nothing on screen to say so. A skipped case is not a failing one, so there is
# no noise to notice.
#
# Only the assignments that prefix the command the shell would actually run,
# because that is the whole of what an assignment prefix means: in
# `seed.js && RUN=1 vitest run` the prefix belongs to vitest, and in
# `RUN=1 seed.js && vitest run` it belongs to seed.js and reaches the runner
# never. A separator therefore drops what was collected before it rather than
# carrying it forward.
#
# Not a second parser. `env`/`cross-env`, a shell `-c` payload, npm's `--call`,
# a package runner's option grammar and a `<pm> run <script>` delegation all
# decide where the command begins, and a reader that modelled them a second
# time would be a reader that could disagree with the first about which command
# the assignments prefix. So this is the same walk under a second projection:
# _tests_js_scan_runner takes a mode, and `env` makes it yield the prefix of the
# command it settles on instead of that command's name. One grammar, two
# answers, and no way for them to drift apart.
#
# Nothing else in the declared script is reproduced. A setup *command* before
# the runner cannot be re-run without running the script, which is a change to
# how every JS lane invokes tests and not one to make inside a review round; it
# is stated on the review thread instead of guessed at here.

# Advances `_q` -- the caller's open-quote character, or empty -- across one
# word. Reads and writes the caller's locals for the reason
# _tests_js_take_payload does: Bash 3.2 has no name references.
_tests_js_env_qstep() {
  local _t="$1" _k=0 _ch
  while [ "$_k" -lt "${#_t}" ]; do
    _ch="${_t:$_k:1}"
    if [ -z "$_q" ]; then
      case "$_ch" in "'"|'"') _q="$_ch" ;; esac
    elif [ "$_ch" = "$_q" ]; then
      _q=""
    fi
    _k=$((_k + 1))
  done
}

# One `NAME=value` line, with one layer of quoting off the value -- or a
# `!expansion <NAME>` line where the value is one this lane cannot reproduce.
#
# The quotes are what the shell strips before the assignment happens, so they
# come off before the value is handed to `export`; the name cannot be quoted, so
# only the value is unwrapped.
#
# What cannot come off is a substitution. `RUN_INTEGRATION="$ENABLE_INTEGRATION"`
# is `1` to the shell that runs the script and the eight literal characters
# `$ENABLE_INTEGRATION` to a reader that only unquotes -- so exporting the text
# gives the runner a *different* environment than the manifest declares, and a
# test guarded on `=== "1"` skips itself while the lane reports PASS. That is
# worse than not setting the variable at all, because it looks like it was set.
#
# Expanding it here is not the alternative: a value is arbitrary shell, and
# `RUN=$(rm -rf build)` is as valid a manifest as any. So the assignment is
# reported as unreproducible and the caller refuses the workspace -- the same
# answer, and for the same reason, as the branch that refuses a workspace
# installing both runners and declaring neither. `!expansion` follows
# ci/checks/test-layout.sh's `!shorthand` verdict, which marks the same kind of
# thing: a value that is present, well-formed, and not evaluable here.
#
# Single quotes are exempt, because the shell exempts them: in `RUN='$LITERAL'`
# the dollar sign *is* the value, and re-exporting it verbatim is exact.
_tests_js_env_emit() {
  local _nm="${1%%=*}" _vl="${1#*=}" _sq=0
  if [ "${#_vl}" -ge 2 ]; then
    case "$_vl" in
      "'"*"'") _vl="${_vl:1:$((${#_vl} - 2))}" ; _sq=1 ;;
      '"'*'"') _vl="${_vl:1:$((${#_vl} - 2))}" ;;
    esac
  fi
  if [ "$_sq" -eq 0 ]; then
    case "$_vl" in
      *'$'*|*'`'*) printf '!expansion %s\n' "$_nm" ; return 0 ;;
    esac
  fi
  printf '%s=%s\n' "$_nm" "$_vl"
}

# Collects the assignment in hand into `_env`, taking further words while its
# value carries an unclosed quote: the scan splits on whitespace, so
# `TITLE="a b"` arrives as `TITLE="a` and `b"`, and half an assignment is not
# one. Advancing `_i` past the remainder is also what stops `b"` from being
# read as a command and ending command position -- which is why
# `TITLE="a b" vitest run` used to declare no runner at all.
#
# Reads and writes `_env`/`_q`/`_words`/`_i`/`_n` from _tests_js_scan_runner for
# the reason _tests_js_take_payload does: Bash 3.2 has no name references.
_tests_js_env_take() {
  local _p="$1"
  _q=""
  _tests_js_env_qstep "$_p"
  while [ -n "$_q" ] && [ "$_i" -lt "$_n" ]; do
    _p="$_p ${_words[$_i]}"
    _tests_js_env_qstep "${_words[$_i]}"
    _i=$((_i + 1))
  done
  _env="${_env}$(_tests_js_env_emit "$_p")
"
}

# What the scan settles on, under the projection asked for: in `runner` mode the
# runner's name, in `env` mode the assignments the shell would apply to it.
#
#   $1 – the runner this scan reached
#   $2 – the command string it reached it *through*, if it delegated, so the
#        assignments inside that string can be gathered under the same grammar
#   $3 – the depth to re-enter at
#
# A delegation carries both: `RUN=1 npm run t` with `"t": "X=2 vitest run"`
# exports RUN to npm, which passes its environment on to the script, so the
# runner sees both and the prefix here comes first.
_tests_js_scan_yield() {
  if [ "${_mode}" != env ]; then printf '%s' "$1" ; return 0 ; fi
  local _se=""
  [ -z "${2:-}" ] || _se="$(_tests_js_scan_runner "$2" "${3:-0}" env)"
  printf '%s' "${_env}${_se}"
}

_tests_js_declared_env() {
  local _cmd
  _cmd="$(_tests_js_contract_command)" || return 1
  _tests_js_scan_runner "$_cmd" 0 env
}
# --- END declared-runner reader -----------------------------------------------

# The path-filter option under the spelling the installed Jest actually has.
#
# Jest 30 renamed `--testPathPattern` to `--testPathPatterns` and removed the
# singular form, so emitting it unconditionally makes Jest 30 reject the
# invocation before it executes a single test -- affected-test narrowing turning
# a working suite into a lane failure, on exactly the workspaces the narrowing
# was meant to speed up. Older majors only know the singular spelling, so this
# cannot simply be switched over either; the version decides.
#
# Read from the workspace's own installed copy, for the same reason the runner
# is: a global Jest is not the version this workspace pins. `jest` is the
# package the CLI ships under, `jest-cli` the one it delegates to, and a
# workspace may carry either. Undetectable falls back to the singular spelling,
# which is what every major before 30 accepts and what this lane emitted before
# the option existed in two spellings.
_tests_jest_path_flag() {
  local _v=""
  if ci::common::command_exists node; then
    # Breaks on a version, not on a successful require: a manifest that parses
    # but carries no `version` used to end the search having written nothing, so
    # a workspace whose `jest/package.json` is a stub while `jest-cli` holds the
    # real 30.x fell back to the singular spelling Jest 30 has removed.
    _v="$(node -e "for(const p of ['./node_modules/jest/package.json','./node_modules/jest-cli/package.json']){try{const v=String(require(p).version||'');if(v){process.stdout.write(v);break}}catch(e){}}" 2>/dev/null || true)"
  fi
  case "${_v%%.*}" in
    ''|*[!0-9]*) printf '%s' '--testPathPattern' ; return 0 ;;
  esac
  if [ "${_v%%.*}" -ge 30 ]; then
    printf '%s' '--testPathPatterns'
  else
    printf '%s' '--testPathPattern'
  fi
}

_tests_js_have_runner() {
  local _c
  for _c in "node_modules/.bin/$1" "node_modules/.bin/$1.exe" "node_modules/.bin/$1.cmd"; do
    [ -x "$_c" ] && return 0
  done
  return 1
}

# 0 when the manifest declares a script this lane's suite would run through, 1
# when it definitively declares none, 2 when the manifest could not be read.
#
# Three answers rather than two, because "could not look" is not "found
# nothing", and reading it the second way is what turns an unreadable manifest
# into a skipped workspace. `_tests_js_script_body` says which happened: 4 is
# "no such script", while 1 (no node) and 3 (the manifest does not parse) are
# both the reader failing, and those keep the refusal.
_tests_js_declared_test_contract() {
  local _s _rc
  for _s in test test:unit; do
    _rc=0
    _tests_js_script_body "$_s" >/dev/null 2>&1 || _rc=$?
    case "$_rc" in
      0) return 0 ;;
      4) ;;
      *) return 2 ;;
    esac
  done
  return 1
}

# Does this workspace ship a file *this lane* would have run?
#
# Vitest's and Jest's documented default collection, and deliberately only
# theirs. ci/checks/node.sh asks a wider question -- "does this workspace ship
# tests" -- and counts Cypress specs, Mocha's `test/` and Jasmine's `spec/`
# with them, which is right there: that lane runs the workspace's own `test`
# script, so whichever runner owns those files is the one it invokes. This lane
# runs `vitest` or `jest` itself and nothing else, so the only files that bear
# on whether it has something to run are the ones those two collect. Asking
# node.sh's question here would refuse a Cypress-only workspace for not having
# installed a runner it does not use; asking this one there would let a Cypress
# suite be deleted unnoticed. Two questions, two predicates, and this is why
# they are not shared -- the same reason node.sh records for not sharing with
# ci/checks/test-layout.sh's TEST_SUFFIXES.
#
#   vitest / jest   `**/?(*.)+(spec|test).[jt]s?(x)`  -- `a.test.ts` and a bare
#                   `test.ts`, which is what the `?(*.)` allows
#   jest            `**/__tests__/**/*.[jt]s?(x)`     -- by directory, any depth
#
# Both halves required -- a name shape *and* an extension a runner executes --
# for the reason node.sh records for its own predicate: `openapi.spec.json` is
# an API description, and a workspace shipping one and no test at all must not
# be read as shipping a suite. `*-test.*`, `*_test.*` and Mocha's bare `test/`
# are absent for the opposite reason: neither vitest nor jest collects them, so
# counting them here would refuse a workspace over files this lane could not
# have run either way.
_tests_js_has_suite() {
  local _f
  _f="$(find . \( -name 'node_modules' -o -path './dist' -o -path './build' -o -path './out' -o -path './coverage' -o -path './htmlcov' -o -path './.next' -o -path './.nuxt' -o -path './.turbo' -o -path './.vite' -o -path './.cache' \) -prune -o \
    -type f \( -name '*.test.*' -o -name '*.spec.*' -o -name 'test.*' -o -name 'spec.*' \
      -o -path './__tests__/*' -o -path '*/__tests__/*' \) \
    \( -name '*.js' -o -name '*.jsx' -o -name '*.mjs' -o -name '*.cjs' \
      -o -name '*.ts' -o -name '*.tsx' -o -name '*.mts' -o -name '*.cts' \) \
    -print 2>/dev/null | head -1)"
  [ -n "$_f" ]
}

# Runs "$@" under the environment the declared `test` script sets, and nothing
# else changed. A subshell, so the exports reach the runner and end with it
# rather than following the next workspace in the loop.
#
# Reads `_TJ_DECLARED_ENV` from _tests_js_workspace, its only caller, which
# declares the array before anything can reach here; guarded on the count
# because expanding an empty array under `set -u` is an error in the Bash 3.2
# this suite still targets.
_tests_js_run_declared_env() {
  (
    local _kv
    if [ "${#_TJ_DECLARED_ENV[@]}" -gt 0 ]; then
      for _kv in "${_TJ_DECLARED_ENV[@]}"; do
        export "${_kv%%=*}=${_kv#*=}"
      done
    fi
    "$@"
  )
}

_tests_js_workspace() {
  local ws="$1" junit_out="$2" jest_pattern="$3"
  cd "$ws" || return 30

  local candidate declared="" have_vitest=0 have_jest=0
  _tests_js_have_runner vitest && have_vitest=1
  _tests_js_have_runner jest && have_jest=1

  # A manifest that could not be read is not a manifest that declares nothing.
  #
  # `|| true` collapsed the two, and the consequence was the worse half of this
  # lane's contract: with `{ invalid json` on disk and a single installed runner,
  # `declared` came back empty, the both-runners refusal did not apply, and the
  # loop below ran that runner directly -- reporting PASS for a workspace whose
  # test contract nobody could read. The lane was inferring a contract from what
  # happened to be installed, which is exactly the substitution the
  # declared-but-absent branch below refuses.
  #
  # Status 1 is what the reader returns for both "no node" and "the manifest
  # does not parse", and neither is an answer. It is FAIL_INFRA rather than a
  # failing test, for the reason the missing-runner branch is: nothing was found
  # wrong with the code, the check could not be performed.
  local _tj_dr_rc=0
  declared="$(_tests_js_declared_runner)" || _tj_dr_rc=$?
  if [ "$_tj_dr_rc" -ne 0 ]; then
    echo "Cannot read ${ws}/package.json to find out which runner owns its suite." >&2
    echo "  A manifest that does not parse -- or a missing node to parse it --" >&2
    echo "  is not a workspace that declares no runner, and running whichever" >&2
    echo "  binary happens to be installed would report PASS over a contract" >&2
    echo "  nobody could read." >&2
    return 30
  fi

  # The environment the declared script establishes, carried onto the runner
  # this lane invokes in the script's place. Read here beside the runner so the
  # two answers come from one reading of one manifest.
  local -a _TJ_DECLARED_ENV=()
  local _tj_kv
  while IFS= read -r _tj_kv; do
    [ -n "$_tj_kv" ] || continue
    # An assignment whose value is a substitution is one this lane cannot
    # reproduce, and reproducing it *wrongly* is the failure: the runner would
    # get the literal `$VAR` where the script's own shell gives it a value, so a
    # test guarded on that value skips itself and the lane reports PASS over a
    # suite that never ran the cases the contract enables. Refused rather than
    # approximated, which is the answer the both-runners branch above gives to
    # the same shape of question.
    case "$_tj_kv" in
      '!expansion '*)
        echo "Workspace ${ws} declares a test environment this lane cannot reproduce." >&2
        echo "  '${_tj_kv#!expansion }' is assigned from a shell substitution in the 'test'" >&2
        echo "  script, and this lane runs the pinned runner rather than that script, so" >&2
        echo "  the value would arrive as the literal text instead of what the shell would" >&2
        echo "  produce. A test gated on it then skips itself and this lane reports PASS." >&2
        echo "  Inline the value in the script, or set it outside the manifest." >&2
        return 30 ;;
    esac
    _TJ_DECLARED_ENV+=( "$_tj_kv" )
  done < <(_tests_js_declared_env 2>/dev/null || true)

  # Two runners installed and nothing saying which owns the suite is a question
  # this lane cannot answer, and answering it by order is how the wrong suite
  # runs green. Refused rather than guessed -- the same rule the missing-runner
  # branch below already follows.
  if [ -z "$declared" ] && [ "$have_vitest" -eq 1 ] && [ "$have_jest" -eq 1 ]; then
    echo "Workspace ${ws} installs both vitest and jest and its 'test' script names neither." >&2
    echo "  This lane runs the suite directly, so it has to know which runner owns it:" >&2
    echo "  running the wrong one collects nothing and exits 0, which reports PASS over" >&2
    echo "  a suite that never ran. Name the runner in the workspace's 'test' script." >&2
    return 30
  fi

  # A declared runner that is not installed is not a reason to run the other
  # one. It is the same broken install the no-runner branch reports, and
  # silently substituting a different runner is the defect this block exists to
  # prevent.
  if [ "$declared" = "vitest" ] && [ "$have_vitest" -eq 0 ]; then return 127; fi
  if [ "$declared" = "jest" ] && [ "$have_jest" -eq 0 ]; then return 127; fi

  if [ "$declared" != "jest" ]; then
    for candidate in node_modules/.bin/vitest node_modules/.bin/vitest.exe node_modules/.bin/vitest.cmd; do
      if [ -x "$candidate" ]; then
        _tests_js_run_declared_env \
          "$candidate" run --reporter=junit --outputFile="$junit_out"
        return $?
      fi
    done
  fi

  if [ "$declared" != "vitest" ]; then
    for candidate in node_modules/.bin/jest node_modules/.bin/jest.exe node_modules/.bin/jest.cmd; do
      if [ -x "$candidate" ]; then
        # Spelled here rather than by the caller: the caller builds one pattern
        # for every workspace, and the spelling is a property of the Jest each
        # workspace installs. Quoted, unlike the bare expansion this replaced --
        # the value is one argument, and a regex is not a list of words.
        local _jest_flag
        _jest_flag="$(_tests_jest_path_flag)"
        if ci::common::command_exists node && node -e "require.resolve('jest-junit')" >/dev/null 2>&1; then
          # Appended after the declared assignments rather than written as a
          # command prefix, and the order is the point: the last export wins, so
          # this lane's own reporter path still overrides a same-named variable
          # the script sets -- which is the precedence the command-prefix
          # spelling this replaced already had.
          _TJ_DECLARED_ENV+=( "JEST_JUNIT_OUTPUT_FILE=${junit_out}" )
          _tests_js_run_declared_env \
            "$candidate" --ci --reporters=default --reporters=jest-junit ${jest_pattern:+"${_jest_flag}=${jest_pattern}"}
          return $?
        fi
        _tests_js_run_declared_env \
          "$candidate" --ci ${jest_pattern:+"${_jest_flag}=${jest_pattern}"}
        return $?
      fi
    done
  fi

  # A workspace can also have nothing here to run. Discovery finds `package.json`
  # and schedules this lane for whatever it finds, and a build-only package --
  # a shared config, a generated client, an assets bundle -- has no `test`
  # script, no runner in its dependencies and no file either runner would
  # collect. Reported as FAIL_INFRA, that blocked the push over a workspace
  # that is not broken and has no suite to be broken, while ci/checks/node.sh
  # runs the same package's `build` and passes it.
  #
  # All three conditions, and each one carries its own half of the guard:
  #
  #   no declared script  -- a workspace that says `"test": "vitest run"` has
  #     stated a contract, and a missing runner is that contract unmet. Skipping
  #     it is the failure this branch exists to prevent.
  #   an unreadable manifest is not a missing one -- return 2 keeps the refusal,
  #     because a workspace whose manifest cannot be parsed has not been shown
  #     to declare nothing.
  #   no collectable file -- and this is the one that matters most. A workspace
  #     that ships `a.test.ts` and has lost its script *and* its runner is
  #     exactly the state where reporting PASS means a real suite silently
  #     stopped running. Deleting a script is one keystroke; the files are the
  #     evidence that something was meant to run.
  local _tj_contract=0
  _tests_js_declared_test_contract || _tj_contract=$?
  if [ "$_tj_contract" -eq 1 ] && ! _tests_js_has_suite; then
    return 126
  fi

  # Deliberately no PATH fallback. A global runner is not the version this
  # workspace pins, and the comment above this function is the evidence: an
  # unpinned Vitest against a different Vite produced ~160 phantom failures in
  # this very checkout, indistinguishable from real breakage. Reporting the
  # runner as unavailable is recoverable; a green run of the wrong binary, or a
  # red one nobody can reproduce, is not.
  return 127
}

tests::run_js() {
  local workspaces _ws_rc=0
  # The status is captured rather than left to `set -e`. A non-zero producer in
  # this substitution aborts the whole script -- raw exit 1, outside the
  # 0/10/20/30 contract, with the Python, Go, Rust and shell suites after it
  # never reached -- so an unreadable tree or an ambiguous layout took every
  # language's tests down and reported neither. The same shape, and the same
  # cure, as ci/checks/typecheck.sh beside it.
  workspaces="$(ci::common::node_workspaces package.json)" || _ws_rc=$?
  if [ "$_ws_rc" -ne 0 ]; then
    OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
    ci::log::error "Could not enumerate JavaScript workspaces (exit ${_ws_rc}); see above."
    ci::log::error "  This lane cannot report on a set of workspaces it could not determine."
    return 0
  fi

  if [ -z "$workspaces" ]; then
    ci::log::info "skipped: no package.json found"
    return 0
  fi

  # The push range narrowed *which* tests are selected and nothing about the
  # code they run against: the runner executes the worktree. A test failing in
  # HEAD and repaired only on disk therefore passed, and the push carrying the
  # failure was reported green -- the enabled JS test check skipped by local
  # WIP rather than validating the pushed tree.
  #
  # ci/checks/node.sh refuses this already; tests-js is scheduled as its own
  # blocker lane and never goes through it. Same shared reader as typecheck-js,
  # so there is one answer to "does this worktree match the push" and not three.
  if [ "${CI_GATE_MODE:-}" = "ship" ]; then
    local _tj_ws
    while IFS= read -r _tj_ws; do
      [ -n "$_tj_ws" ] || continue
      ci::common::refuse_on_workspace_drift "$_tj_ws"
    done <<< "$workspaces"
  fi

  local jest_pattern=""
  local js_tests=""
  local pattern
  if [ -n "$AFFECTED_TESTS" ]; then
    js_tests="$(_tests_filter_affected javascript)"
    if [ -z "$js_tests" ]; then
      ci::log::info "skipped: no affected JavaScript tests"
      return 0
    fi
    while IFS= read -r pattern; do
      [ -z "$pattern" ] && continue
      pattern="$(_tests_glob_to_regex "$pattern")"
      if [ -n "$jest_pattern" ]; then
        jest_pattern="${jest_pattern}|${pattern}"
      else
        jest_pattern="$pattern"
      fi
    done <<< "$js_tests"
    # Left as the bare regex. The option's name is chosen per workspace, from
    # the Jest that workspace installs, because Jest 30 removed the singular
    # spelling this used to hard-code.
  fi

  mkdir -p "$JUNIT_DIR"

  local ws rc junit_out label
  while IFS= read -r ws; do
    [ -n "$ws" ] || continue

    if [ "$ws" = "." ]; then
      junit_out="$JUNIT_DIR/js.xml"
      label="JavaScript"
    else
      junit_out="$JUNIT_DIR/js-$(printf '%s' "$ws" | tr '/' '-').xml"
      label="JavaScript (${ws})"
    fi

    ci::log::info "Running JavaScript tests in ${ws}..."

    rc=0
    # Subshell so the cd cannot leak into later languages.
    ( _tests_js_workspace "$ws" "$junit_out" "$jest_pattern" ) || rc=$?

    if [ "$rc" -eq 126 ]; then
      # Not a skip this lane decided on its own: the workspace was inspected and
      # found to declare no test script, install no runner and ship no file
      # either runner collects. See the branch in _tests_js_workspace that
      # returns this for why all three are required before it counts.
      ci::log::info "No JavaScript suite in ${ws} (no test script, no runner, no test files); nothing to run."
      continue
    fi

    if [ "$rc" -eq 127 ]; then
      # A workspace that has a suite and no runner is broken infrastructure,
      # not a skip. The branch above has already separated out the workspace
      # that has neither, so reaching here means something in this workspace --
      # a declared script, a collectable file, or a manifest that could not be
      # read to rule either out -- says a suite is meant to run and it could
      # not be. Continuing let tests-js exit 0 having run no JavaScript at all:
      # an uninstalled node_modules or a missing binary would take a blocking
      # lane green.
      #
      # The same rule ci/checks/tests-shell.sh applies to a missing bats: an
      # enabled blocker that cannot find its runner has not passed.
      OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
      ci::log::error "No workspace-local JS test runner in ${ws}/node_modules/.bin"
      ci::log::error "  (a global jest/vitest is deliberately not used). This workspace was"
      ci::log::error "  detected and scheduled, so reporting PASS here would mean its suite"
      ci::log::error "  never ran. Install its dependencies, or remove the workspace."
      continue
    fi

    if [ "$rc" -eq "$CI_RESULT_FAIL_INFRA" ]; then
      # The workspace could not be entered, or it installs two runners and does
      # not say which owns its suite. Neither is a failing test, and reporting
      # it as one sends someone looking for a broken assertion; it is the same
      # "the suite could not be run" statement the branch above makes.
      OVERALL_RESULT="$(ci::common::merge_results "$OVERALL_RESULT" "$CI_RESULT_FAIL_INFRA")"
      ci::log::error "JavaScript tests in ${ws} could not be run (see above)."
      continue
    fi

    if [ "$rc" -ne 0 ]; then
      _tests_record_failure "$label" "exit code ${rc}"
    fi
  done <<< "$workspaces"

  return 0
}

tests::run_python() {
  if ! ci::common::command_exists pytest; then
    _tests_tool_missing pytest
    return 0
  fi
  local has_python=0
  if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ] || \
     [ -f requirements.txt ] || [ -d tests ]; then
    has_python=1
  fi
  if [ "$has_python" -eq 0 ]; then
    ci::log::info "skipped: no Python project detected"
    return 0
  fi
  ci::log::info "Running pytest..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  if [ -n "$AFFECTED_TESTS" ]; then
    local python_tests=""
    python_tests="$(_tests_filter_affected python)"
    if [ -z "$python_tests" ]; then
      ci::log::info "skipped: no affected Python tests"
      return 0
    fi
    # Expanded to files first. `pytest` takes paths and node IDs, never shell
    # globs -- `pytest 'tests/**/test_*.py'` exits 4 with a usage error, having
    # collected nothing -- so every changeset that mapped to a Python pattern
    # failed this lane with a usage error instead of running the affected
    # suite. The narrowing has therefore never selected a Python test; it has
    # only ever broken the run that tried to use it.
    local _py_files="" _pat
    while IFS= read -r _pat; do
      [ -n "$_pat" ] || continue
      _py_files="${_py_files}$(_tests_expand_glob "$_pat")
"
    done <<< "$python_tests"
    _py_files="$(printf '%s' "$_py_files" | grep -v '^[[:space:]]*$' | sort -u || true)"

    if [ -z "$_py_files" ]; then
      # The patterns named no file that is actually here. Not narrowing is the
      # only answer that cannot skip a pushed change -- the same rule the range
      # reader at the top of this file follows where it cannot ask, and the
      # opposite of the "skipped: no affected Python tests" branch above, which
      # is reached when the changeset maps to no Python pattern at all rather
      # than when the patterns turn out to match nothing.
      ci::log::info "Affected Python patterns match no file here; running every Python test."
      pytest --junitxml="$JUNIT_DIR/python.xml" || rc=$?
    else
      local pytest_args=() _pyf
      while IFS= read -r _pyf; do
        [ -n "$_pyf" ] && pytest_args+=("$_pyf")
      done <<< "$_py_files"
      pytest --junitxml="$JUNIT_DIR/python.xml" "${pytest_args[@]}" || rc=$?
    fi
  else
    pytest --junitxml="$JUNIT_DIR/python.xml" || rc=$?
  fi
  [ "$rc" -ne 0 ] && _tests_record_failure "Python" "exit code ${rc}"
  return 0
}

tests::run_go() {
  if ! ci::common::command_exists go; then
    _tests_tool_missing go
    return 0
  fi
  if [ ! -f go.mod ]; then
    ci::log::info "skipped: no go.mod found"
    return 0
  fi
  ci::log::info "Running Go tests..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  if ci::common::command_exists gotestsum; then
    gotestsum --junitfile "$JUNIT_DIR/go.xml" ./... || rc=$?
  else
    go test ./... -v 2>&1 || rc=$?
  fi
  [ "$rc" -ne 0 ] && _tests_record_failure "Go" "exit code ${rc}"
  return 0
}

tests::run_rust() {
  if ! ci::common::command_exists cargo; then
    _tests_tool_missing cargo
    return 0
  fi
  if [ ! -f Cargo.toml ]; then
    ci::log::info "skipped: no Cargo.toml found"
    return 0
  fi
  ci::log::info "Running cargo test..."
  local rc=0
  cargo test 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    _tests_record_failure "Rust" "exit code ${rc}"
  else
    # Emit a minimal pass JUnit for consistency
    mkdir -p "$JUNIT_DIR"
    ci::junit::init "rust" "$JUNIT_DIR/rust.xml"
    ci::junit::add_test "cargo" "cargo test" "0" "pass"
    ci::junit::finish
  fi
  return 0
}

tests::run_shell() {
  if ! ci::common::command_exists bats; then
    _tests_tool_missing bats
    return 0
  fi
  if [ ! -d ci/tests ]; then
    ci::log::info "skipped: no ci/tests directory found"
    return 0
  fi
  ci::log::info "Running bats shell tests..."
  mkdir -p "$JUNIT_DIR"
  local rc=0
  bats --formatter junit ci/tests/ > "$JUNIT_DIR/shell.xml" 2> "$JUNIT_DIR/shell.stderr" || rc=$?
  [ "$rc" -ne 0 ] && _tests_record_failure "Shell" "exit code ${rc}"
  return 0
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

_tests_main() {
  ci::log::section "Check: tests"

  local check_id="${CI_GATE_CHECK_ID:-all}"

  case "$check_id" in
    tests-js)     tests::run_js ;;
    tests-python) tests::run_python ;;
    tests-go)     tests::run_go ;;
    tests-rust)   tests::run_rust ;;
    tests-shell)  tests::run_shell ;;
    all|*)
      tests::run_js
      tests::run_python
      tests::run_go
      tests::run_rust
      tests::run_shell
      ;;
  esac

  local result_name
  result_name="$(ci::common::result_name "$OVERALL_RESULT")"
  ci::log::info "Tests result: ${result_name}"
  exit "$OVERALL_RESULT"
}

if [[ "${BASH_SOURCE[0]}" = "${0}" ]]; then
  _tests_main "$@"
fi
