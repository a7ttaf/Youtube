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
    ci::runner::init "$CI_REPORT_DIR"
  fi
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

_check_should_skip() {
  local label="$1"

  # 1. Respect checks.yml enabled: false
  if [ -f "ci/config/checks.yml" ]; then
    local in_check=0 check_id="" enabled="true"
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) continue ;;
      esac
      if [[ "$line" =~ ^[[:space:]]*-[[:space:]]*id:[[:space:]]*(.*)$ ]]; then
        in_check=1
        check_id="${BASH_REMATCH[1]}"
        check_id="$(echo "$check_id" | tr -d ' ' | tr -d '"')"
        enabled="true"
      fi
      if [ "$in_check" = "1" ] && [[ "$line" =~ ^[[:space:]]*enabled:[[:space:]]*(.*)$ ]]; then
        enabled="${BASH_REMATCH[1]}"
        enabled="$(echo "$enabled" | tr -d ' ')"
      fi
      if [ "$in_check" = "1" ] && [ "$check_id" = "$label" ] && [ "$enabled" = "false" ]; then
        return 0  # skip
      fi
    done < "ci/config/checks.yml"
  fi

  # 2. Skip by changeset if incremental and no relevant files changed
  if [ "${CI_GATE_INCREMENTAL:-1}" = "1" ] && [ -n "${_CI_CHANGESET_CHECKS:-}" ]; then
    local found=0
    local ck
    for ck in $_CI_CHANGESET_CHECKS; do
      [ "$ck" = "$label" ] && found=1 && break
    done
    if [ "$found" = "0" ]; then
      # Always-run checks are never skipped by changeset
      case "$label" in
        git-safety|changed-files|security|debt) ;;
        *) return 0 ;;
      esac
    fi
  fi

  return 1
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
    local cache_key="" cache_hit=0
    if [ "${CI_GATE_CACHE_ENABLED:-1}" = "1" ] && type ci::cache::key >/dev/null 2>&1; then
      local tool_ver="unknown"
      case "$label" in
        node) tool_ver="$(ci::cache::tool_version node)" ;;
        python) tool_ver="$(ci::cache::tool_version python3)" ;;
        *) tool_ver="bash-$(bash --version | head -1)" ;;
      esac
      local files_hash=""
      if [ -n "${_CI_CHANGESET_FILES_RAW:-}" ]; then
        files_hash="$(printf '%s' "$_CI_CHANGESET_FILES_RAW" | sha256sum | cut -d' ' -f1)"
      else
        files_hash="$(git rev-parse HEAD 2>/dev/null || echo 'none')"
      fi
      local config_hash=""
      if [ -f "ci/config/checks.yml" ]; then
        config_hash="$(ci::cache::_sha256_file ci/config/checks.yml)"
      else
        config_hash="none"
      fi
      cache_key="$(ci::cache::key "$label" "$tool_ver" "$files_hash" "$config_hash")"
      local cache_dest="${CI_REPORT_DIR}/.cache/${label}"
      if ci::cache::get "$cache_key" "$cache_dest"; then
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
          printf '%d' "$$" > "${_CI_RUNNER_JOBS_DIR}/${label}.pid"
          printf '%d' "$cached_rc" > "${_CI_RUNNER_JOBS_DIR}/${label}.rc"
          printf '%s' "$(date '+%s')" > "${_CI_RUNNER_JOBS_DIR}/${label}.start"
          printf '%s' "$(date '+%s')" > "${_CI_RUNNER_JOBS_DIR}/${label}.end"
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
    if [ "${CI_GATE_CACHE_ENABLED:-1}" = "1" ] && type ci::cache::key >/dev/null 2>&1; then
      local cache_dest="${CI_REPORT_DIR}/.cache/${lbl}"
      mkdir -p "$cache_dest"
      printf '%d' "$rc" > "$cache_dest/result.txt"
      printf '%s' "$output" > "$cache_dest/output.txt"
      # Cache key was computed earlier; recompute for simplicity
      local tool_ver="unknown"
      case "$lbl" in
        node) tool_ver="$(ci::cache::tool_version node)" ;;
        python) tool_ver="$(ci::cache::tool_version python3)" ;;
        *) tool_ver="bash-$(bash --version | head -1)" ;;
      esac
      local files_hash=""
      if [ -n "${_CI_CHANGESET_FILES_RAW:-}" ]; then
        files_hash="$(printf '%s' "$_CI_CHANGESET_FILES_RAW" | sha256sum | cut -d' ' -f1)"
      else
        files_hash="$(git rev-parse HEAD 2>/dev/null || echo 'none')"
      fi
      local config_hash=""
      if [ -f "ci/config/checks.yml" ]; then
        config_hash="$(ci::cache::_sha256_file ci/config/checks.yml)"
      else
        config_hash="none"
      fi
      local put_key
      put_key="$(ci::cache::key "$lbl" "$tool_ver" "$files_hash" "$config_hash")"
      ci::cache::put "$put_key" "$cache_dest"
    fi
  done
}

# ---- Mode check groups ----
run_common_checks() {
  run_phase     "git-safety:./ci/checks/git-safety.sh"     "changed-files:./ci/checks/changed-files.sh"
  run_phase     "node:./ci/checks/node.sh"     "python:./ci/checks/python.sh"
}

run_full_or_ship_checks() {
  run_phase     "git-safety:./ci/checks/git-safety.sh"     "changed-files:./ci/checks/changed-files.sh"
  run_phase     "security:./ci/checks/security.sh"
  run_phase     "node:./ci/checks/node.sh"     "python:./ci/checks/python.sh"     "build:./ci/checks/build.sh"
  run_phase     "debt:./ci/checks/debt.sh"
}

