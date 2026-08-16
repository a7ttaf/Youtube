#!/usr/bin/env bash
# ci/preflight.sh – Main orchestrator for the CI Gate.
# Bash 3.2+ compatible. Do not source; execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Determine repository root (portable: works when run from any sub-directory)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# ---------------------------------------------------------------------------
# Source libraries
# ---------------------------------------------------------------------------
source "$ROOT_DIR/ci/lib/common.sh"
source "$ROOT_DIR/ci/lib/git.sh"
source "$ROOT_DIR/ci/lib/report.sh"
source "$ROOT_DIR/ci/lib/runner.sh" 2>/dev/null || true
source "$ROOT_DIR/ci/lib/changeset.sh" 2>/dev/null || true
source "$ROOT_DIR/ci/lib/cache.sh" 2>/dev/null || true

# Load gate configuration
ci::common::load_gate_config "ci/config/gate.yml"
ci::common::load_thresholds_config "ci/config/thresholds.yml"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<'EOF'
Usage: ./ci/preflight.sh [options]

Options:
  --mode <quick|full|ship|debt>  Select run mode (default: quick)
  --fix                          Run auto-fix formatting step before checks
  --all                          Ignore incremental filtering and run all checks
  --profile                      Show timing profile in terminal output
  -h, --help                     Show this help text
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE="quick"
RUN_FIX=0
RUN_ALL=0
RUN_PROFILE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --mode)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --mode requires a value" >&2
        usage >&2
        exit 1
      fi
      MODE="$2"
      shift 2
      ;;
    --fix)
      RUN_FIX=1
      shift
      ;;
    --all)
      RUN_ALL=1
      shift
      ;;
    --profile)
      RUN_PROFILE=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Validate the mode here rather than at dispatch. The changeset check below can
# short-circuit with "No relevant changes detected" and exit 0 before run_mode
# is ever reached, so on a clean tree an unknown --mode was silently accepted.
case "$MODE" in
  quick|full|ship|debt) ;;
  *)
    echo "ERROR: unknown --mode '$MODE'" >&2
    usage >&2
    exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
START_EPOCH="$(date '+%s')"
START_ISO="$(ci::common::now_iso_utc)"

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
COMMIT_SHA="$(git rev-parse HEAD 2>/dev/null || echo 'unknown')"

CI_REPORT_DIR="${CI_REPORT_DIR:-ci/reports}"
mkdir -p "$CI_REPORT_DIR"

# Initialize cache if available
if type ci::cache::init >/dev/null 2>&1; then
  ci::cache::init
fi

# Initialize runner if available
_ensure_runner_init() {
  if type ci::runner::init >/dev/null 2>&1; then
    ci::runner::init
  fi
}

_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\b'/\\b}"
  s="${s//$'\f'/\\f}"
  printf '%s' "$s"
}

_xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  printf '%s' "$s"
}

_html_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  s="${s//\'/&#39;}"
  printf '%s' "$s"
}

_changeset_content_hash() {
  local entries="$1"
  local status path extra
  local files=()
  while IFS=$'\t' read -r status path extra; do
    [ -z "$path" ] && continue
    case "$status" in
      R*|C*) [ -n "$extra" ] && path="$extra" ;;
    esac
    # The record spells a path in the changeset's internal form, where a tab or
    # a newline in a filename is escaped. Hashing that spelling hashes a name
    # no file has: `[ -f ]` was false, the file dropped out of the key, and a
    # cacheable lane could restore an earlier PASS over contents nothing had
    # scanned. The key has to name the file on disk.
    ci::changeset::_decode_path "$path"
    path="$_CI_CHANGESET_PATH"
    # `if`, not `&&`. A final record whose path is not a regular file -- a
    # deletion, which is an ordinary entry -- leaves the test false, and that
    # becomes the status of the loop, of this function, and of the assignment
    # calling it. Under this script's `set -e` the gate aborts there, before a
    # check has run.
    if [ -f "$path" ]; then files+=("$path"); fi
  done <<< "$entries"
  ci::cache::_sha256 "${entries}:$(ci::cache::hash_files "${files[@]}")"
}

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
AGG_RESULT="$CI_RESULT_PASS"
COMMANDS_RUN=()
COMMANDS_PASSED=()
COMMANDS_FAILED=()
FAILED_COUNT=0
KNOWN_DEBT_SEEN=0
NEW_ISSUE_SEEN=0
INFRA_ISSUE_SEEN=0
SECURITY_STATUS="n/a"
BUILD_STATUS="n/a"

_TIMING_LABELS=()
_TIMING_STARTS=()
_TIMING_ENDS=()
_TIMING_RESULTS=()

# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------

_collect_check_result() {
  local label="$1"
  local rc="$2"
  local output="$3"
  local check_start="${4:-0}"
  local check_end="${5:-0}"
  local raw_rc=0

  if [ -n "$output" ]; then
    printf '%s
' "$output"
    ci::report::append_log "$output"
  fi

  case "$rc" in
    0) COMMANDS_PASSED+=("$label") ;;
    10) KNOWN_DEBT_SEEN=1; COMMANDS_PASSED+=("$label (known debt unchanged)") ;;
    20) NEW_ISSUE_SEEN=1; FAILED_COUNT=$((FAILED_COUNT + 1)); COMMANDS_FAILED+=("$label") ;;
    30) INFRA_ISSUE_SEEN=1; FAILED_COUNT=$((FAILED_COUNT + 1)); COMMANDS_FAILED+=("$label") ;;
    124)
      INFRA_ISSUE_SEEN=1; FAILED_COUNT=$((FAILED_COUNT + 1))
      COMMANDS_FAILED+=("$label (timeout)"); rc="$CI_RESULT_FAIL_INFRA"
      ;;
    *)
      INFRA_ISSUE_SEEN=1; FAILED_COUNT=$((FAILED_COUNT + 1))
      raw_rc="$rc"; rc="$CI_RESULT_FAIL_INFRA"
      COMMANDS_FAILED+=("$label:$raw_rc")
      ;;
  esac

  _TIMING_LABELS+=("$label")
  _TIMING_STARTS+=("$check_start")
  _TIMING_ENDS+=("$check_end")
  _TIMING_RESULTS+=("$rc")

  if [ "$label" = "security" ]; then
    [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] && SECURITY_STATUS="pass" || SECURITY_STATUS="fail"
  fi
  if [ "$label" = "build" ]; then
    [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] && BUILD_STATUS="pass" || BUILD_STATUS="fail"
  fi

  local result_name
  result_name="$(ci::common::result_name "$rc")"
  echo "Result [$label]: $result_name"
  ci::report::append_log "Result [$label]: $result_name"

  AGG_RESULT="$(ci::common::merge_results "$AGG_RESULT" "$rc")"
}

# run_check "label" "script"
run_check() {
  local label="$1"
  local script="$2"
  local check_start check_end
  check_start="$(date '+%s')"

  if _check_should_skip "$label"; then
    echo "Skipping [$label] (filtered)"
    return 0
  fi
  COMMANDS_RUN+=("$label")
  ci::report::append_log ""
  ci::report::append_log ">>> ${label} (${script})"

  local output="" rc=0
  set +e
  output=$("$script" 2>&1)
  rc=$?
  set -e

  check_end="$(date '+%s')"
  _collect_check_result "$label" "$rc" "$output" "$check_start" "$check_end"
}

# _check_disabled_in_config <id> – 0 when checks.yml disables that id.
#
# One awk pass, where this used to be a `while read` loop that piped each line
# into six separate greps and seds. The parse is unchanged, line for line; only
# the number of processes is different, and it was the dominant cost of running
# the gate at all. Measured on this repository's 213-line checks.yml: a single
# call took 34 seconds, and _check_should_skip makes one per lane plus one per
# related check, so a `quick` run spent minutes deciding what to run before it
# ran anything -- against a mode that declares a pre-commit budget.
# Which copy of ci/config/checks.yml the toggles are read from, resolved once.
#
# In ship mode: HEAD's, for the reason the lane plan is read from HEAD one
# function down. These toggles decide which lanes run at all, and reading them
# from the worktree let an *unstaged* edit switch lanes off for a push that does
# not carry the edit. Setting `tests-shell`, `tests-js` and `typecheck-js` to
# false is enough on its own -- `lint-js` and `format-js` are already disabled,
# so `_all_related_checks_disabled` then drops the whole `node` lane too, and
# `tests-shell` is the lane that would have reported the config file as drift.
# A pushed frontend commit with failing tests passes on toggles that exist in
# nobody's tree but the pusher's.
#
# Resolved once and cached because `_check_should_skip` asks per lane *and* per
# related check: materialising HEAD's copy per call would be one `git show` per
# question, against a mode with a budget.
#
# An unreadable HEAD copy falls back to `/dev/null`, not to the worktree. An
# empty configuration disables nothing, so every lane runs -- the failure
# direction is more work, not less, which is the same direction the lane plan
# takes when its own HEAD read fails. `full` is deliberately a whole-tree run
# against the worktree and keeps reading it.
_CHECKS_CONFIG_FILE=""
_CHECKS_CONFIG_TMP=""
# Removed at exit rather than after the first read: the answer is cached for the
# whole run, so the file has to outlive every caller. Guarded rather than
# unconditional, so the trap is inert until there is something to remove, and
# written inline rather than as a function so it is visibly the command that
# runs -- a named helper here is a definition with no call site any linter can
# see. `[ -z ]` short-circuits to status 0 when there is nothing to do, which
# keeps the trap from reporting a status of its own.
trap '[ -z "${_CHECKS_CONFIG_TMP:-}" ] || rm -f "$_CHECKS_CONFIG_TMP"' EXIT

