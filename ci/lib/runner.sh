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
# ci::runner::_declared_timeout <check-id> – echo the check's own timeout_sec
# from checks.yml, or nothing when it declares none.
ci::runner::_declared_timeout() {
  local want="$1"
  local file="${CI_CHECKS_CONFIG:-ci/config/checks.yml}"
  [ -f "$file" ] || return 0
  awk -v want="${want}" -v wantkey="  ${want}:" '
    $0 == wantkey { found = 1; next }
    found && $0 ~ /^[[:space:]]*timeout_sec:/ {
      sub(/^[[:space:]]*timeout_sec:[[:space:]]*/, "")
      sub(/[[:space:]]*#.*$/, "")
      sub(/[[:space:]]+$/, "")
      # Validated, not filtered. `gsub(/[^0-9]/, "")` deleted the non-digits and
      # joined what was left, so `1e3` became 13 and `-1` became 1: the runner
      # then killed a blocking check seconds in and reported an infrastructure
      # timeout that the configuration never asked for. A value that is not a
      # whole number of seconds is a configuration error, and silently turning
      # it into a different number is the worst of the available answers.
      if ($0 ~ /^[0-9]+$/ && $0 + 0 > 0) {
        print
        exit 0
      }
      # A diagnostic is not a result. This printed and exited 0 with no value,
      # so submit could not tell "no timeout declared" from "the declared one is
      # unusable" and silently fell back to the global timeout, or to none at
      # all. A typo on the long `tests-shell` blocker therefore removed its
      # bound and left full and ship validation to hang, which is the same
      # "silently turning it into a different number" the comment above refuses
      # -- reached by discarding the number instead of rewriting it.
      print "timeout_sec for " want " is not a positive whole number: " $0 > "/dev/stderr"
      exit 3
    }
    found && $0 ~ /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { exit }
  ' "$file"
}

# ci::runner::_timeout_cmd <seconds> – echo the command prefix that enforces
# <seconds>, or return 1 when this host has nothing that can enforce it.
#
# Split out of submit so the "no utility available" answer is a value the
# caller has to handle rather than an empty string it can fall through on --
# which is exactly how the bound used to get dropped in silence.
ci::runner::_timeout_cmd() {
  local secs="$1"
  if command -v timeout >/dev/null 2>&1; then
    printf 'timeout %s' "$secs"
    return 0
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    printf 'gtimeout %s' "$secs"
    return 0
  fi
  return 1
}

ci::runner::submit() {
  local job_id="$1"
  shift
  local check_script="$1"
  shift
  local args=("$@")

  local log_file="${_CI_RUNNER_LOG_DIR}/${job_id}.log"
  local pid_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.pid"
  local start_file="${_CI_RUNNER_JOBS_DIR}/${job_id}.start"

  # Determine timeout. checks.yml may declare timeout_sec per check; gate.yml's
  # default_timeout_sec applies otherwise.
  #
  # The per-check value used to be documentation -- nothing read it -- and the
  # shell suites have since grown past the 20-minute default, so `tests-shell`
  # was killed at the cap and reported FAIL_INFRA. A blocking lane that cannot
  # finish is a lane that does not run, which is the failure this gate exists to
  # catch, announced in a single word at the end of a two-hour run.
  #
  # Computed before the sequential branch on purpose: with CI_GATE_PARALLEL=0,
  # or a single-worker pool, that branch executes the check directly, so a
  # timeout applied only to the background path meant a supported mode ignored
  # both the declared value and the global one -- and could hang indefinitely.
  local check_timeout="${CI_GATE_TIMEOUT:-}"
  local declared_timeout declared_rc=0
  declared_timeout="$(ci::runner::_declared_timeout "$job_id")" || declared_rc=$?
  if [ "$declared_rc" -ne 0 ]; then
    # Configuration this runner cannot act on, recorded as infrastructure
    # instead of run without the bound it asked for. Running the check anyway
    # is the answer that looks harmless and is not: the value exists precisely
    # because the global default is wrong for that check, so ignoring it is how
    # a blocking lane runs unbounded and the gate reports a timeout nobody
    # configured -- or never reports at all.
    #
    # Written the way the sequential branch below records a finished job, so the
    # pool's bookkeeping sees an ordinary completed job rather than a launch
    # that never happened.
    {
      echo "Check '${job_id}' declares a timeout_sec this runner cannot use."
      echo "  ${CI_CHECKS_CONFIG:-ci/config/checks.yml} has to carry a positive"
      echo "  whole number of seconds, or nothing at all. Refusing to run the"
      echo "  check unbounded under a configuration that asked for a bound."
    } > "$log_file" 2>&1
    printf '%d' "$CI_RESULT_FAIL_INFRA" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.rc"
    printf '%s' "$(ci::runner::_epoch)" > "$start_file"
    printf '%s' "$(ci::runner::_epoch)" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.end"
    printf '%d' "$$" > "$pid_file"
    return 0
  fi
  [ -n "$declared_timeout" ] && check_timeout="$declared_timeout"

  local timeout_cmd="" timeout_rc=0
  if [ -n "$check_timeout" ] && [ "$check_timeout" != "0" ]; then
    # Assigned separately from `local` on purpose: `local x="$(cmd)"` reports the
    # status of `local`, not of cmd, so the refusal below would never be seen.
    timeout_cmd="$(ci::runner::_timeout_cmd "$check_timeout")" || timeout_rc=$?
    if [ "$timeout_rc" -ne 0 ]; then
      # FIX: a bound was asked for and this host has no utility that can keep
      # it. Falling through to the unbounded branch below is the same mistake
      # the declared-timeout arm above refuses: the cap exists because someone
      # decided this check must not run forever, so dropping it silently is how
      # a hung blocker like `tests-shell` stalls preflight for as long as the
      # caller will wait, with nothing in the log saying the bound was skipped.
      #
      # Not a hypothetical host, either -- a stock macOS box has neither
      # `timeout` nor `gtimeout` until coreutils is installed, so this is a
      # configuration the runner has to refuse rather than assume away. Nor is
      # it opt-in: ci/config/gate.yml carries default_timeout_sec, so on such a
      # host this arm answers for every scheduled check and the whole run stops
      # on infrastructure. That is the intended reading -- a gate that cannot
      # bound its checks has not validated anything -- and it is why the message
      # names the install that fixes it rather than only the condition.
      # Recorded exactly like that arm, so the pool's bookkeeping sees an
      # ordinary completed job rather than a launch that never happened.
      {
        echo "Check '${job_id}' asks for a ${check_timeout}s timeout this host cannot enforce."
        echo "  Neither 'timeout' nor 'gtimeout' is on PATH, so the bound cannot be"
        echo "  applied. Install GNU coreutils (on macOS 'brew install coreutils'"
        echo "  provides gtimeout), or clear the timeout rather than have the check"
        echo "  run unbounded under a configuration that asked for a bound."
      } > "$log_file" 2>&1
      printf '%d' "$CI_RESULT_FAIL_INFRA" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.rc"
      printf '%s' "$(ci::runner::_epoch)" > "$start_file"
      printf '%s' "$(ci::runner::_epoch)" > "${_CI_RUNNER_JOBS_DIR}/${job_id}.end"
      printf '%d' "$$" > "$pid_file"
      return 0
    fi
  fi

  # If sequential mode (max_jobs=1 or CI_GATE_PARALLEL=0)
  if [ "${CI_GATE_PARALLEL:-}" = "0" ] || [ "$_CI_RUNNER_MAX_JOBS" -eq 1 ]; then
    local exit_code=0
    printf '%s' "$(ci::runner::_epoch)" > "$start_file"
    set +e
    if [ -n "$timeout_cmd" ]; then
      $timeout_cmd "$check_script" ${args[@]+"${args[@]}"} > "$log_file" 2>&1
    else
      "$check_script" ${args[@]+"${args[@]}"} > "$log_file" 2>&1
    fi
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
