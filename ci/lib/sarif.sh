#!/usr/bin/env bash
# ci/lib/sarif.sh – SARIF 2.1.0 output helper library.
# Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# State (file-scoped variables, reset on each init)
# ---------------------------------------------------------------------------
_CI_SARIF_TOOL_NAME=""
_CI_SARIF_TOOL_VERSION=""
_CI_SARIF_OUTPUT_FILE=""
_CI_SARIF_RESULTS=""   # accumulated JSON result objects (comma-separated)
_CI_SARIF_RULES=""     # accumulated rule objects
_CI_SARIF_RULE_IDS=""  # space-separated list of already-added rule IDs

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ci_sarif_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

_ci_sarif_severity_to_level() {
  case "${1:-warning}" in
    error|blocker|critical|high) echo "error" ;;
    warning|medium)              echo "warning" ;;
    note|advisory|low|info)     echo "note" ;;
    *)                           echo "warning" ;;
  esac
}

_ci_sarif_ensure_rule() {
  local rule_id="$1"
  local rule_desc="${2:-}"
  # Only add rule once
  case " ${_CI_SARIF_RULE_IDS} " in
    *" ${rule_id} "*) return 0 ;;
  esac
  _CI_SARIF_RULE_IDS="${_CI_SARIF_RULE_IDS} ${rule_id}"
  local escaped_id escaped_desc
  escaped_id="$(_ci_sarif_json_escape "$rule_id")"
  escaped_desc="$(_ci_sarif_json_escape "${rule_desc:-$rule_id}")"
  local rule_obj
  rule_obj="{\"id\":\"${escaped_id}\",\"name\":\"${escaped_id}\",\"shortDescription\":{\"text\":\"${escaped_desc}\"}}"
  if [ -z "$_CI_SARIF_RULES" ]; then
    _CI_SARIF_RULES="$rule_obj"
  else
    _CI_SARIF_RULES="${_CI_SARIF_RULES},${rule_obj}"
  fi
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ci::sarif::init <tool_name> <tool_version> <output_file>
ci::sarif::init() {
  _CI_SARIF_TOOL_NAME="${1:-unknown}"
  _CI_SARIF_TOOL_VERSION="${2:-0.0.0}"
  _CI_SARIF_OUTPUT_FILE="${3:?ci::sarif::init requires output_file as \$3}"
  _CI_SARIF_RESULTS=""
  _CI_SARIF_RULES=""
  _CI_SARIF_RULE_IDS=""
  mkdir -p "$(dirname "$_CI_SARIF_OUTPUT_FILE")"
}

# ci::sarif::add_result <rule_id> <message> <file> <line> <severity>
ci::sarif::add_result() {
  local rule_id="${1:?}"
  local message="${2:?}"
  local file="${3:-}"
  local line="${4:-1}"
  local severity="${5:-warning}"

  _ci_sarif_ensure_rule "$rule_id" "$message"

  local escaped_rule escaped_msg escaped_file level
  escaped_rule="$(_ci_sarif_json_escape "$rule_id")"
  escaped_msg="$(_ci_sarif_json_escape "$message")"
  escaped_file="$(_ci_sarif_json_escape "${file:-.}")"
  level="$(_ci_sarif_severity_to_level "$severity")"

  # Clamp line to a positive integer
  [[ "$line" =~ ^[0-9]+$ ]] || line=1
  [ "$line" -lt 1 ] && line=1

  local result_obj
  result_obj="{\"ruleId\":\"${escaped_rule}\",\"level\":\"${level}\",\"message\":{\"text\":\"${escaped_msg}\"},\"locations\":[{\"physicalLocation\":{\"artifactLocation\":{\"uri\":\"${escaped_file}\",\"uriBaseId\":\"%SRCROOT%\"},\"region\":{\"startLine\":${line}}}}]}"

  if [ -z "$_CI_SARIF_RESULTS" ]; then
    _CI_SARIF_RESULTS="$result_obj"
  else
    _CI_SARIF_RESULTS="${_CI_SARIF_RESULTS},${result_obj}"
  fi
}

# ci::sarif::finish – write complete SARIF document to the output file
ci::sarif::finish() {
  local tool_name version output_file
  tool_name="$(_ci_sarif_json_escape "$_CI_SARIF_TOOL_NAME")"
  version="$(_ci_sarif_json_escape "$_CI_SARIF_TOOL_VERSION")"
  output_file="$_CI_SARIF_OUTPUT_FILE"

  cat > "$output_file" <<SARIF
{
  "\$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
  "version": "2.1.0",
  "runs": [
    {
      "tool": {
        "driver": {
          "name": "${tool_name}",
          "version": "${version}",
          "rules": [${_CI_SARIF_RULES}]
        }
      },
      "results": [${_CI_SARIF_RESULTS}]
    }
  ]
}
SARIF
}