# Sets `_CHECKS_CONFIG_FILE`; prints nothing.
#
# Assignment rather than output, and it is not a style choice: a caller reading
# this through `$(...)` would run it in a subshell, where both the cached answer
# and the temp file's lifetime die with the substitution -- the `git show` would
# repeat on every question, which is the cost this cache exists to avoid.
_checks_config_resolve() {
  # `${...:-}` rather than a bare expansion: ci/tests/test_preflight.bats lifts
  # these functions out one at a time, so the cache variable's top-level
  # initialiser is not necessarily in scope where they run. A helper that only
  # works when the whole file is present is a helper the suite cannot drive.
  [ -z "${_CHECKS_CONFIG_FILE:-}" ] || return 0
  _CHECKS_CONFIG_FILE="ci/config/checks.yml"
  if [ "${MODE:-}" = "ship" ]; then
    local _cc_tmp=""
    if command -v git >/dev/null 2>&1 \
      && git rev-parse --verify HEAD >/dev/null 2>&1 \
      && _cc_tmp="$(ci::common::mktemp_file checks-config 2>/dev/null)" \
      && git show "HEAD:ci/config/checks.yml" > "$_cc_tmp" 2>/dev/null \
      && [ -s "$_cc_tmp" ]; then
      _CHECKS_CONFIG_FILE="$_cc_tmp"
      _CHECKS_CONFIG_TMP="$_cc_tmp"
    else
      [ -n "$_cc_tmp" ] && rm -f "$_cc_tmp"
      _CHECKS_CONFIG_FILE="/dev/null"
      echo "ci/config/checks.yml is not readable in HEAD; running every lane." >&2
    fi
  fi
}

_check_disabled_in_config() {
  local wanted="$1" _cc_file
  # A plain call, not `$(...)`: see _checks_config_resolve.
  _checks_config_resolve
  _cc_file="$_CHECKS_CONFIG_FILE"

  [ -f "$_cc_file" ] || return 1

  awk -v want="$wanted" '
    BEGIN { in_checks = 0; in_list = 0; id = ""; enabled = "true" }
    {
      line = $0
      if (line == "" || substr(line, 1, 1) == "#") next

      if (line ~ /^checks:[[:space:]]*$/) { in_checks = 1; next }

      if (in_checks == 1 && line ~ /^[^[:space:]]/) { in_checks = 0; id = "" }

      if (in_checks == 1 && line ~ /^  [A-Za-z0-9_-]+:[[:space:]]*(#.*)?$/) {
        id = line
        sub(/^  /, "", id)
        sub(/:.*$/, "", id)
        enabled = "true"
        in_list = 0
        next
      }

      if (line ~ /^[[:space:]]*-[[:space:]]*id:[[:space:]]*/) {
        in_list = 1
        id = line
        sub(/^[[:space:]]*-[[:space:]]*id:[[:space:]]*/, "", id)
        gsub(/[ "]/, "", id)
        enabled = "true"
      }

      if ((in_checks == 1 || in_list == 1) \
        && line ~ /^[[:space:]]*enabled:[[:space:]]*/) {
        enabled = line
        sub(/^[[:space:]]*enabled:[[:space:]]*/, "", enabled)
        # Whitespace first, then the comment -- the order the sed pair used, so
        # "true  # note" reduces the same way it always did.
        gsub(/[[:space:]]/, "", enabled)
        sub(/#.*$/, "", enabled)
      }

      # Flagged rather than `exit 0`: an exit inside a rule still runs END, and
      # an END that exits with its own status overrides this one.
      if (id == want && enabled == "false") { disabled = 1; exit }
    }
    END { exit(disabled ? 0 : 1) }
  ' "$_cc_file"
}

_checks_for_lane_label() {
  local label="$1"
  case "$label" in
    node) printf 'lint-js typecheck-js format-js tests-js' ;;
    python) printf 'lint-python typecheck-python format-python tests-python' ;;
    security) printf 'sast secrets supply-chain' ;;
    build) printf 'lint-shell format-shell lint-docker lint-yaml lint-markdown lint-actions' ;;
    branch-protection) printf 'branch-protection' ;;
    *) printf '' ;;
  esac
}

_all_related_checks_disabled() {
  local label="$1"
  local related check_id any=0
  related="$(_checks_for_lane_label "$label")"
  [ -z "$related" ] && return 1

  for check_id in $related; do
    any=1
    if ! _check_disabled_in_config "$check_id"; then
      return 1
    fi
  done

  [ "$any" = "1" ]
}

_check_should_skip() {
  local label="$1"

  # 1. Respect checks.yml enabled: false for the mapping-based schema.
  if _check_disabled_in_config "$label" || _all_related_checks_disabled "$label"; then
    return 0
  fi

  # 2. Skip by changeset if incremental and no relevant files changed
  if [ "${CI_GATE_INCREMENTAL:-1}" = "1" ] && [ -n "${_CI_CHANGESET_CHECKS:-}" ]; then
    local found=0
    local ck
    for ck in $_CI_CHANGESET_CHECKS; do
      _check_disabled_in_config "$ck" && continue
      # Keep this reverse mapping aligned with ci::changeset::_checks_for_language.
      # The gate submits coarse lane labels, while changeset emits fine-grained check ids.
      case "${label}:${ck}" in
        "$label:$label" | \
        node:lint-js | node:typecheck-js | node:format-js | node:tests-js | \
        python:lint-python | python:typecheck-python | python:format-python | python:tests-python | \
        build:lint-shell | build:format-shell | build:lint-docker | build:lint-yaml | build:lint-markdown | build:lint-actions)
          found=1
          break
          ;;
      esac
    done
    # The bats suites assert on the gate's own inputs, and those inputs are not
    # all classifiable by language: test_js_lane.bats validates
    # ci/config/affected.yml and checks.yml (yaml -> lint-yaml only) and the
    # .gitignore negations that keep frontend tests trackable (unknown -> no
    # checks at all). Either would filter this lane and let a regression past
    # the very test written to catch it. Matched by path rather than language so
    # unrelated yaml elsewhere does not pull in a four-minute suite.
    #
    # frontend/README.md is here for the same reason: test_test_layout.bats
    # asserts on its prose about which modes run the guard, and a README-only
    # change classifies as markdown, which schedules lint-markdown and nothing
    # else -- so the gate could publish a false coverage claim without running
    # the case written to reject it.
    #
    # Keep this list in step with what ci/tests/ actually asserts on.
    #
    # The whole-path entries are anchored on a field boundary, not on end of
    # line. _CI_CHANGESET_FILES_RAW holds `STATUS<TAB>PATH` records, and a
    # rename is `R100<TAB>old<TAB>new` — so `\.gitignore$` matched only the
    # destination, and renaming .gitignore away filtered out the suite that
    # guards it. The `ci/` and `.githooks/` entries are prefixes and were never
    # affected.
    #
    # `Makefile` joined that list because the suites assert on it. The
    # classifier derives a language from each path and `Makefile` yields `make`,
    # which no lane maps -- so a commit touching only the Makefile scheduled
    # nothing, and `ci/tests/test_preflight.bats` asserts that the `bats-install`
    # target exists and is what the lane's own refusal message names. Deleting or
    # breaking that target could therefore pass the gate and leave the documented
    # remedy for a missing Bats unusable, which is the one failure a provisioning
    # target has. The rule here is the same one the other four entries follow: a
    # path a suite reads is a path that schedules the suite.
    if [ "$found" = "0" ] && [ "$label" = "tests-shell" ] \
      && printf '%s\n' "${_CI_CHANGESET_FILES_RAW:-}" \
        | grep -qE '(^|[[:space:]])(ci/|\.githooks/|\.gitignore([[:space:]]|$)|Makefile([[:space:]]|$)|frontend/README\.md([[:space:]]|$))'; then
      found=1
    fi

    if [ "$found" = "0" ]; then
      # Always-run checks are never skipped by changeset.
      # test-layout belongs here rather than in the reverse mapping above: the
      # changeset emits language-derived check ids and never emits test-layout,
      # so any mapping entry would still leave it filtered whenever the diff
      # does not look like JavaScript — which is exactly when a misplaced test
      # goes unnoticed. The check is a pruned walk of one directory tree and
      # costs milliseconds.
      # branch-protection belongs here for the same reason as test-layout, and
      # its absence was total rather than conditional: nothing anywhere emits
      # `branch-protection` as a changeset check id -- not the always-checks
      # seed ("secrets commit-hygiene"), not the language mapping -- so the
      # only arm that could set found=0 to 1 for this label was unreachable and
      # the filter dropped the lane on every run, in every mode, including a
      # direct commit on main. Reproduced: the check alone exits 20 on a
      # protected branch, while the same tree through the gate prints
      # "Skipping [branch-protection] (filtered)" and exits 0. Which branch you
      # are on is not a function of the changed-file list, so no changeset can
      # ever be the thing that decides whether this runs.
      case "$label" in
        git-safety|changed-files|security|debt|test-layout|branch-protection) ;;
        *) return 0 ;;
      esac
    fi
  fi

  return 1
}

