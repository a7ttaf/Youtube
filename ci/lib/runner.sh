#!/usr/bin/env bash
# ci/lib/runner.sh – Parallel pipeline orchestrator.
# Bash 3.2+ compatible (no process substitution with arrays, no mapfile).
# Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# State (module-level variables, reset by ci::runner::init)
# ---------------------------------------------------------------------------
_CI_RUNNER_MAX_JOBS=0
_CI_RUNNER_LOG_DIR=""
_CI_RUNNER_JOBS_DIR=""   # temp dir for PID files and result files

# Parallel job tracking via flat files (Bash 3.2 compat – no associative arrays)
# File naming conventions under $_CI_RUNNER_JOBS_DIR/:
#   <job_id>.pid     – PID of background process
#   <job_id>.rc      – exit code (written when job completes)
#   <job_id>.start   – epoch seconds at job start
#   <job_id>.end     – epoch seconds at job end

_CI_RUNNER_JOBS_DIR_CLEANUP=""  # set to jobs dir path; cleaned on EXIT

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

ci::runner::_cleanup() {
  if [ -n "$_CI_RUNNER_JOBS_DIR_CLEANUP" ] && [ -d "$_CI_RUNNER_JOBS_DIR_CLEANUP" ]; then
    rm -rf "$_CI_RUNNER_JOBS_DIR_CLEANUP"
  fi
}

ci::runner::_epoch() {
  date +%s 2>/dev/null || echo "0"
}

