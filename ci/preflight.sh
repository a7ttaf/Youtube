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
    [ -f "$path" ] && files+=("$path")
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

_check_disabled_in_config() {
  local wanted="$1"

  if [ -f "ci/config/checks.yml" ]; then
    local in_checks=0 in_list_check=0 check_id="" enabled="true" line=""
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) continue ;;  
      esac

      if printf '%s\n' "$line" | grep -qE '^checks:[[:space:]]*$'; then
        in_checks=1
        continue
      fi

      if [ "$in_checks" = "1" ] && printf '%s\n' "$line" | grep -qE '^[^[:space:]]'; then
        in_checks=0
        check_id=""
      fi

      if [ "$in_checks" = "1" ] && printf '%s\n' "$line" | grep -qE '^  [A-Za-z0-9_-]+:[[:space:]]*(#.*)?$'; then
        check_id=$(printf '%s\n' "$line" | sed -E 's/^  ([A-Za-z0-9_-]+):[[:space:]]*(#.*)?$/\1/')
        enabled="true"
        in_list_check=0
        continue
      fi

      if printf '%s\n' "$line" | grep -qE '^[[:space:]]*-[[:space:]]*id:[[:space:]]*(.*)$'; then
        in_list_check=1
        check_id=$(printf '%s\n' "$line" | sed -E 's/^[[:space:]]*-[[:space:]]*id:[[:space:]]*(.*)$/\1/')
        check_id="$(echo "$check_id" | tr -d ' ' | tr -d '"')"
        enabled="true"
      fi

      if { [ "$in_checks" = "1" ] || [ "$in_list_check" = "1" ]; } && printf '%s\n' "$line" | grep -qE '^[[:space:]]*enabled:[[:space:]]*(.*)$'; then
        enabled=$(printf '%s\n' "$line" | sed -E 's/^[[:space:]]*enabled:[[:space:]]*(.*)$/\1/')
        enabled="$(echo "$enabled" | sed 's/[[:space:]]//g; s/#.*$//')"
      fi

      if [ "$check_id" = "$wanted" ] && [ "$enabled" = "false" ]; then
        return 0  # skip
      fi
    done < "ci/config/checks.yml"
  fi

  return 1
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
    if [ "$found" = "0" ] && [ "$label" = "tests-shell" ] \
      && printf '%s\n' "${_CI_CHANGESET_FILES_RAW:-}" \
        | grep -qE '(^|[[:space:]])(ci/|\.githooks/|\.gitignore([[:space:]]|$)|frontend/README\.md([[:space:]]|$))'; then
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
      case "$label" in
        git-safety|changed-files|security|debt|test-layout) ;;
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
  esac
  return 0
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
}

run_full_or_ship_checks() {
  run_phase     "git-safety:./ci/checks/git-safety.sh"     "changed-files:./ci/checks/changed-files.sh"     "branch-protection:./ci/checks/branch-protection.sh"     "test-layout:./ci/checks/test-layout.sh"
  run_phase     "security:./ci/checks/security.sh"
  run_phase     "node:./ci/checks/node.sh"     "python:./ci/checks/python.sh"     "build:./ci/checks/build.sh"
  run_phase     "tests-shell:./ci/checks/tests-shell.sh"
  run_phase     "debt:./ci/checks/debt.sh"
}

run_mode() {
  # If lanes.conf exists and CI_GATE_USE_LANES=1, read it. Otherwise use hardcoded defaults.
  if [ "${CI_GATE_USE_LANES:-0}" = "1" ] && [ -f "ci/config/lanes.conf" ]; then
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
    done < "ci/config/lanes.conf"
    return 0
  fi

  case "$MODE" in
    quick)
      run_common_checks
      ;;
    full|ship)
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
  ci::changeset::detect "$CHANGESET_MODE" || true
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
  if [ "$RUN_ALL" -eq 0 ] && [ -z "${_CI_CHANGESET_LANGUAGES:-}" ] \
    && ! printf '%s\n' "${_CI_CHANGESET_FILES_RAW:-}" \
      | grep -qE '(^|[[:space:]])(ci/|\.githooks/|\.gitignore([[:space:]]|$))'; then
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