# Checks whose result is not a function of the changed-file list, so a cache
# entry keyed on that list can be served when the answer has actually changed.
#
# test-layout reads the git index and walks the whole frontend tree.
# _changeset_content_hash hashes worktree copies of the changed paths, so
# staging a narrowed vitest.config.ts while keeping the previously passing
# worktree copy leaves both the path list and the content hash identical — and
# the guard would report a cached PASS for a commit it never inspected. It costs
# under a second; caching it buys nothing worth that hole.
_check_is_cacheable() {
  case "$1" in
    # test-layout reads the git index and walks the whole frontend tree, neither
    # of which the changed-file key describes.
    test-layout) return 1 ;;
    # tests-shell asserts on the entire ci/ tree and on files the changeset need
    # not mention at all -- .gitignore rules, frontend/README.md, every gate
    # script the suites exercise. A PASS cached for a .gitignore-only branch and
    # then rebased onto a base carrying a regression in ci/checks/node.sh has an
    # identical key: same tools, same changed files, same checks.yml. Listing
    # the suite's inputs was the alternative, but "did we list them all?" is the
    # question that produced this bug, and the suite runs only in full and ship.
    tests-shell) return 1 ;;
    # node runs the workspace's complete test, typecheck and build scripts, so
    # its result depends on every file in that workspace -- not just the ones
    # this branch happens to touch. A PASS cached for one frontend change and
    # then rebased onto a base carrying a regression in another frontend file
    # has an identical key, and the lane is restored without executing
    # anything. Same argument as tests-shell, one lane over.
    node) return 1 ;;
    # typecheck-js compiles every project ci/checks/typecheck.sh discovers, not
    # the ones this branch touched, so the changed-file key does not describe
    # what it read -- the same argument as `node` above, and it arrived with the
    # lane. A PASS cached for a branch touching frontend/src/a.ts is replayed
    # after a rebase onto a base where tsconfig.node.json now has a type error,
    # and the node lane does not cover that gap: the `-p <project>` rule in
    # ci/checks/node.sh accepts a typecheck script naming one project *because*
    # this lane compiles the rest. Cached, it compiles nothing.
    #
    # Reported by review, and separately met head-on: the case asserting this
    # lane is scheduled began failing under ci/preflight.sh --mode ship, because
    # a hit restored the lane and the stub never printed its marker.
    #
    #   RAN:node
    #     [cache hit] typecheck-js
    #   Result [typecheck-js]: PASS
    #
    # That was a test-isolation bug and is fixed in ci/tests/gate_env.bash; this
    # is the product half of the same observation, and only this half stops a
    # green result standing for a compile that did not happen.
    typecheck-js) return 1 ;;
    # python is the same lane one language over, and it was left cacheable.
    # It runs `ruff check`, `ruff format --check`, a bare `pytest` and
    # `compileall` across the whole of PYTHON_PACKAGE_DIR, none of which is
    # scoped to the changed files. Reproduced: with one .py staged so the lane
    # is scheduled and a syntax error left *unstaged* in a second file — absent
    # from the changeset, so invisible to the key — a cached run returned
    # "[cache hit] python PASS" while the identical tree with CI_GATE_NO_CACHE=1
    # returned FAIL_INFRA.
    python) return 1 ;;
    # git-safety and branch-protection read the outgoing commit *history*, and
    # the cache key describes the endpoint changeset: the paths that differ and
    # the worktree contents. Two branches with identical final files have
    # identical keys, so a PASS cached for one is served for the other — and an
    # amend or rebase can add and remove a secret in an intermediate commit, or
    # replace a signed commit with an unsigned one, without changing a single
    # final byte. That is precisely the history these two now walk per commit,
    # and precisely what the key cannot see. Listing the commit identities in
    # the key would work; not caching a check whose input is the history is
    # simpler and cannot drift out of step with what the check reads.
    git-safety | branch-protection) return 1 ;;
    # security runs `pnpm audit` / `yarn npm audit` / `npm audit` against a live
    # advisory database. Its answer changes when a CVE is published and no file
    # in this repository changes at all, so there is nothing for the key to
    # move: same paths, same contents, same tool versions, same checks.yml, and
    # a PASS from before the advisory landed is served after it.
    security) return 1 ;;
    # build runs `bash -n` and, where it is installed, shellcheck over
    # `ci/*.sh ci/checks/*.sh ci/lib/*.sh ci/scripts/*.sh .githooks/*` plus
    # install.sh and deploy.sh -- the whole set, not the changed part of it.
    # This is the tests-shell argument one lane over: a PASS cached for a
    # docs-only branch and then rebased onto a base carrying a syntax error in
    # ci/checks/node.sh has an identical key, because that file is not in the
    # changeset the key describes.
    build) return 1 ;;
    # debt executes the underlying tools across the repository and counts
    # signature occurrences in their output against ci/debt/known-failures.yml.
    # What it reads is whatever those tools read, which is everything; the
    # ratchet it compares against is a file the changeset need never mention.
    debt) return 1 ;;
    # Named positively, and it is the only one: this lane writes the three file
    # lists under ci/reports/ and returns PASS, and nothing else in the gate
    # reads them. Its output is a function of exactly the thing the key is made
    # of, so a hit and a run produce the same bytes.
    changed-files) return 0 ;;
  esac
  # Closed by default, and that is the fix behind the three arms above.
  #
  # This was `return 0`: a list of lanes that may not be cached, with everything
  # else cacheable for not being on it. An exclusion has no tail -- it has to
  # name every lane that must never reach the default, including the ones added
  # after it was written -- and security, build and debt were three that never
  # were. The same shape, in the same file, that made `--mode debt` run the full
  # plan for not being `quick`.
  #
  # Turned round, a lane nobody has classified is simply run. That costs time
  # and cannot report a PASS for work that did not happen, which is the
  # direction to be wrong in. `preflight: every lane it schedules is classified
  # for the cache` enumerates the plan and fails on a lane this function has
  # never heard of, so the classification is made deliberately rather than by
  # falling off the end of a case.
  return 1
}

# _tool_fingerprint <tool> – echo "<tool>-<version>", or "<tool>-absent".
#
# The version probe goes through ci::cache::tool_version, which disables errexit
# around the call. That matters more than it looks: deriving a cache key must
# never be able to end the run, and under `set -Eeuo pipefail` a bare
# `$(tool --version | head -1)` aborts preflight the moment a present-but-broken
# executable exits non-zero -- before a single check has run, with a message
# about nothing the commit touched.
#
# The absent branch stays separate because uninstalling a tool has to change the
# key too: "absent" and "installed but unreadable" are different states, and
# collapsing them would let a lane replay a PASS across the removal.
_tool_fingerprint() {
  if command -v "$1" >/dev/null 2>&1; then
    printf '%s-%s' "$1" "$(ci::cache::tool_version "$1")"
  else
    printf '%s-absent' "$1"
  fi
}

# _cached_reports_for <label> – the report files this lane owns, one per line.
#
# A cached lane does not run, so anything it writes besides its result and its
# output is simply not written. `changed-files` produces three file lists under
# ci/reports/ and a hit restored neither of them: they were left as whatever an
# earlier run had put there, or absent on a fresh checkout, while the lane
# reported PASS. An audit trail describing a different tree is worse than none,
# and "the lane passed" standing beside it is the false confidence the rest of
# this gate is built to refuse.
#
# Empty for every other lane, which is the honest answer today: those lanes are
# not cached, and one that becomes cacheable has to declare what it owns here.
_cached_reports_for() {
  case "$1" in
    changed-files)
      printf '%s\n' changed-files.txt staged-files.txt untracked-files.txt
      ;;
  esac
}

_compute_cache_key() {
  local label="$1"
  local tool_ver="unknown"
  # Every probe goes through _tool_fingerprint. Lanes whose result depends on a
  # toolchain the key cannot describe -- node, tests-shell -- are excluded from
  # the cache outright by _check_is_cacheable rather than given a longer key, so
  # no branch here needs to enumerate their tools. What remains is the property
  # that matters for the lanes that ARE cached: a probe can never end the run.
  case "$label" in
    python) tool_ver="$(_tool_fingerprint python3)" ;;
    *) tool_ver="$(_tool_fingerprint bash)" ;;
  esac

  local files_hash=""
  if [ -n "${_CI_CHANGESET_FILES_RAW:-}" ]; then
    files_hash="$(_changeset_content_hash "$_CI_CHANGESET_FILES_RAW")"
  else
    files_hash="$(git rev-parse HEAD 2>/dev/null || echo 'none')"
  fi

  # The one cached lane is keyed on what it reports, not on the changeset.
  #
  # `changed-files` writes three lists -- changed, staged and untracked -- and
  # the changeset the key above is made of is at most one of them: in quick mode
  # it is the staged paths whenever there are any. Two trees with identical
  # staging and different unstaged or untracked files therefore share a key, and
  # a hit serves counts and lists describing the other one. Hashing the three
  # lists themselves makes a hit possible exactly when the answer is the same
  # answer, which is also what lets the entry carry those lists back (see
  # _cached_reports_for).
  case "$label" in
    changed-files)
      files_hash="$(ci::cache::_sha256 "$(
        ci::git::changed_files 2>/dev/null || true
        printf '\036'
        ci::git::staged_files 2>/dev/null || true
        printf '\036'
        ci::git::untracked_files 2>/dev/null || true
      )")"
      ;;
  esac

  local config_hash=""
  if [ -f "ci/config/checks.yml" ]; then
    config_hash="$(ci::cache::_sha256_file ci/config/checks.yml)"
  else
    config_hash="none"
  fi

  ci::cache::key "$label" "$tool_ver" "$files_hash" "$config_hash"
}