# ci::runner::_running_count – count jobs still running
ci::runner::_running_count() {
  # Save and suppress ERR trap so kill -0 non-zero returns don't fire it.
  local _saved_err
  _saved_err="$(trap -p ERR 2>/dev/null || true)"
  trap - ERR

  local count=0
  local pid_file pid
  for pid_file in "${_CI_RUNNER_JOBS_DIR}"/*.pid; do
    [ -f "$pid_file" ] || continue
    pid="$(cat "$pid_file" 2>/dev/null)" || continue
    [ -z "$pid" ] && continue
    local rc_file="${pid_file%.pid}.rc"
    [ -f "$rc_file" ] && continue  # already done
    # Check if PID is still alive
    if kill -0 "$pid" 2>/dev/null; then
      count=$((count + 1))
    fi
  done
  printf '%d' "$count"

  eval "${_saved_err:-trap - ERR}"
}

# ci::runner::_collect_finished – write .rc file for any completed jobs
ci::runner::_collect_finished() {
  # Save and suppress ERR trap so kill -0 / wait non-zero returns don't fire it.
  local _saved_err
  _saved_err="$(trap -p ERR 2>/dev/null || true)"
  trap - ERR

  local pid_file pid job_id rc_file
  for pid_file in "${_CI_RUNNER_JOBS_DIR}"/*.pid; do
    [ -f "$pid_file" ] || continue
    pid="$(cat "$pid_file" 2>/dev/null)" || continue
    [ -z "$pid" ] && continue
    job_id="$(basename "$pid_file" .pid)"
    rc_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.rc"
    [ -f "$rc_file" ] && continue  # already collected
    # Check if still running
    if ! kill -0 "$pid" 2>/dev/null; then
      # Process ended – collect exit code via wait
      local exit_code=0
      set +e
      wait "$pid" 2>/dev/null
      exit_code=$?
      set -e
      printf '%d' "$exit_code" > "$rc_file"
      printf '%s' "$(ci::runner::_epoch)" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.end"
    fi
  done

  eval "${_saved_err:-trap - ERR}"
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ci::runner::init [<max_jobs>] – initialize runner state
ci::runner::init() {
  local max_jobs="${1:-0}"

  # Honor CI_GATE_PARALLEL override
  if [ -n "${CI_GATE_PARALLEL:-}" ]; then
    max_jobs="$CI_GATE_PARALLEL"
  fi

  # Auto-detect CPU count
  if [ "$max_jobs" -eq 0 ] 2>/dev/null; then
    local ncpu=1
    set +e
    if command -v nproc >/dev/null 2>&1; then
      ncpu="$(nproc 2>/dev/null)" || ncpu=1
    elif command -v sysctl >/dev/null 2>&1; then
      ncpu="$(sysctl -n hw.ncpu 2>/dev/null)" || ncpu=1
    fi
    set -e
    max_jobs="$ncpu"
  fi

  _CI_RUNNER_MAX_JOBS="$max_jobs"

  # Resolve log directory (sibling to this lib file)
  local lib_dir root_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root_dir="$(cd "$lib_dir/../.." && pwd)"
  _CI_RUNNER_LOG_DIR="${root_dir}/ci/reports"
  mkdir -p "$_CI_RUNNER_LOG_DIR"

  # Create jobs state dir inside ci/reports (no /tmp)
  _CI_RUNNER_JOBS_DIR="${_CI_RUNNER_LOG_DIR}/.runner_jobs_$$"
  mkdir -p "$_CI_RUNNER_JOBS_DIR"
  _CI_RUNNER_JOBS_DIR_CLEANUP="$_CI_RUNNER_JOBS_DIR"

  # Register cleanup trap (merge with any existing EXIT trap)
  trap 'ci::runner::_cleanup' EXIT
}

# ci::runner::submit <job_id> <check_script> [args...]
# Submits a job to the parallel pool. Blocks if pool is full.
ci::runner::submit() {
  local job_id="$1"
  shift
  local check_script="$1"
  shift
  local args=("$@")

  local log_file="${_CI_RUNNER_LOG_DIR}/${job_id}.log"
  local pid_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.pid"
  local start_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.start"

  # If sequential mode (max_jobs=1 or CI_GATE_PARALLEL=0)
  if [ "${CI_GATE_PARALLEL:-}" = "0" ] || [ "$_CI_RUNNER_MAX_JOBS" -eq 1 ]; then
    local exit_code=0
    printf '%s' "$(ci::runner::_epoch)" > "$start_file"
    set +e
    "$check_script" ${args[@]+"${args[@]}"} > "$log_file" 2>&1
    exit_code=$?
    set -e
    printf '%d' "$exit_code" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.rc"
    printf '%s' "$(ci::runner::_epoch)" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.end"
    # Use fake PID to mark as submitted
    printf '%d' "$$" > "$pid_file"
    return 0
  fi

  # Wait until there's room in the pool
  while true; do
    ci::runner::_collect_finished
    local running
    running="$(ci::runner::_running_count)"
    if [ "$running" -lt "$_CI_RUNNER_MAX_JOBS" ]; then
      break
    fi
    sleep 0.2 2>/dev/null || sleep 1
  done

  # Determine timeout
  local timeout_cmd=""
  if [ -n "${CI_GATE_TIMEOUT:-}" ] && [ "${CI_GATE_TIMEOUT}" != "0" ]; then
    if command -v timeout >/dev/null 2>&1; then
      timeout_cmd="timeout ${CI_GATE_TIMEOUT}"
    elif command -v gtimeout >/dev/null 2>&1; then
      timeout_cmd="gtimeout ${CI_GATE_TIMEOUT}"
    fi
  fi

  # Launch background job
  printf '%s' "$(ci::runner::_epoch)" > "$start_file"
  (
    trap - ERR  # don't inherit the outer ERR trap into the check subshell
    set +e
    if [ -n "$timeout_cmd" ]; then
      $timeout_cmd "$check_script" ${args[@]+"${args[@]}"} > "$log_file" 2>&1
    else
      "$check_script" ${args[@]+"${args[@]}"} > "$log_file" 2>&1
    fi
    ec=$?
    printf '%d' "$ec" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.rc"
    printf '%s' "$(ci::runner::_epoch)" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.end"
  ) &
  local bg_pid=$!
  printf '%d' "$bg_pid" > "$pid_file"
}

# ci::runner::wait_all – wait for all submitted jobs to complete
ci::runner::wait_all() {
  # Save and suppress ERR trap so internal polling non-zero returns don't fire it.
  local _saved_err
  _saved_err="$(trap -p ERR 2>/dev/null || true)"
  trap - ERR

  while true; do
    ci::runner::_collect_finished
    local running
    running="$(ci::runner::_running_count)"
    [ "$running" -eq 0 ] && break
    sleep 0.5 2>/dev/null || sleep 1
  done
  # Final reap: collect any children that exited between the last poll and now.
  set +e
  wait 2>/dev/null
  set -e
  ci::runner::_collect_finished

  eval "${_saved_err:-trap - ERR}"
}

# ci::runner::get_result <job_id> – echo exit code, or 255 if not found
ci::runner::get_result() {
  local job_id="$1"
  local rc_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.rc"
  if [ -f "$rc_file" ]; then
    cat "$rc_file"
  else
    printf '255'
    printf '[runner] WARNING: job "%s" has no .rc file; runner lost track of it\n' "$job_id" >&2
  fi
}

# ci::runner::get_output <job_id> – print captured log output
ci::runner::get_output() {
  local job_id="$1"
  local log_file="${_CI_RUNNER_LOG_DIR}/${job_id}.log"
  if [ -f "$log_file" ]; then
    cat "$log_file"
  fi
}

# ci::runner::print_summary – print TUI-style pass/fail/skip summary
ci::runner::print_summary() {
  local pass=0 fail=0 skip=0 total=0
  local job_id rc start_epoch end_epoch duration

  # Enumerate all submitted jobs
  local pid_file
  for pid_file in "${_CI_RUNNER_JOBS_DIR}"/*.pid; do
    [ -f "$pid_file" ] || continue
    job_id="$(basename "$pid_file" .pid)"
    total=$((total + 1))

    rc="$(ci::runner::get_result "$job_id")"

    local start_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.start"
    local end_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.end"
    start_epoch=0
    end_epoch=0
    if [ -f "$start_file" ]; then start_epoch="$(cat "$start_file" 2>/dev/null)"; fi
    if [ -f "$end_file"   ]; then end_epoch="$(cat "$end_file" 2>/dev/null)"; fi
    duration=$((end_epoch - start_epoch))

    local status_str color_code reset_code=""
    reset_code='\033[0m'
    case "$rc" in
      0)
        pass=$((pass + 1))
        status_str="PASS"
        color_code='\033[32m'
        ;;
      10)
        pass=$((pass + 1))
        status_str="PASS*"
        color_code='\033[33m'
        ;;
      *)
        fail=$((fail + 1))
        status_str="FAIL"
        color_code='\033[31m'
        ;;
    esac

    if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
      printf "  ${color_code}%-8s${reset_code} %-30s %3ds\n" "$status_str" "$job_id" "$duration"
    else
      printf "  %-8s %-30s %3ds\n" "$status_str" "$job_id" "$duration"
    fi
  done

  printf '\n'
  if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    printf '\033[1mSummary:\033[0m  total=%d  \033[32mpass=%d\033[0m  \033[31mfail=%d\033[0m  skip=%d\n' \
      "$total" "$pass" "$fail" "$skip"
  else
    printf 'Summary:  total=%d  pass=%d  fail=%d  skip=%d\n' \
      "$total" "$pass" "$fail" "$skip"
  fi
}
