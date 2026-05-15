#!/usr/bin/env bash
set -Eeuo pipefail

CI_RESULT_PASS=0
CI_RESULT_PASS_WITH_KNOWN_DEBT=10
CI_RESULT_FAIL_NEW_ISSUE=20
CI_RESULT_FAIL_INFRA=30
# Distinguish explicit help requests from failures in CLI parsers.
CI_RESULT_HELP=99
export CI_RESULT_PASS CI_RESULT_PASS_WITH_KNOWN_DEBT CI_RESULT_FAIL_NEW_ISSUE CI_RESULT_FAIL_INFRA CI_RESULT_HELP

ci::common::project_root() {
  git rev-parse --show-toplevel 2>/dev/null || pwd
}

ci::common::section() {
  printf "\n========== %s ==========\n" "$1"
}

ci::common::command_exists() {
  command -v "$1" >/dev/null 2>&1
}

ci::common::result_name() {
  case "${1:-999}" in
    "$CI_RESULT_PASS") echo "PASS" ;;
    "$CI_RESULT_PASS_WITH_KNOWN_DEBT") echo "PASS_WITH_KNOWN_DEBT" ;;
    "$CI_RESULT_FAIL_NEW_ISSUE") echo "FAIL_NEW_ISSUE" ;;
    "$CI_RESULT_FAIL_INFRA") echo "FAIL_INFRA" ;;
    "$CI_RESULT_HELP") echo "HELP" ;;
    *) echo "FAIL_INFRA" ;;
  esac
}

ci::common::result_severity() {
  case "${1:-999}" in
    "$CI_RESULT_PASS") echo 0 ;;
    "$CI_RESULT_PASS_WITH_KNOWN_DEBT") echo 1 ;;
    "$CI_RESULT_FAIL_NEW_ISSUE") echo 2 ;;
    "$CI_RESULT_FAIL_INFRA") echo 3 ;;
    "$CI_RESULT_HELP") echo 0 ;;
    *) echo 3 ;;
  esac
}

ci::common::merge_results() {
  local current="${1:-$CI_RESULT_PASS}"
  local next="${2:-$CI_RESULT_PASS}"
  local s_current s_next
  s_current="$(ci::common::result_severity "$current")"
  s_next="$(ci::common::result_severity "$next")"
  if [ "$s_next" -gt "$s_current" ]; then
    echo "$next"
  else
    echo "$current"
  fi
}

ci::common::parse_mode() {
  local mode="full"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --mode)
        if [ "$#" -lt 2 ]; then
          echo "Missing value for --mode" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        mode="$2"
        shift 2
        ;;
      --mode=*)
        mode="${1#--mode=}"
        if [ -z "$mode" ]; then
          echo "Missing value for --mode" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        shift
        ;;
      --help|-h)
        echo "Usage: ./ci/preflight.sh [--mode quick|full|ship|debt]"
        return "$CI_RESULT_HELP"
        ;;
      *)
        echo "Unknown argument: $1" >&2
        return "$CI_RESULT_FAIL_INFRA"
        ;;
    esac
  done

  case "$mode" in
    quick|full|ship|debt)
      echo "$mode"
      ;;
    *)
      echo "Invalid mode: $mode" >&2
      return "$CI_RESULT_FAIL_INFRA"
      ;;
  esac
}

ci::common::now_iso_utc() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

# ci::common::load_gate_config <file> – simple grep-based YAML parser for gate.yml
ci::common::load_gate_config() {
  local config_file="${1:-ci/config/gate.yml}"
  [ -f "$config_file" ] || return 0
  local key val
  while IFS= read -r line; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%:*}"
    val="${line#*:}"
    key="$(printf '%s' "$key" | tr -d ' ')"
    val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//')"
    case "$key" in
      parallelism)
        [ -n "$val" ] && [ "$val" != "0" ] && export CI_GATE_PARALLEL="$val"
        ;;
      default_timeout_sec)
        [ -n "$val" ] && export CI_GATE_TIMEOUT="$val"
        ;;
      pre_commit_budget_sec)
        [ -n "$val" ] && export CI_GATE_PRE_COMMIT_BUDGET="$val"
        ;;
      pre_push_budget_sec)
        [ -n "$val" ] && export CI_GATE_PRE_PUSH_BUDGET="$val"
        ;;
      fail_fast_on_blocker)
        if [ "$val" = "true" ]; then export CI_GATE_FAIL_FAST=1; else export CI_GATE_FAIL_FAST=0; fi
        ;;
      incremental)
        if [ "$val" = "true" ]; then export CI_GATE_INCREMENTAL=1; else export CI_GATE_INCREMENTAL=0; fi
        ;;
      cache_enabled)
        if [ "$val" = "true" ]; then export CI_GATE_CACHE_ENABLED=1; else export CI_GATE_CACHE_ENABLED=0; fi
        ;;
      verbose)
        if [ "$val" = "true" ]; then export CI_GATE_VERBOSE=1; else export CI_GATE_VERBOSE=0; fi
        ;;
    esac
  done < "$config_file"
}

# ci::common::load_thresholds_config – simple parser for thresholds.yml
ci::common::load_thresholds_config() {
  local config_file="${1:-ci/config/thresholds.yml}"
  [ -f "$config_file" ] || return 0
  local key val
  while IFS= read -r line; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%:*}"
    val="${line#*:}"
    key="$(printf '%s' "$key" | tr -d ' ')"
    val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//')"
    case "$key" in
      coverage_min_percent)
        [ -n "$val" ] && export CI_GATE_COVERAGE_MIN="$val"
        ;;
      complexity_max)
        [ -n "$val" ] && export CI_GATE_COMPLEXITY_MAX="$val"
        ;;
      bundle_size_max)
        [ -n "$val" ] && export CI_GATE_BUNDLE_SIZE_MAX="$val"
        ;;
    esac
  done < "$config_file"
}