# run_phase "label1:script1" "label2:script2" ...
# Runs checks in parallel (using runner if available), waits, collects results.
run_phase() {
  local entry label script

  # Fall back to sequential if runner not available
  if ! type ci::runner::init >/dev/null 2>&1; then
    for entry in "$@"; do
      label="${entry%%:*}"
      script="${entry#*:}"
      run_check "$label" "$script"
    done
    return 0
  fi

  _ensure_runner_init

  local submitted_labels=""

  for entry in "$@"; do
    label="${entry%%:*}"
    script="${entry#*:}"

    if _check_should_skip "$label"; then
      echo "Skipping [$label] (filtered)"
      continue
    fi

    COMMANDS_RUN+=("$label")
    ci::report::append_log ""
    ci::report::append_log ">>> ${label} (${script}) [parallel]"

    # Try cache lookup before submitting
    local cache_digest="" cache_hit=0
    if [ "${CI_GATE_CACHE_ENABLED:-1}" = "1" ] && _check_is_cacheable "$label" && type ci::cache::key >/dev/null 2>&1; then
      # Composite content-hash digest identifying this check's cache entry
      # (lane + tool version + files hash + config hash).
      cache_digest="$(_compute_cache_key "$label")"
      local cache_dest="${CI_REPORT_DIR}/.cache/${label}"
      if ci::cache::get "$cache_digest" "$cache_dest"; then
        # The reports this lane owns come back with it, and an entry that cannot
        # produce one of them is not a hit. Written before the result is, so a
        # half-restored ci/reports/ never sits beside a PASS -- and treated as a
        # miss rather than patched up, because an entry stored by an older
        # revision of this file has no way to know what it was supposed to
        # carry. Running the lane is the cheap, correct answer.
        local _cr_name _cr_ok=1
        while IFS= read -r _cr_name; do
          [ -n "$_cr_name" ] || continue
          if [ -f "${cache_dest}/${_cr_name}" ]; then
            cp -f "${cache_dest}/${_cr_name}" "${CI_REPORT_DIR}/${_cr_name}" || _cr_ok=0
          else
            _cr_ok=0
          fi
        done <<< "$(_cached_reports_for "$label")"
        if [ "$_cr_ok" -ne 1 ]; then
          echo "  [cache entry incomplete] $label — running it"
          rm -rf "$cache_dest"
        else
          echo "  [cache hit] $label"
          cache_hit=1
          if [ -f "$cache_dest/result.txt" ]; then
            local cached_rc
            cached_rc="$(cat "$cache_dest/result.txt")"
            local cached_output=""
            [ -f "$cache_dest/output.txt" ] && cached_output="$(cat "$cache_dest/output.txt")"
            # Write cached output to runner log file so get_output works
            local log_file="${CI_REPORT_DIR}/${label}.log"
            printf '%s' "$cached_output" > "$log_file"
            if [ -z "${_CI_RUNNER_JOBS_DIR:-}" ]; then
              _ensure_runner_init
            fi
            if [ -n "${_CI_RUNNER_JOBS_DIR:-}" ]; then
              mkdir -p "$_CI_RUNNER_JOBS_DIR"
              printf '%d' "$$" > "${_CI_RUNNER_JOBS_DIR}/${label}.pid"
              printf '%d' "$cached_rc" > "${_CI_RUNNER_JOBS_DIR}/${label}.rc"
              printf '%s' "$(date '+%s')" > "${_CI_RUNNER_JOBS_DIR}/${label}.start"
              printf '%s' "$(date '+%s')" > "${_CI_RUNNER_JOBS_DIR}/${label}.end"
            fi
          fi
        fi
      fi
    fi

    if [ "$cache_hit" = "0" ]; then
      ci::runner::submit "$label" "$script"
    fi
    if [ -n "$submitted_labels" ]; then
      submitted_labels="${submitted_labels} ${label}"
    else
      submitted_labels="$label"
    fi
  done

  [ -z "$submitted_labels" ] && return 0

  ci::runner::wait_all

  # Collect results from each submitted job
  local lbl rc output check_start check_end
  for lbl in $submitted_labels; do
    rc="$(ci::runner::get_result "$lbl" 2>/dev/null || echo "30")"
    output="$(ci::runner::get_output "$lbl" 2>/dev/null || true)"

    # Read timing from runner job files
    check_start=0
    check_end=0
    if [ -n "${_CI_RUNNER_JOBS_DIR:-}" ]; then
      local sf="${_CI_RUNNER_JOBS_DIR}/${lbl}.start"
      local ef="${_CI_RUNNER_JOBS_DIR}/${lbl}.end"
      [ -f "$sf" ] && check_start="$(cat "$sf" 2>/dev/null || echo "0")"
      [ -f "$ef" ] && check_end="$(cat "$ef" 2>/dev/null || echo "0")"
    fi

    _collect_check_result "$lbl" "$rc" "$output" "$check_start" "$check_end"

    # Store result in cache
    if [ "${CI_GATE_CACHE_ENABLED:-1}" = "1" ] && _check_is_cacheable "$lbl" && type ci::cache::key >/dev/null 2>&1; then
      local cache_dest="${CI_REPORT_DIR}/.cache/${lbl}"
      mkdir -p "$cache_dest"
      printf '%d' "$rc" > "$cache_dest/result.txt"
      printf '%s' "$output" > "$cache_dest/output.txt"
      # And whatever this lane owns besides its result, or the entry cannot
      # stand in for the run. A report the lane did not write is not stored:
      # the restore side treats a missing one as a miss, which is the same
      # answer and reached without guessing here what a failed run should have
      # produced.
      local _cs_name
      while IFS= read -r _cs_name; do
        [ -n "$_cs_name" ] || continue
        [ -f "${CI_REPORT_DIR}/${_cs_name}" ] || continue
        cp -f "${CI_REPORT_DIR}/${_cs_name}" "${cache_dest}/${_cs_name}" || true
      done <<< "$(_cached_reports_for "$lbl")"
      local put_digest
      # Same composite content-hash digest as the cache-get call site above.
      put_digest="$(_compute_cache_key "$lbl")"
      ci::cache::put "$put_digest" "$cache_dest"
    fi
  done
}

# ---- Mode check groups ----
run_common_checks() {
  run_phase     "git-safety:./ci/checks/git-safety.sh"     "changed-files:./ci/checks/changed-files.sh"     "test-layout:./ci/checks/test-layout.sh"
  run_phase     "node:./ci/checks/node.sh"     "python:./ci/checks/python.sh"
  # typecheck-js belongs to every mode that runs the node lane, not to full and
  # ship alone.
  #
  # ci/checks/node.sh accepts a typecheck script that names one of several
  # discovered projects -- `tsc -p tsconfig.app.json --noEmit` in a workspace
  # that also has tsconfig.node.json -- and the entire justification for
  # accepting it is that this lane compiles the rest. The comment carrying that
  # justification says so, and says the two are one decision.
  #
  # They had come apart by mode. This lane was scheduled only from
  # run_full_or_ship_checks, so in quick the acceptance stood while the lane
  # that makes it true never ran:
  #
  #   --mode quick  ->  git-safety node test-layout
  #   --mode ship   ->  ... node ... typecheck-js
  #
  # and a type error in tsconfig.node.json passed the pre-commit gate, surfacing
  # only at the next full or ship run. That is the same "reasoning about a lane
  # instead of checking that it runs" the comment above the -p rule was written
  # to record, one mode over.
  #
  # Cost is bounded by the changeset the way every other language lane is: this
  # id maps from `node`, so a commit touching no JavaScript filters it out
  # before it executes.
  run_phase     "typecheck-js:./ci/checks/typecheck-js.sh"
}

# A push that carries no content has nothing for a content lane to report on.
#
# `git push --delete origin feature` is every record being a deletion, so
# hook-dispatch sets CI_GATE_PUSH_DELETIONS_ONLY and git-safety self-skips on
# it -- and the ship plan still ran test-layout, security, the node lane, the
# build and the shell suites against whatever happens to be checked out. So
# deleting an unprotected branch could be refused for a layout error or a
# failing suite in a worktree the push is not sending anywhere, and it cost the
# half hour the shell suites take. A gate that blocks correct work gets switched
# off, and then it guards nothing.
#
# Destination protection is the one thing that must still run: `git push origin
# :main` deletes a protected branch outright, and that is precisely the push
# that has to be refused. branch-protection.sh reads the same flag and has its
# own arms for it.
run_ship_deletion_only_checks() {
  echo "Push carries only ref deletions; content lanes have nothing to report on."
  echo "  Destination protection still runs."
  run_phase "branch-protection:./ci/checks/branch-protection.sh"
}

# The other push that carries no content: one that publishes only a name.
#
# `git tag v1.2 <commit the destination already has>; git push origin v1.2`
# sends no tree. ci::git::worktree_covers_push accepts it on exactly that
# ground -- the tag adds a label and no content -- and then this dispatch ran
# the complete ship plan anyway, because deletions were the only content-free
# case it knew. So test-layout, the node lane, python, the build and the shell
# suites all ran against whatever happened to be checked out, and any failure
# already sitting there blocked an ordinary release. Same reasoning as the
# deletions path above, and the same exception: destination protection still
# runs, because where a push is going is a question a label-only push still
# answers.
run_ship_label_only_checks() {
  echo "Push publishes only refs the destination already carries; no tree is going out."
  echo "  Destination protection still runs."
  run_phase "branch-protection:./ci/checks/branch-protection.sh"
}

