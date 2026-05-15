#!/usr/bin/env bash
set -Eeuo pipefail

CI_TEST_PLAN_LANES_PATH="ci/config/lanes.conf"
CI_TEST_PLAN_REPORT_JSON="ci/reports/test-plan.json"
CI_TEST_PLAN_REPORT_MD="ci/reports/test-plan.md"
TEST_PLAN_IMPACT_ARGS=()
TEST_PLAN_SKIPPED_LANES=()
CI_TEST_PLAN_HELP_REQUESTED=0

ci::test_plan::usage() {
  cat <<'USAGE'
Usage: ./ci/test-plan.sh [--base REF] [--rules PATH] [--lanes PATH]

Generates an advisory smart test plan from impact analysis, then writes:
  ci/reports/test-plan.json
  ci/reports/test-plan.md

Options:
  --base REF    Also include files changed between REF and HEAD.
  --rules PATH  Use a custom path-rules config file.
  --lanes PATH  Use a custom lane metadata config file.
  --help, -h    Show this help.
USAGE
}

ci::test_plan::parse_args() {
  TEST_PLAN_IMPACT_ARGS=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --base|--rules)
        if [ "$#" -lt 2 ]; then
          echo "Missing value for $1" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        TEST_PLAN_IMPACT_ARGS+=("$1" "$2")
        shift 2
        ;;
      --lanes)
        if [ "$#" -lt 2 ]; then
          echo "Missing value for --lanes" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        CI_TEST_PLAN_LANES_PATH="$2"
        shift 2
        ;;
      --help|-h)
        ci::test_plan::usage
        CI_TEST_PLAN_HELP_REQUESTED=1
        return "$CI_RESULT_PASS"
        ;;
      *)
        echo "Unknown test-plan argument: $1" >&2
        return "$CI_RESULT_FAIL_INFRA"
        ;;
    esac
  done
}

ci::test_plan::lookup_lane() {
  local wanted="$1"
  LANE_ID=""
  LANE_NAME=""
  LANE_COMMAND=""
  LANE_BLOCKING=""
  LANE_DESCRIPTION=""

  while IFS='|' read -r LANE_ID LANE_NAME LANE_COMMAND LANE_BLOCKING LANE_DESCRIPTION; do
    LANE_ID="$(ci::impact::trim "${LANE_ID:-}")"
    [ -z "$LANE_ID" ] && continue
    case "$LANE_ID" in \#*) continue ;; esac
    if [ "$LANE_ID" = "$wanted" ]; then
      LANE_NAME="$(ci::impact::trim "${LANE_NAME:-}")"
      LANE_COMMAND="$(ci::impact::trim "${LANE_COMMAND:-}")"
      LANE_BLOCKING="$(ci::impact::trim "${LANE_BLOCKING:-}")"
      LANE_DESCRIPTION="$(ci::impact::trim "${LANE_DESCRIPTION:-}")"
      return 0
    fi
  done < "$CI_TEST_PLAN_LANES_PATH"

  return 1
}

ci::test_plan::lane_selected() {
  local wanted="$1"
  if [ "${#IMPACT_LANES[@]}" -eq 0 ]; then
    return 1
  fi
  ci::impact::contains_value "$wanted" "${IMPACT_LANES[@]}"
}

ci::test_plan::collect_skipped_lanes() {
  local lane_id=""

  TEST_PLAN_SKIPPED_LANES=()
  while IFS='|' read -r lane_id _lane_name _lane_command _lane_blocking _lane_description; do
    lane_id="$(ci::impact::trim "${lane_id:-}")"
    [ -z "$lane_id" ] && continue
    case "$lane_id" in \#*) continue ;; esac
    if ! ci::test_plan::lane_selected "$lane_id"; then
      TEST_PLAN_SKIPPED_LANES+=("$lane_id")
    fi
  done < "$CI_TEST_PLAN_LANES_PATH"
}

