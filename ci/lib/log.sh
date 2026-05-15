#!/usr/bin/env bash
# ci/lib/log.sh – Structured logging with levels and NDJSON event emission.
# Bash 3.2+ compatible. Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
CI_GATE_DEBUG="${CI_GATE_DEBUG:-0}"
CI_GATE_LOG_CHECK="${CI_GATE_LOG_CHECK:-}"
CI_GATE_LOG_PHASE="${CI_GATE_LOG_PHASE:-}"

# Lazily resolved at first use via ci::log::_init_paths
_CI_LOG_EVENTS_FILE=""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

ci::log::_init_paths() {
  if [ -n "$_CI_LOG_EVENTS_FILE" ]; then
    return 0
  fi
  local lib_dir root_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root_dir="$(cd "$lib_dir/../.." && pwd)"
  local reports_dir="${root_dir}/ci/reports"
  mkdir -p "$reports_dir"
  _CI_LOG_EVENTS_FILE="${CI_GATE_EVENTS_LOG:-${reports_dir}/events.ndjson}"
  export _CI_LOG_EVENTS_FILE
}

# ci::log::_color_supported – returns 0 if stdout is a TTY and TERM supports color
ci::log::_color_supported() {
  [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]
}

# Portable lowercase without ${var,,} (Bash 4+)
ci::log::_lc() {
  echo "$1" | tr '[:upper:]' '[:lower:]'
}

# Escape a string for JSON (minimal: backslash, double-quote, control chars)
ci::log::_json_escape() {
  local s="$1"
  # Replace \ first, then "
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  # Newlines and tabs
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

# Emit one NDJSON record to the events log
ci::log::_emit_ndjson() {
  local level="$1"
  local msg="$2"
  ci::log::_init_paths
  local ts check phase escaped_msg
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  check="$(ci::log::_json_escape "${CI_GATE_LOG_CHECK:-}")"
  phase="$(ci::log::_json_escape "${CI_GATE_LOG_PHASE:-}")"
  escaped_msg="$(ci::log::_json_escape "$msg")"
  printf '{"ts":"%s","level":"%s","check":"%s","phase":"%s","msg":"%s"}\n' \
    "$ts" "$level" "$check" "$phase" "$escaped_msg" \
    >> "$_CI_LOG_EVENTS_FILE"
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ci::log::debug() {
  [ "${CI_GATE_DEBUG:-0}" = "1" ] || return 0
  local msg="${*}"
  if ci::log::_color_supported; then
    printf '\033[2m[DEBUG] %s\033[0m\n' "$msg" >&2
  else
    printf '[DEBUG] %s\n' "$msg" >&2
  fi
  ci::log::_emit_ndjson "debug" "$msg"
}

ci::log::info() {
  local msg="${*}"
  if ci::log::_color_supported; then
    printf '\033[36m[INFO]  %s\033[0m\n' "$msg"
  else
    printf '[INFO]  %s\n' "$msg"
  fi
  ci::log::_emit_ndjson "info" "$msg"
}

ci::log::warn() {
  local msg="${*}"
  if ci::log::_color_supported; then
    printf '\033[33m[WARN]  %s\033[0m\n' "$msg" >&2
  else
    printf '[WARN]  %s\n' "$msg" >&2
  fi
  ci::log::_emit_ndjson "warn" "$msg"
}

ci::log::error() {
  local msg="${*}"
  if ci::log::_color_supported; then
    printf '\033[31m[ERROR] %s\033[0m\n' "$msg" >&2
  else
    printf '[ERROR] %s\n' "$msg" >&2
  fi
  ci::log::_emit_ndjson "error" "$msg"
}

# ci::log::section <title> – prints a box-drawing section header
ci::log::section() {
  local title="${*}"
  local len=${#title}
  # Build border line: "──" * (len+2)
  local border="" _i
  for _i in $(seq 1 $((len + 4))); do
    border="${border}─"
  done
  if ci::log::_color_supported; then
    printf '\033[1;36m┌%s┐\033[0m\n' "$border"
    printf '\033[1;36m│  %s  │\033[0m\n' "$title"
    printf '\033[1;36m└%s┘\033[0m\n' "$border"
  else
    printf '+%s+\n' "$border"
    printf '|  %s  |\n' "$title"
    printf '+%s+\n' "$border"
  fi
  ci::log::_emit_ndjson "info" "=== ${title} ==="
}
