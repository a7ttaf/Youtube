#!/usr/bin/env bash
# ci/lib/junit.sh – JUnit XML helper library.
# Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_CI_JUNIT_SUITE_NAME=""
_CI_JUNIT_OUTPUT_FILE=""
_CI_JUNIT_TESTS=0
_CI_JUNIT_FAILURES=0
_CI_JUNIT_SKIPPED=0
_CI_JUNIT_ERRORS=0
_CI_JUNIT_CASES=""  # accumulated <testcase> XML strings

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ci_junit_xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  s="${s//\"/&quot;}"
  s="${s//\'/&apos;}"
  printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ci::junit::init <testsuite_name> <output_file>
ci::junit::init() {
  _CI_JUNIT_SUITE_NAME="${1:?ci::junit::init requires testsuite_name as \$1}"
  _CI_JUNIT_OUTPUT_FILE="${2:?ci::junit::init requires output_file as \$2}"
  _CI_JUNIT_TESTS=0
  _CI_JUNIT_FAILURES=0
  _CI_JUNIT_SKIPPED=0
  _CI_JUNIT_ERRORS=0
  _CI_JUNIT_CASES=""
  mkdir -p "$(dirname "$_CI_JUNIT_OUTPUT_FILE")"
}

# ci::junit::add_test <classname> <name> <time_sec> <status>
# status: pass | skip
ci::junit::add_test() {
  local classname="${1:?}"
  local name="${2:?}"
  local time_sec="${3:-0}"
  local status="${4:-pass}"

  local esc_class esc_name
  esc_class="$(_ci_junit_xml_escape "$classname")"
  esc_name="$(_ci_junit_xml_escape "$name")"

  _CI_JUNIT_TESTS=$((_CI_JUNIT_TESTS + 1))

  local case_xml
  if [ "$status" = "skip" ]; then
    _CI_JUNIT_SKIPPED=$((_CI_JUNIT_SKIPPED + 1))
    case_xml="    <testcase classname=\"${esc_class}\" name=\"${esc_name}\" time=\"${time_sec}\"><skipped/></testcase>"
  else
    case_xml="    <testcase classname=\"${esc_class}\" name=\"${esc_name}\" time=\"${time_sec}\"/>"
  fi

  if [ -z "$_CI_JUNIT_CASES" ]; then
    _CI_JUNIT_CASES="$case_xml"
  else
    _CI_JUNIT_CASES="${_CI_JUNIT_CASES}
${case_xml}"
  fi
}

# ci::junit::add_failure <classname> <name> <time_sec> <message>
ci::junit::add_failure() {
  local classname="${1:?}"
  local name="${2:?}"
  local time_sec="${3:-0}"
  local message="${4:-}"

  local esc_class esc_name esc_msg
  esc_class="$(_ci_junit_xml_escape "$classname")"
  esc_name="$(_ci_junit_xml_escape "$name")"
  esc_msg="$(_ci_junit_xml_escape "$message")"

  _CI_JUNIT_TESTS=$((_CI_JUNIT_TESTS + 1))
  _CI_JUNIT_FAILURES=$((_CI_JUNIT_FAILURES + 1))

  local case_xml
  case_xml="    <testcase classname=\"${esc_class}\" name=\"${esc_name}\" time=\"${time_sec}\"><failure message=\"${esc_msg}\">${esc_msg}</failure></testcase>"

  if [ -z "$_CI_JUNIT_CASES" ]; then
    _CI_JUNIT_CASES="$case_xml"
  else
    _CI_JUNIT_CASES="${_CI_JUNIT_CASES}
${case_xml}"
  fi
}

# ci::junit::finish – write complete JUnit XML document
ci::junit::finish() {
  local esc_suite
  esc_suite="$(_ci_junit_xml_escape "$_CI_JUNIT_SUITE_NAME")"
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%S')"

  cat > "$_CI_JUNIT_OUTPUT_FILE" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="${esc_suite}" tests="${_CI_JUNIT_TESTS}" failures="${_CI_JUNIT_FAILURES}" errors="${_CI_JUNIT_ERRORS}" skipped="${_CI_JUNIT_SKIPPED}" timestamp="${ts}">
${_CI_JUNIT_CASES}
</testsuite>
XML
}