ci::test_plan::write_reports() {
  local generated_at=""
  local lane=""
  local first=1
  generated_at="$(ci::common::now_iso_utc)"
  mkdir -p ci/reports

  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "generated_at": "%s",\n' "$(ci::impact::json_escape "$generated_at")"
    printf '  "execution_policy": "advisory-plan-full-gate-remains-authoritative",\n'
    printf '  "impact_report": "ci/reports/affected-areas.json",\n'
    printf '  "risk": "%s",\n' "$(ci::impact::json_escape "$IMPACT_RISK")"
    if [ "$IMPACT_REQUIRES_FULL_GATE" = "yes" ]; then
      printf '  "requires_full_gate": true,\n'
    else
      printf '  "requires_full_gate": false,\n'
    fi
    printf '  "required_lanes": [\n'
    first=1
    if [ "${#IMPACT_LANES[@]}" -gt 0 ]; then
      for lane in "${IMPACT_LANES[@]}"; do
        if ! ci::test_plan::lookup_lane "$lane"; then
          echo "Lane selected by rules but missing from lane config: $lane" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        if [ "$first" -eq 0 ]; then
          printf ',\n'
        fi
        first=0
        printf '    {"id": "%s", "name": "%s", "command": "%s", "blocking": %s, "description": "%s"}' \
          "$(ci::impact::json_escape "$LANE_ID")" \
          "$(ci::impact::json_escape "$LANE_NAME")" \
          "$(ci::impact::json_escape "$LANE_COMMAND")" \
          "$([ "$LANE_BLOCKING" = "yes" ] && echo true || echo false)" \
          "$(ci::impact::json_escape "$LANE_DESCRIPTION")"
      done
    fi
    printf '\n  ],\n'
    printf '  "skipped_lanes": '
    if [ "${#TEST_PLAN_SKIPPED_LANES[@]}" -gt 0 ]; then
      ci::impact::json_string_array "${TEST_PLAN_SKIPPED_LANES[@]}"
    else
      ci::impact::json_string_array
    fi
    printf '\n'
    printf '}\n'
  } > "$CI_TEST_PLAN_REPORT_JSON"

  {
    printf '# Smart Test Plan\n\n'
    printf -- '- generated: %s\n' "$generated_at"
    printf -- '- execution policy: advisory plan; full preflight remains authoritative\n'
    printf -- '- impact report: ci/reports/affected-areas.md\n'
    printf -- '- risk: %s\n' "$IMPACT_RISK"
    printf -- '- requires full gate: %s\n\n' "$IMPACT_REQUIRES_FULL_GATE"

    printf '## Required Checks\n'
    if [ "${#IMPACT_LANES[@]}" -eq 0 ]; then
      # shellcheck disable=SC2016
      printf -- '- No lanes selected. This is an infra problem; rerun `make verify`.\n'
    else
      printf '| Lane | Command | Blocking | Why |\n'
      printf '|---|---|---|---|\n'
      for lane in "${IMPACT_LANES[@]}"; do
        if ! ci::test_plan::lookup_lane "$lane"; then
          echo "Lane selected by rules but missing from lane config: $lane" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        # shellcheck disable=SC2016
        printf '| `%s` | `%s` | %s | %s |\n' \
          "$LANE_ID" \
          "$LANE_COMMAND" \
          "$LANE_BLOCKING" \
          "$LANE_DESCRIPTION"
      done
    fi

    printf '\n## Skipped Checks\n'
    if [ "${#TEST_PLAN_SKIPPED_LANES[@]}" -eq 0 ]; then
      printf -- '- None.\n'
    else
      for lane in "${TEST_PLAN_SKIPPED_LANES[@]}"; do
        # shellcheck disable=SC2016
        printf -- '- `%s`: no changed path rule selected this lane.\n' "$lane"
      done
    fi

    printf '\n## Safety Note\n'
    # shellcheck disable=SC2016
    printf 'This plan is advisory. `make smart` still runs `./ci/preflight.sh --mode full` after generating the plan, and `make verify`, `make ship`, and pre-push remain full-gate flows.\n'
  } > "$CI_TEST_PLAN_REPORT_MD"
}

ci::test_plan::print_summary() {
  ci::common::section "Smart test plan"
  echo "Required lanes: ${IMPACT_LANES[*]-}"
  echo "Skipped lanes: ${TEST_PLAN_SKIPPED_LANES[*]:-(none)}"
  echo "Execution policy: advisory plan; full preflight remains authoritative."
  echo "Report: $CI_TEST_PLAN_REPORT_MD"
  echo "JSON: $CI_TEST_PLAN_REPORT_JSON"
}

ci::test_plan::generate() {
  local parse_rc=0
  local impact_rc=0
  CI_TEST_PLAN_HELP_REQUESTED=0
  ci::test_plan::parse_args "$@"
  parse_rc=$?
  if [ "$parse_rc" -ne 0 ]; then
    return "$parse_rc"
  fi
  if [ "$CI_TEST_PLAN_HELP_REQUESTED" -eq 1 ]; then
    return "$CI_RESULT_PASS"
  fi

  if [ ! -f "$CI_TEST_PLAN_LANES_PATH" ]; then
    echo "Missing lane config file: $CI_TEST_PLAN_LANES_PATH" >&2
    return "$CI_RESULT_FAIL_INFRA"
  fi

  if [ "${#TEST_PLAN_IMPACT_ARGS[@]}" -gt 0 ]; then
    CI_IMPACT_QUIET=1 ci::impact::generate "${TEST_PLAN_IMPACT_ARGS[@]}"
    impact_rc=$?
  else
    CI_IMPACT_QUIET=1 ci::impact::generate
    impact_rc=$?
  fi
  if [ "$impact_rc" -ne 0 ]; then
    return "$CI_RESULT_FAIL_INFRA"
  fi
  local step_rc=0
  ci::test_plan::collect_skipped_lanes
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
  ci::test_plan::write_reports
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
  ci::test_plan::print_summary
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
}
