#!/usr/bin/env bash
set -Eeuo pipefail

ci::report::init_paths() {
  local lib_dir=""
  local root_dir=""
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root_dir="$(cd "$lib_dir/../.." && pwd)"
  CI_REPORT_DIR="${root_dir}/ci/reports"
  CI_REPORT_MARKDOWN="${CI_REPORT_DIR}/latest.md"
  CI_REPORT_LOG="${CI_REPORT_DIR}/latest.log"
  export CI_REPORT_DIR CI_REPORT_MARKDOWN CI_REPORT_LOG
  mkdir -p "$CI_REPORT_DIR"
}

ci::report::ensure_initialized() {
  if [ -z "${CI_REPORT_DIR:-}" ] || [ -z "${CI_REPORT_MARKDOWN:-}" ] || [ -z "${CI_REPORT_LOG:-}" ]; then
    ci::report::init_paths
  fi
}

ci::report::reset_run_files() {
  ci::report::ensure_initialized
  : > "$CI_REPORT_MARKDOWN"
  : > "$CI_REPORT_LOG"
}

ci::report::append_log() {
  ci::report::ensure_initialized
  printf '%s\n' "$1" >> "$CI_REPORT_LOG"
}

ci::report::append_markdown() {
  ci::report::ensure_initialized
  printf '%s\n' "$1" >> "$CI_REPORT_MARKDOWN"
}

ci::report::append_block() {
  ci::report::ensure_initialized
  local content="${1:-}"
  if [ -n "$content" ]; then
    case "$content" in
      *$'\n')
        printf '%s' "$content" >> "$CI_REPORT_MARKDOWN"
        ;;
      *)
        printf '%s\n' "$content" >> "$CI_REPORT_MARKDOWN"
        ;;
    esac
  fi
}