# ci/checks/typecheck.sh is scheduled here, and was not scheduled anywhere.
#
# It is registered in ci/checks/manifest.yml with `severity: blocker`, and no
# mode ran it: `typecheck-js` survives only as a changeset id that
# _lane_for_check maps back to the combined `node` lane above, so the file has
# never executed in quick, full or ship. Registered at one layer and run at
# none, which is the failure this whole branch is about.
#
# Through ci/checks/typecheck-js.sh rather than typecheck.sh directly: run_phase
# splits a phase entry on the first `:` and executes the command path, so an
# entry cannot carry CI_GATE_CHECK_ID, and typecheck.sh's default is `all` --
# which would run the Python, Go and Rust typecheckers under a `-js` label,
# duplicating the `python` lane beside it. ci/checks/tests-shell.sh is the same
# wrapper for the same reason.
#
# It is not a duplicate of what `node` does. The node lane runs the workspace's
# own `typecheck` script, whatever that script happens to compile; this lane
# compiles the root project and then every other discovered project by name. A
# workspace whose script is `tsc -p tsconfig.app.json --noEmit` has
# tsconfig.node.json checked by this lane and by nothing else -- and the
# argument rule in ci/checks/node.sh accepts that script *because* this lane
# covers the rest. That justification was written before this line existed and
# was false until it did.
run_full_or_ship_checks() {
  run_phase     "git-safety:./ci/checks/git-safety.sh"     "changed-files:./ci/checks/changed-files.sh"     "branch-protection:./ci/checks/branch-protection.sh"     "test-layout:./ci/checks/test-layout.sh"
  run_phase     "security:./ci/checks/security.sh"
  run_phase     "node:./ci/checks/node.sh"     "python:./ci/checks/python.sh"     "build:./ci/checks/build.sh"
  run_phase     "typecheck-js:./ci/checks/typecheck-js.sh"
  run_phase     "tests-shell:./ci/checks/tests-shell.sh"
  run_phase     "debt:./ci/checks/debt.sh"
}

run_mode() {
  # Whether a push carries a tree is a fact about the push, and it is settled
  # before any scheduler is chosen.
  #
  # These two cases used to live inside the `full|ship` arm below, past a
  # `return 0`: with CI_GATE_USE_LANES=1 and ci/config/lanes.conf present -- a
  # supported configuration -- a deletion-only push ran the whole lane list over
  # whatever happened to be checked out, and never reached the content-free
  # path. Worse in that mode than in this one, because lanes.conf carries no
  # branch-protection entry:
  #
  #   grep -c 'branch-protection' ci/config/lanes.conf   -> 0
  #
  # so the one check that both content-free paths deliberately keep -- where the
  # push is going -- was the single check that could not run. The narrow path
  # and the wide path were each missing the other's protection.
  #
  # Hoisted rather than duplicated into the lanes branch: a rule stated twice is
  # a rule that will be true in one place, which is the shape of nearly every
  # finding on this branch.
  if [ "$MODE" = "ship" ] && [ "${CI_GATE_PUSH_DELETIONS_ONLY:-0}" = "1" ]; then
    run_ship_deletion_only_checks
    return 0
  fi
  if [ "$MODE" = "ship" ] && ci::git::push_is_label_only; then
    run_ship_label_only_checks
    return 0
  fi

  # If lanes.conf exists and CI_GATE_USE_LANES=1, read it. Otherwise use hardcoded defaults.
  #
  # Full and ship only. ci/config/lanes.conf carries no mode column, so this
  # branch ran every lane in it whatever the mode -- and the file is a
  # description of the full plan: security, build, debt and tests-shell are all
  # in it and none of them belongs to a pre-commit run. `--mode quick` under
  # CI_GATE_USE_LANES=1 therefore ran the whole bats suite, about an hour here
  # against a 30s pre-commit budget, and could fail a commit on gate self-tests
  # unrelated to anything staged.
  #
  # Named positively, and the first version of this was not. It read
  # `[ "$MODE" != "quick" ]`, which fixed the mode in the report and let every
  # other one through: `--mode debt` is not quick, so a debt-ratchet run took
  # this branch and executed node, security, build and the shell suites before
  # returning, instead of the two lanes the `debt)` arm below schedules. An
  # exclusion list has to name every mode that must not reach here, including
  # the ones added later; naming the two that must is the same rule with no
  # tail. Reported one round after the exclusion landed.
  #
  # Gated by mode rather than by adding a column, because the alternative is a
  # second list of which lanes are pre-commit lanes -- run_common_checks below
  # is the first, and ci/checks/manifest.yml's `pre_commit:` field is arguably a
  # third. Quick keeps the one that already decides it.
  if [ "${CI_GATE_USE_LANES:-0}" = "1" ] \
     && { [ "$MODE" = "full" ] || [ "$MODE" = "ship" ]; } \
     && [ -f "ci/config/lanes.conf" ]; then
    # In ship mode the plan is read from the pushed tree, not from disk.
    #
    # This branch selected the lanes *and* read them from the worktree copy, so
    # an unstaged edit deleting the `node` and `tests-shell` rows removed the
    # frontend tests from the run and -- with `tests-shell` gone -- removed the
    # one lane whose drift scan would have reported the edit. A pushed commit
    # carrying a failing frontend test then passed, on a plan that is not in the
    # push. Which lanes run is gate configuration like any other, and ship mode
    # reports on HEAD.
    #
    # An unreadable HEAD copy falls through to the standard ship plan below
    # rather than to the worktree file. That plan is the wider one -- it runs
    # every lane rather than the configured subset -- so the failure direction
    # is more work, not less, and it needs no error path of its own. `full` is
    # deliberately a whole-tree run against the worktree and keeps reading it.
    local _lp_file="ci/config/lanes.conf" _lp_tmp="" _lp_ok=1
    if [ "$MODE" = "ship" ]; then
      _lp_ok=0
      if command -v git >/dev/null 2>&1 \
        && git rev-parse --verify HEAD >/dev/null 2>&1 \
        && _lp_tmp="$(ci::common::mktemp_file lane-plan 2>/dev/null)" \
        && git show "HEAD:ci/config/lanes.conf" > "$_lp_tmp" 2>/dev/null \
        && [ -s "$_lp_tmp" ]; then
        _lp_file="$_lp_tmp"
        _lp_ok=1
      else
        [ -n "$_lp_tmp" ] && rm -f "$_lp_tmp"
        _lp_tmp=""
        echo "ci/config/lanes.conf is not readable in HEAD; running the full ship plan." >&2
      fi
    fi
    if [ "$_lp_ok" -eq 1 ]; then
      # lanes.conf has 5 columns (id|name|command|blocking|description); only id and
      # command are consumed here. Unused columns are read into `_` so shellcheck does
      # not flag them (SC2034) while still consuming every delimited field.
      local lane_id="" lane_cmd=""
      while IFS='|' read -r lane_id _ lane_cmd _ _; do
        lane_id="$(echo "$lane_id" | tr -d ' ')"
        lane_cmd="$(echo "$lane_cmd" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
        [ -z "$lane_id" ] && continue
        case "$lane_id" in '#'* ) continue ;; esac
        [ -z "$lane_cmd" ] && continue
        run_phase "${lane_id}:${lane_cmd}"
      done < "$_lp_file"
      [ -n "$_lp_tmp" ] && rm -f "$_lp_tmp"
      return 0
    fi
  fi

  case "$MODE" in
    quick)
      run_common_checks
      ;;
    full|ship)
      # The content-free cases are decided at the top of this function, above
      # the scheduler choice. Only ship reaches them, and only when the hook
      # said so: `full` is a deliberate whole-tree run and must not be narrowed
      # by an environment variable.
      run_full_or_ship_checks
      ;;
    debt)
      run_phase "git-safety:./ci/checks/git-safety.sh"
      run_phase "debt:./ci/checks/debt.sh"
      ;;
    *)
      echo "Unhandled preflight mode: $MODE"
      INFRA_ISSUE_SEEN=1
      COMMANDS_FAILED+=("mode-dispatch")
      AGG_RESULT="$(ci::common::merge_results "$AGG_RESULT" "$CI_RESULT_FAIL_INFRA")"
      ;;
  esac
}

# ---- --fix preprocessing ----
if [ "$RUN_FIX" -eq 1 ] && [ -f "./ci/checks/format.sh" ]; then
  echo "=== --fix: running format.sh with CI_GATE_FIX=1 ==="
  if ! CI_GATE_FIX=1 ./ci/checks/format.sh 2>&1; then
    echo "ERROR: --fix formatting step failed." >&2
    exit "$CI_RESULT_FAIL_INFRA"
  fi
  echo "=== --fix: format pass complete; continuing gate ==="
fi

# Which tree is this run vouching for? Checks that compare the worktree against
# what will be committed need to know, and the answer is not the changeset mode:
# --all rewrites that to "all" without changing what the gate is standing behind.
# quick is the pre-commit gate and stands behind the index; ship is the pre-push
# gate and stands behind HEAD, which is already committed.
export CI_GATE_MODE="$MODE"