run_mode() {
  # If lanes.conf exists and CI_GATE_USE_LANES=1, read it. Otherwise use hardcoded defaults.
  if [ "${CI_GATE_USE_LANES:-0}" = "1" ] && [ -f "ci/config/lanes.conf" ]; then
    local lane_id="" lane_checks=""
    while IFS='|' read -r lane_id _lane_name _lane_cmd lane_checks _lane_blocking _lane_desc; do
      lane_id="$(echo "$lane_id" | tr -d ' ')"
      [ -z "$lane_id" ] && continue
      case "$lane_id" in '#') continue ;; esac
      local check_entries=""
      local chk
      for chk in $lane_checks; do
        local script_path="./ci/checks/${chk}.sh"
        [ -f "$script_path" ] || script_path="./ci/checks/${chk}"
        if [ -n "$check_entries" ]; then
          check_entries="${check_entries} \"${chk}:${script_path}\""
        else
          check_entries="\"${chk}:${script_path}\""
        fi
      done
      eval "run_phase $check_entries"
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
  CI_GATE_FIX=1 ./ci/checks/format.sh 2>&1 || true
  echo "=== --fix: format pass complete; continuing gate ==="
fi

# ---- Changeset detection ----
CHANGESET_MODE="pre-commit"
case "$MODE" in
  quick) CHANGESET_MODE="pre-commit" ;;
  ship) CHANGESET_MODE="pre-push" ;;
  full|debt) CHANGESET_MODE="pr" ;;
esac
[ "$RUN_ALL" -eq 1 ] && CHANGESET_MODE="all"

if type ci::changeset::detect >/dev/null 2>&1; then
  ci::changeset::detect "$CHANGESET_MODE" || true
  if type ci::changeset::emit_json >/dev/null 2>&1; then
    ci::changeset::emit_json || true
  fi
  # Fast-exit if no relevant files changed (applies to every mode except --all).
  if [ "$RUN_ALL" -eq 0 ] && [ -z "${_CI_CHANGESET_FILES_RAW:-}" ]; then
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
' "$MODE"
printf '  "branch": "%s",
' "${BRANCH:-unknown}"
printf '  "sha": "%s",
' "${COMMIT_SHA:-unknown}"
printf '  "start": "%s",
' "$START_ISO"
printf '  "finish": "%s",
' "$FINISH_ISO"
printf '  "duration_sec": %d,
' "$DURATION_SEC"
printf '  "result": "%s",
' "$FINAL_RESULT_NAME"
printf '  "known_debt_status": "%s",
' "$KNOWN_DEBT_STATUS"
printf '  "new_issue_status": "%s",
' "$NEW_ISSUE_STATUS"
printf '  "security_status": "%s",
' "$SECURITY_STATUS"
printf '  "build_status": "%s",
' "$BUILD_STATUS"
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
  printf '    {"label":"%s","result":"%s","exit_code":%d,"duration_sec":%d}'     "$_sjlabel" "$_sjstatus" "$_sjrc" "$_sjdur"
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
  printf '  {"name":"%s","ph":"X","ts":%d,"dur":%d,"pid":1,"tid":1}'     "$_trlabel" "$_trts" "$_trdur"
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
    "$MODE" "$_total_count" "$_fail_count" "$DURATION_SEC"
  printf '  <testsuite name="ci-gate-%s" tests="%d" failures="%d" time="%d">\n' \
    "$MODE" "$_total_count" "$_fail_count" "$DURATION_SEC"
  _juidx=0
  while [ "$_juidx" -lt "${#_TIMING_LABELS[@]}" ]; do
    _julabel="${_TIMING_LABELS[$_juidx]}"
    _justart="${_TIMING_STARTS[$_juidx]}"
    _juend="${_TIMING_ENDS[$_juidx]}"
    _jurc="${_TIMING_RESULTS[$_juidx]}"
    _judur=$(( _juend - _justart ))
    if [ "$_jurc" -eq 0 ] || [ "$_jurc" -eq 10 ]; then
      printf '    <testcase name="%s" time="%d"/>\n' "$_julabel" "$_judur"
    else
      printf '    <testcase name="%s" time="%d">\n' "$_julabel" "$_judur"
      printf '      <failure message="Check %s failed with exit code %d"/>\n' "$_julabel" "$_jurc"
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
  printf '      {"id":"%s","name":"%s","shortDescription":{"text":"CI check %s"}}' "$_salabel" "$_salabel" "$_salabel"
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
    printf '      {"ruleId":"%s","level":"error","message":{"text":"Check %s failed with exit code %d"},"locations":[{"physicalLocation":{"artifactLocation":{"uri":"ci/reports/%s.log"}}}]}'       "$_salabel" "$_salabel" "$_sarc" "$_salabel"
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
    _html_rows="${_html_rows}<tr class=\"${_hrclass}\"><td>${_hrlabel}</td><td>${_hrstatus}</td><td>${_hrdur}s</td><td><details><summary>show log</summary><pre>${_log_content}</pre></details></td></tr>"
    _hridx=$(( _hridx + 1 ))
  done

  _res_class="pass"
  [ "$AGG_RESULT" -ne 0 ] && _res_class="fail"

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
<p><strong>Mode:</strong> ${MODE} | <strong>Result:</strong> <span class="badge ${_res_class}">${FINAL_RESULT_NAME}</span> | <strong>Duration:</strong> ${DURATION_SEC}s</p>
<table>
<thead><tr><th>Check</th><th>Status</th><th>Duration</th><th>Log</th></tr></thead>
<tbody>
${_html_rows}
</tbody>
</table>
<h2>Changed Files</h2>
<pre>${CHANGED_FILES}</pre>
<h2>Next Action</h2>
<p>${NEXT_ACTION}</p>
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
