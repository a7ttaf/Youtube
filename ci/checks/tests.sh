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

cd "$ROOT_DIR"

JUNIT_DIR="$ROOT_DIR/ci/reports/junit"
OVERALL_RESULT=$CI_RESULT_PASS

# ---------------------------------------------------------------------------
# Affected test selection
# ---------------------------------------------------------------------------
AFFECTED_TESTS=""
if type ci::affected::get_affected_tests >/dev/null 2>&1 && [ -f "ci/config/affected.yml" ]; then
  _changed_files="$(git diff --cached --name-only 2>/dev/null || true)"
  if [ -z "$_changed_files" ]; then
    ci::log::info "No staged files found; falling back to unstaged changes for ad-hoc test selection."
    _changed_files="$(git diff --name-only 2>/dev/null || true)"
  fi
  if [ -n "$_changed_files" ]; then
    AFFECTED_TESTS="$(while IFS= read -r f; do
      [ -n "$f" ] && ci::affected::get_affected_tests "$f"
    done <<< "$_changed_files" | sort -u || true)"
  fi
fi

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
_tests_js_declared_runner() {
  local _cmd _w
  ci::common::command_exists node || return 1
  _cmd="$(node -e "try{const p=require('./package.json');process.stdout.write(String((p.scripts||{}).test||''))}catch(e){process.exit(3)}" 2>/dev/null)" || return 1
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
  local _expect=1 _tp=0 _ep=0 _pp=0
  for _w in $_cmd; do
    case "$_w" in
      ';'|'&&'|'||'|'|'|'&'|'('|')'|'{'|'}')
        _expect=1 ; _tp=0 ; _ep=0 ; _pp=0 ; continue ;;
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
        [A-Za-z_]*=*) continue ;;
        *) _ep=0 ;;
      esac
    elif [ "$_ep" -eq 2 ]; then _ep=1 ; continue
    fi
    if [ "$_pp" -eq 1 ]; then
      case "$_w" in
        --filter|-F|--dir|-C|--cwd|--prefix|--workspace|-w) _pp=2 ; continue ;;
        -*) continue ;;
        *) _pp=0 ;;
      esac
    elif [ "$_pp" -eq 2 ]; then _pp=1 ; continue
    fi
    case "${_w##*/}" in
      vitest|vitest.cmd|vitest.exe) printf 'vitest' ; return 0 ;;
      jest|jest.cmd|jest.exe) printf 'jest' ; return 0 ;;
      timeout) _tp=1 ; continue ;;
      env|cross-env) _ep=1 ; continue ;;
      npx|pnpm|yarn|npm|bun) _pp=1 ; continue ;;
      nohup|command|exec|time|dlx|run|--) continue ;;
    esac
    case "$_w" in
      -*) continue ;;
      [A-Za-z_]*=*) continue ;;
    esac
    # Some other command. What follows are its arguments, and a runner named
    # among them is not the one this script runs.
    _expect=0
  done
  return 0
}

_tests_js_have_runner() {
  local _c
  for _c in "node_modules/.bin/$1" "node_modules/.bin/$1.exe" "node_modules/.bin/$1.cmd"; do
    [ -x "$_c" ] && return 0
  done
  return 1
}

_tests_js_workspace() {
  local ws="$1" junit_out="$2" jest_pattern="$3"
  cd "$ws" || return 30

  local candidate declared="" have_vitest=0 have_jest=0
  _tests_js_have_runner vitest && have_vitest=1
  _tests_js_have_runner jest && have_jest=1
  declared="$(_tests_js_declared_runner || true)"

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
        "$candidate" run --reporter=junit --outputFile="$junit_out"
        return $?
      fi
    done
  fi

  if [ "$declared" != "vitest" ]; then
    for candidate in node_modules/.bin/jest node_modules/.bin/jest.exe node_modules/.bin/jest.cmd; do
      if [ -x "$candidate" ]; then
        if ci::common::command_exists node && node -e "require.resolve('jest-junit')" >/dev/null 2>&1; then
          JEST_JUNIT_OUTPUT_FILE="$junit_out" \
            "$candidate" --ci --reporters=default --reporters=jest-junit ${jest_pattern:+$jest_pattern}
          return $?
        fi
        "$candidate" --ci ${jest_pattern:+$jest_pattern}
        return $?
      fi
    done
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
      if [ -n "$jest_pattern" ]; then
        jest_pattern="${jest_pattern}|${pattern}"
      else
        jest_pattern="$pattern"
      fi
    done <<< "$js_tests"
    [ -n "$jest_pattern" ] && jest_pattern="--testPathPattern=${jest_pattern}"
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

    if [ "$rc" -eq 127 ]; then
      # A detected workspace with no runner is broken infrastructure, not a
      # skip. Reaching here means discovery found a manifest and this lane was
      # scheduled for it, so "no runner" does not mean "nothing to run" -- it
      # means the suite that exists could not be executed, and continuing let
      # tests-js exit 0 having run no JavaScript at all. An uninstalled
      # node_modules or a missing binary would take a blocking lane green.
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
    local pytest_args=()
    while IFS= read -r pattern; do
      [ -n "$pattern" ] && pytest_args+=("$pattern")
    done <<< "$python_tests"
    pytest --junitxml="$JUNIT_DIR/python.xml" "${pytest_args[@]}" || rc=$?
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