# The runner reads its per-check `timeout_sec` from checks.yml too, and it was
# reading the worktree's while scheduling read HEAD's. Same file, two answers,
# and both directions bite: an unstaged `timeout_sec: 1` on typecheck-js kills an
# unrelated outgoing check seconds in and blocks the push, while an unstaged
# increase weakens the budget using configuration the push does not carry.
#
# Pointed at the copy _checks_config_resolve already materialised, so there is
# one HEAD-derived file and not a second `git show`. Resolved here rather than
# left to the first lane, because this is the point where the value has to be in
# the environment the runner inherits. Outside ship mode the resolver hands back
# the worktree path, which is what `full` and `quick` are supposed to read, so
# the export is correct in every mode rather than conditional on one.
_checks_config_resolve
export CI_CHECKS_CONFIG="$_CHECKS_CONFIG_FILE"

# And is that tree the one being pushed? Ship mode resolves its *ranges* from
# the hook's SHAs, so the history checks read the right commits — but every
# check that runs content reads the worktree, and `git push origin
# other-branch` leaves those two describing different branches. The gate would
# then install, typecheck, test and build the branch you are standing on and
# report the result as a verdict on the one going out.
#
# Refused rather than worked around. Running the outgoing tip would mean
# checking it out, or building a second worktree, inside a pre-push hook —
# which is a much larger change than this is a bug, and one that fails badly
# on a dirty tree. Declining to answer is the honest option, and the remedy is
# one command for the developer.
if [ "$MODE" = "ship" ] && type ci::git::worktree_covers_push >/dev/null 2>&1 \
  && ! ci::git::worktree_covers_push; then
  ci::git::explain_push_tip_drift >&2
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

# ---- Changeset detection ----
CHANGESET_MODE="pre-commit"
case "$MODE" in
  quick) CHANGESET_MODE="pre-commit" ;;
  ship) CHANGESET_MODE="pre-push" ;;
  full) CHANGESET_MODE="all" ;;
  debt) CHANGESET_MODE="pr" ;;
esac
[ "$RUN_ALL" -eq 1 ] && CHANGESET_MODE="all"

if type ci::changeset::detect >/dev/null 2>&1; then
  # The status matters. Detection failing and detection finding nothing produce
  # the same empty result, and the fast-exit below reads an empty result as
  # "nothing relevant changed" — so with `git diff` broken but everything else
  # working, a tree with two staged secrets exited 0 with "No relevant changes
  # detected. Skipping gate.", no check run and no report written, while
  # git-safety.sh run directly against that same tree exited 20. Every
  # per-check hardening sits downstream of this line; nothing downstream can
  # recover from never being called.
  #
  # A failure here means the gate does not know what changed, which is not a
  # licence to skip — it is the reason to run everything. RUN_ALL both
  # suppresses the fast-exit and makes _check_should_skip stop filtering.
  _changeset_rc=0
  ci::changeset::detect "$CHANGESET_MODE" || _changeset_rc=$?
  if [ "$_changeset_rc" -ne 0 ]; then
    echo "Changeset detection failed (status ${_changeset_rc}); running the full set." >&2
    RUN_ALL=1
    # RUN_ALL alone only suppresses the fast-exit below. _check_should_skip
    # filters on _CI_CHANGESET_CHECKS, and that list is seeded non-empty
    # (_CI_CHANGESET_ALWAYS_CHECKS) even when nothing was detected — so leaving
    # it set would keep the filter live and drop every lane it does not name,
    # turning a detection failure into a near-silent skip by another route.
    # Empty is what the filter's own guard treats as "do not filter".
    _CI_CHANGESET_CHECKS=""
    _CI_CHANGESET_LANGUAGES=""
  fi
  if type ci::changeset::emit_json >/dev/null 2>&1; then
    ci::changeset::emit_json || true
  fi
  # Fast-exit if no relevant files changed (applies to every mode except --all).
  #
  # "No language detected" is not the same as "nothing relevant changed". The
  # gate's own inputs — ci/, .githooks/, .gitignore — carry no language, so a
  # commit touching only those would exit 0 here, before run_mode, making the
  # tests-shell path exception in _check_should_skip unreachable. That is how a
  # regression in the frontend lib/ negations could ship with its own test never
  # running.
  #
  # And never in ship mode, whatever the changeset says. The changeset here is
  # derived from an endpoint diff, and ship mode's safety checks are not: they
  # walk every outgoing commit. Both ways the two disagree are live. A first
  # push with no upstream and no default-branch merge base yields an empty
  # changeset; and a secret added by one outgoing commit and removed by a later
  # one leaves the endpoint diff clean while both commits are pushed. Either
  # exits here with PASS, before git-safety.sh has run at all — the pre-push
  # gate skipped on the strength of a diff that was never what it stands behind.
  #
  # And "no relevant changes" now means no changes. It used to mean "no path
  # that named a language", which is a statement about the *lanes* and not about
  # the tree: every path can be auto-ignored -- `dist/`, `build/`, `vendor/`,
  # `node_modules/` -- or simply carry no language at all, a .md file or a
  # LICENSE, and the changeset comes out with an empty language list and a
  # perfectly non-empty file list. git-safety is the lane that blocks exactly
  # those paths, and it never ran: staging `dist/bundle.js` containing an AWS
  # key exits 20 from git-safety.sh directly and exited 0 through preflight,
  # with no lane executed at all. The changeset filter downstream is where a
  # lane decides it has nothing to do; this exit is only for a run with nothing
  # in front of it.
  _pf_has_paths=0
  case "${_CI_CHANGESET_FILES_RAW:-}" in *[![:space:]]*) _pf_has_paths=1 ;; esac
  if [ "$RUN_ALL" -eq 0 ] && [ "$MODE" != "ship" ] \
    && [ "$_pf_has_paths" -eq 0 ] \
    && [ -z "${_CI_CHANGESET_LANGUAGES:-}" ]; then
    echo "No relevant changes detected. Skipping gate."
    exit 0
  fi
fi

# ---- Run mode ----
run_mode

# ---- Compute final state ----
FINAL_RESULT_NAME="$(ci::common::result_name "$AGG_RESULT")"
FINISH_ISO="$(ci::common::now_iso_utc)"
FINISH_EPOCH="$(date '+%s')"
DURATION_SEC=$((FINISH_EPOCH - START_EPOCH))

# Performance budget check
if [ "$MODE" = "quick" ] && [ -n "${CI_GATE_PRE_COMMIT_BUDGET:-}" ]; then
  if [ "$DURATION_SEC" -gt "$CI_GATE_PRE_COMMIT_BUDGET" ]; then
    echo "WARNING: Quick mode budget exceeded: ${DURATION_SEC}s > ${CI_GATE_PRE_COMMIT_BUDGET}s"
  fi
fi
if [ "$MODE" = "ship" ] && [ -n "${CI_GATE_PRE_PUSH_BUDGET:-}" ]; then
  if [ "$DURATION_SEC" -gt "$CI_GATE_PRE_PUSH_BUDGET" ]; then
    echo "WARNING: Ship mode budget exceeded: ${DURATION_SEC}s > ${CI_GATE_PRE_PUSH_BUDGET}s"
  fi
fi

CHANGED_FILES="$(ci::git::changed_files || true)"
STAGED_FILES="$(ci::git::staged_files || true)"

[ -z "$CHANGED_FILES" ] && CHANGED_FILES="(none)"
[ -z "$STAGED_FILES" ] && STAGED_FILES="(none)"

KNOWN_DEBT_STATUS="none"
[ "$KNOWN_DEBT_SEEN" -eq 1 ] && KNOWN_DEBT_STATUS="unchanged"

NEW_ISSUE_STATUS="none"
if [ "$NEW_ISSUE_SEEN" -eq 1 ]; then
  NEW_ISSUE_STATUS="found"
fi
if [ "$INFRA_ISSUE_SEEN" -eq 1 ]; then
  if [ "$NEW_ISSUE_SEEN" -eq 1 ]; then
    NEW_ISSUE_STATUS="found+infra-failure"
  else
    NEW_ISSUE_STATUS="infra-failure"
  fi
fi

NEXT_ACTION="If result is PASS or PASS_WITH_KNOWN_DEBT, continue with ship flow."
if [ "$AGG_RESULT" -eq "$CI_RESULT_FAIL_NEW_ISSUE" ] || [ "$AGG_RESULT" -eq "$CI_RESULT_FAIL_INFRA" ]; then
  NEXT_ACTION="Fix failing checks and rerun ./ci/preflight.sh --mode $MODE."
fi

# ---- Markdown report (summary.md) ----
SUMMARY_MD="${CI_REPORT_DIR}/summary.md"
: > "$SUMMARY_MD"

_md() { printf '%s
' "$1" >> "$SUMMARY_MD"; }

_md "# CI Gate Report"
_md ""
_md "| Field | Value |"
_md "|-------|-------|"
_md "| Mode | $MODE |"
_md "| Branch | ${BRANCH:-unknown} |"
_md "| Commit SHA | ${COMMIT_SHA:-unknown} |"
_md "| Start | $START_ISO |"
_md "| Finish | $FINISH_ISO |"
_md "| Duration (s) | $DURATION_SEC |"
_md "| Result | **$FINAL_RESULT_NAME** |"
_md "| Known Debt | $KNOWN_DEBT_STATUS |"
_md "| New Issues | $NEW_ISSUE_STATUS |"
_md "| Security | $SECURITY_STATUS |"
_md "| Build | $BUILD_STATUS |"
_md ""
_md "## Check Results"
_md ""
_md "| Check | Status | Duration |"
_md "|-------|--------|----------|"

_timing_idx=0
while [ "$_timing_idx" -lt "${#_TIMING_LABELS[@]}" ]; do
  _tlabel="${_TIMING_LABELS[$_timing_idx]}"
  _tstart="${_TIMING_STARTS[$_timing_idx]}"
  _tend="${_TIMING_ENDS[$_timing_idx]}"
  _trc="${_TIMING_RESULTS[$_timing_idx]}"
  _tdur=$(( _tend - _tstart ))
  _tstatus="$(ci::common::result_name "$_trc")"
  _md "| $_tlabel | $_tstatus | ${_tdur}s |"
  _timing_idx=$(( _timing_idx + 1 ))
done

_md ""
_md "## Changed Files"
_md '```text'
_md "$CHANGED_FILES"
_md '```'
_md ""
_md "## Next Action"
_md "$NEXT_ACTION"

# Also write to legacy latest.md for backward compat
ci::report::append_markdown "# Local CI Report"
ci::report::append_markdown ""
ci::report::append_markdown "- mode: $MODE"
ci::report::append_markdown "- branch: ${BRANCH:-unknown}"
ci::report::append_markdown "- commit SHA: ${COMMIT_SHA:-unknown}"
ci::report::append_markdown "- start time: $START_ISO"
ci::report::append_markdown "- finish time: $FINISH_ISO"
ci::report::append_markdown "- duration (seconds): $DURATION_SEC"
ci::report::append_markdown "- final result: $FINAL_RESULT_NAME"
ci::report::append_markdown "- known debt status: $KNOWN_DEBT_STATUS"
ci::report::append_markdown "- new issue status: $NEW_ISSUE_STATUS"
ci::report::append_markdown "- security status: $SECURITY_STATUS"
ci::report::append_markdown "- build status: $BUILD_STATUS"
ci::report::append_markdown ""
ci::report::append_markdown "## Changed Files"
ci::report::append_markdown '```text'
ci::report::append_block "$CHANGED_FILES"
ci::report::append_markdown '```'
ci::report::append_markdown ""
ci::report::append_markdown "## Staged Files"
ci::report::append_markdown '```text'
ci::report::append_block "$STAGED_FILES"
ci::report::append_markdown '```'
ci::report::append_markdown ""
ci::report::append_markdown "## Commands Run"
if [ "${#COMMANDS_RUN[@]}" -eq 0 ]; then
  ci::report::append_markdown "- (none)"
else
  for cmd in "${COMMANDS_RUN[@]}"; do
    ci::report::append_markdown "- $cmd"
  done
fi
ci::report::append_markdown ""
ci::report::append_markdown "## Commands Passed"
if [ "${#COMMANDS_PASSED[@]}" -eq 0 ]; then
  ci::report::append_markdown "- (none)"
else
  for cmd in "${COMMANDS_PASSED[@]}"; do
    ci::report::append_markdown "- $cmd"
  done
fi
ci::report::append_markdown ""
ci::report::append_markdown "## Commands Failed"
if [ "${#COMMANDS_FAILED[@]}" -eq 0 ]; then
  ci::report::append_markdown "- (none)"
else
  for cmd in "${COMMANDS_FAILED[@]}"; do
    ci::report::append_markdown "- $cmd"
  done
fi
ci::report::append_markdown ""
ci::report::append_markdown "## Next Recommended Action"
ci::report::append_markdown "- $NEXT_ACTION"

# ---- summary.json ----
SUMMARY_JSON="${CI_REPORT_DIR}/summary.json"
{
printf '{
'
printf '  "schema_version": 1,
'
printf '  "mode": "%s",
' "$(_json_escape "$MODE")"
printf '  "branch": "%s",
' "$(_json_escape "${BRANCH:-unknown}")"
printf '  "sha": "%s",
' "$(_json_escape "${COMMIT_SHA:-unknown}")"
printf '  "start": "%s",
' "$(_json_escape "$START_ISO")"
printf '  "finish": "%s",
' "$(_json_escape "$FINISH_ISO")"
printf '  "duration_sec": %d,
' "$DURATION_SEC"
printf '  "result": "%s",
' "$(_json_escape "$FINAL_RESULT_NAME")"
printf '  "known_debt_status": "%s",
' "$(_json_escape "$KNOWN_DEBT_STATUS")"
printf '  "new_issue_status": "%s",
' "$(_json_escape "$NEW_ISSUE_STATUS")"
printf '  "security_status": "%s",
' "$(_json_escape "$SECURITY_STATUS")"
printf '  "build_status": "%s",
' "$(_json_escape "$BUILD_STATUS")"
printf '  "checks": ['
_json_first=1
_sjidx=0
while [ "$_sjidx" -lt "${#_TIMING_LABELS[@]}" ]; do
  _sjlabel="${_TIMING_LABELS[$_sjidx]}"
  _sjstart="${_TIMING_STARTS[$_sjidx]}"
  _sjend="${_TIMING_ENDS[$_sjidx]}"
  _sjrc="${_TIMING_RESULTS[$_sjidx]}"
  _sjdur=$(( _sjend - _sjstart ))
  _sjstatus="$(ci::common::result_name "$_sjrc")"
  if [ "$_json_first" = "1" ]; then
    printf '
'
    _json_first=0
  else
    printf ',
'
  fi
  printf '    {"label":"%s","result":"%s","exit_code":%d,"duration_sec":%d}'     "$(_json_escape "$_sjlabel")" "$(_json_escape "$_sjstatus")" "$_sjrc" "$_sjdur"
  _sjidx=$(( _sjidx + 1 ))
done
if [ "$_json_first" = "0" ]; then
  printf '
  '
fi
printf ']
'
printf '}
'
} > "$SUMMARY_JSON"

# ---- trace.json (Chrome Trace Event format) ----
TRACE_JSON="${CI_REPORT_DIR}/trace.json"
{
printf '[
'
_trace_first=1
_tridx=0
while [ "$_tridx" -lt "${#_TIMING_LABELS[@]}" ]; do
  _trlabel="${_TIMING_LABELS[$_tridx]}"
  _trstart="${_TIMING_STARTS[$_tridx]}"
  _trend="${_TIMING_ENDS[$_tridx]}"
  # Convert epoch seconds to microseconds (relative to run start)
  _trts=$(( (_trstart - START_EPOCH) * 1000000 ))
  _trdur=$(( (_trend - _trstart) * 1000000 ))
  [ "$_trdur" -lt 0 ] && _trdur=0
  if [ "$_trace_first" = "1" ]; then
    _trace_first=0
  else
    printf ',
'
  fi
  printf '  {"name":"%s","ph":"X","ts":%d,"dur":%d,"pid":1,"tid":1}'     "$(_json_escape "$_trlabel")" "$_trts" "$_trdur"
  _tridx=$(( _tridx + 1 ))
done
printf '
]
'
} > "$TRACE_JSON"

# ---- junit.xml ----
JUNIT_XML="${CI_REPORT_DIR}/junit.xml"
{
  _pass_count="${#COMMANDS_PASSED[@]}"
  _fail_count="${#COMMANDS_FAILED[@]}"
  _total_count="${#COMMANDS_RUN[@]}"
  printf '<?xml version="1.0" encoding="UTF-8"?>\n'
  printf '<testsuites name="ci-gate-%s" tests="%d" failures="%d" time="%d">\n' \
    "$(_xml_escape "$MODE")" "$_total_count" "$_fail_count" "$DURATION_SEC"
  printf '  <testsuite name="ci-gate-%s" tests="%d" failures="%d" time="%d">\n' \
    "$(_xml_escape "$MODE")" "$_total_count" "$_fail_count" "$DURATION_SEC"
  _juidx=0
  while [ "$_juidx" -lt "${#_TIMING_LABELS[@]}" ]; do
    _julabel="${_TIMING_LABELS[$_juidx]}"
    _justart="${_TIMING_STARTS[$_juidx]}"
    _juend="${_TIMING_ENDS[$_juidx]}"
    _jurc="${_TIMING_RESULTS[$_juidx]}"
    _judur=$(( _juend - _justart ))
    _julabel_escaped="$(_xml_escape "$_julabel")"
    if [ "$_jurc" -eq 0 ] || [ "$_jurc" -eq 10 ]; then
      printf '    <testcase name="%s" time="%d"/>\n' "$_julabel_escaped" "$_judur"
    else
      printf '    <testcase name="%s" time="%d">\n' "$_julabel_escaped" "$_judur"
      printf '      <failure message="Check %s failed with exit code %d"/>\n' "$_julabel_escaped" "$_jurc"
      printf '    </testcase>\n'
    fi
    _juidx=$(( _juidx + 1 ))
  done
  printf '  </testsuite>\n'
  printf '</testsuites>\n'
} > "$JUNIT_XML"

# ---- sarif.json ----
SARIF_JSON="${CI_REPORT_DIR}/sarif.json"
{
printf '{
'
# shellcheck disable=SC2016
printf '  "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
'
printf '  "version": "2.1.0",
'
printf '  "runs": [{
'
printf '    "tool": {"driver": {"name": "ci-gate", "version": "1.0.0", "rules": ['
_rule_first=1
_saidx=0
while [ "$_saidx" -lt "${#_TIMING_LABELS[@]}" ]; do
  _salabel="${_TIMING_LABELS[$_saidx]}"
  if [ "$_rule_first" = "1" ]; then
    printf '
'
    _rule_first=0
  else
    printf ',
'
  fi
  printf '      {"id":"%s","name":"%s","shortDescription":{"text":"CI check %s"}}' "$(_json_escape "$_salabel")" "$(_json_escape "$_salabel")" "$(_json_escape "$_salabel")"
  _saidx=$(( _saidx + 1 ))
done
if [ "$_rule_first" = "0" ]; then
  printf '
    '
fi
printf ']}},
'
printf '    "results": ['
_sarif_first=1
_saidx=0
while [ "$_saidx" -lt "${#_TIMING_LABELS[@]}" ]; do
  _salabel="${_TIMING_LABELS[$_saidx]}"
  _sarc="${_TIMING_RESULTS[$_saidx]}"
  if [ "$_sarc" -ne 0 ] && [ "$_sarc" -ne 10 ]; then
    if [ "$_sarif_first" = "1" ]; then
      printf '
'
      _sarif_first=0
    else
      printf ',
'
    fi
    printf '      {"ruleId":"%s","level":"error","message":{"text":"Check %s failed with exit code %d"},"locations":[{"physicalLocation":{"artifactLocation":{"uri":"ci/reports/%s.log"}}}]}'       "$(_json_escape "$_salabel")" "$(_json_escape "$_salabel")" "$_sarc" "$(_json_escape "$_salabel")"
  fi
  _saidx=$(( _saidx + 1 ))
done
if [ "$_sarif_first" = "0" ]; then
  printf '
    '
fi
printf ']
'
printf '  }]
'
printf '}
'
} > "$SARIF_JSON"

# ---- HTML dashboard (index.html) ----
INDEX_HTML="${CI_REPORT_DIR}/index.html"
{
  _html_rows=""
  _hridx=0
  while [ "$_hridx" -lt "${#_TIMING_LABELS[@]}" ]; do
    _hrlabel="${_TIMING_LABELS[$_hridx]}"
    _hrstart="${_TIMING_STARTS[$_hridx]}"
    _hrend="${_TIMING_ENDS[$_hridx]}"
    _hrrc="${_TIMING_RESULTS[$_hridx]}"
    _hrdur=$(( _hrend - _hrstart ))
    _hrstatus="$(ci::common::result_name "$_hrrc")"
    case "$_hrrc" in
      0) _hrclass="pass" ;;
      10) _hrclass="warn" ;;
      *) _hrclass="fail" ;;
    esac
    _log_file="${CI_REPORT_DIR}/${_hrlabel}.log"
    _log_content=""
    [ -f "$_log_file" ] && _log_content="$(sed \
      -e 's/&/\&amp;/g' \
      -e 's/</\&lt;/g' \
      -e 's/>/\&gt;/g' \
      -e 's/"/\&quot;/g' \
      -e "s/'/\&#39;/g" \
      "$_log_file" 2>/dev/null || true)"
    _html_rows="${_html_rows}<tr class=\"${_hrclass}\"><td>$(_html_escape "$_hrlabel")</td><td>$(_html_escape "$_hrstatus")</td><td>${_hrdur}s</td><td><details><summary>show log</summary><pre>${_log_content}</pre></details></td></tr>"
    _hridx=$(( _hridx + 1 ))
  done

  _res_class="pass"
  [ "$AGG_RESULT" -ne 0 ] && _res_class="fail"
  _mode_html="$(_html_escape "$MODE")"
  _result_html="$(_html_escape "$FINAL_RESULT_NAME")"
  _changed_files_html="$(_html_escape "$CHANGED_FILES")"
  _next_action_html="$(_html_escape "$NEXT_ACTION")"

  cat << ENDHTML
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CI Gate Report</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #333; }
  h1 { border-bottom: 2px solid #ddd; padding-bottom: .5rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: left; padding: .5rem; border-bottom: 1px solid #eee; }
  th { background: #f5f5f5; }
  .pass { color: #2ea44f; }
  .warn { color: #d29922; }
  .fail { color: #cf222e; }
  .badge { display: inline-block; padding: .2rem .5rem; border-radius: 4px; font-size: .85rem; font-weight: 600; }
  .badge.pass { background: #dafbe1; }
  .badge.fail { background: #ffebe9; }
  pre { background: #f6f8fa; padding: 1rem; overflow-x: auto; font-size: .85rem; }
  details summary { cursor: pointer; color: #0969da; }
</style>
</head>
<body>
<h1>CI Gate Report</h1>
<p><strong>Mode:</strong> ${_mode_html} | <strong>Result:</strong> <span class="badge ${_res_class}">${_result_html}</span> | <strong>Duration:</strong> ${DURATION_SEC}s</p>
<table>
<thead><tr><th>Check</th><th>Status</th><th>Duration</th><th>Log</th></tr></thead>
<tbody>
${_html_rows}
</tbody>
</table>
<h2>Changed Files</h2>
<pre>${_changed_files_html}</pre>
<h2>Next Action</h2>
<p>${_next_action_html}</p>
</body>
</html>
ENDHTML
} > "$INDEX_HTML"

# ---- TUI summary ----
echo ""
echo "╔══════════════════════════════════════╗"
echo "║        CI Gate Final Status          ║"
echo "╚══════════════════════════════════════╝"
echo "  Mode:     $MODE"
echo "  Result:   $FINAL_RESULT_NAME"
echo "  Duration: ${DURATION_SEC}s"
echo "  Branch:   ${BRANCH:-unknown}"
echo "  Commit:   ${COMMIT_SHA:-unknown}"
echo ""

if [ "${#COMMANDS_PASSED[@]}" -gt 0 ]; then
  echo "  ✓ Passed: ${#COMMANDS_PASSED[@]} check(s)"
fi
if [ "${#COMMANDS_FAILED[@]}" -gt 0 ]; then
  echo "  ✗ Failed: ${#COMMANDS_FAILED[@]} check(s)"
  for _fc in "${COMMANDS_FAILED[@]}"; do
    echo "    - $_fc"
  done
fi

# Per-check timing table
if [ "${#_TIMING_LABELS[@]}" -gt 0 ]; then
  echo ""
  printf "  %-30s %-12s %6s
" "CHECK" "STATUS" "TIME"
  printf "  %-30s %-12s %6s
" "-----" "------" "----"
  _ttidx=0
  while [ "$_ttidx" -lt "${#_TIMING_LABELS[@]}" ]; do
    _ttlabel="${_TIMING_LABELS[$_ttidx]}"
    _ttrc="${_TIMING_RESULTS[$_ttidx]}"
    _ttstart="${_TIMING_STARTS[$_ttidx]}"
    _ttend="${_TIMING_ENDS[$_ttidx]}"
    _ttdur=$(( _ttend - _ttstart ))
    _ttstatus="$(ci::common::result_name "$_ttrc")"
    printf "  %-30s %-12s %5ds
" "$_ttlabel" "$_ttstatus" "$_ttdur"
    _ttidx=$(( _ttidx + 1 ))
  done
fi

# --profile: ASCII bar chart
if [ "$RUN_PROFILE" -eq 1 ] && [ "${#_TIMING_LABELS[@]}" -gt 0 ]; then
  echo ""
  echo "  ── Timing profile ──────────────────"
  _max_dur=1
  _ptidx=0
  while [ "$_ptidx" -lt "${#_TIMING_LABELS[@]}" ]; do
    _ptstart="${_TIMING_STARTS[$_ptidx]}"
    _ptend="${_TIMING_ENDS[$_ptidx]}"
    _ptdur=$(( _ptend - _ptstart ))
    [ "$_ptdur" -gt "$_max_dur" ] && _max_dur="$_ptdur"
    _ptidx=$(( _ptidx + 1 ))
  done
  _ptidx=0
  while [ "$_ptidx" -lt "${#_TIMING_LABELS[@]}" ]; do
    _ptlabel="${_TIMING_LABELS[$_ptidx]}"
    _ptstart="${_TIMING_STARTS[$_ptidx]}"
    _ptend="${_TIMING_ENDS[$_ptidx]}"
    _ptdur=$(( _ptend - _ptstart ))
    _ptbars=$(( (_ptdur * 40) / _max_dur ))
    [ "$_ptbars" -lt 1 ] && _ptbars=1
    _ptbar=""
    _pbi=0
    while [ "$_pbi" -lt "$_ptbars" ]; do
      _ptbar="${_ptbar}█"
      _pbi=$(( _pbi + 1 ))
    done
    printf "  %-20s %s %ds
" "$_ptlabel" "$_ptbar" "$_ptdur"
    _ptidx=$(( _ptidx + 1 ))
  done
fi

echo ""
echo "  Reports:"
echo "    ci/reports/summary.md"
echo "    ci/reports/summary.json"
echo "    ci/reports/trace.json"
echo "    ci/reports/junit.xml"
echo "    ci/reports/sarif.json"
echo "    ci/reports/index.html"
echo ""
echo "  Fix command (if needed):"
echo "    ./ci/preflight.sh --mode $MODE --fix"

if [ "$AGG_RESULT" -eq "$CI_RESULT_FAIL_NEW_ISSUE" ] || [ "$AGG_RESULT" -eq "$CI_RESULT_FAIL_INFRA" ]; then
  exit 1
fi

exit 0
