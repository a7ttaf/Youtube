#!/usr/bin/env bash
set -Eeuo pipefail

CI_IMPACT_RULES_PATH="ci/config/path-rules.conf"
CI_IMPACT_BASE_REF=""
CI_IMPACT_QUIET="${CI_IMPACT_QUIET:-0}"
CI_IMPACT_REPORT_JSON="ci/reports/affected-areas.json"
CI_IMPACT_REPORT_MD="ci/reports/affected-areas.md"
CI_IMPACT_HELP_REQUESTED=0

IMPACT_PATHS=()
IMPACT_MATCH_PATHS=()
IMPACT_MATCH_PATTERNS=()
IMPACT_MATCH_AREAS=()
IMPACT_MATCH_RISKS=()
IMPACT_MATCH_LANES=()
IMPACT_MATCH_FULL_GATE=()
IMPACT_MATCH_REASONS=()
IMPACT_AREAS=()
IMPACT_LANES=()
IMPACT_RISK="low"
IMPACT_REQUIRES_FULL_GATE="no"

ci::impact::usage() {
  cat <<'USAGE'
Usage: ./ci/impact.sh [--base REF] [--rules PATH]

Classifies changed, staged, and untracked files, then writes:
  ci/reports/affected-areas.json
  ci/reports/affected-areas.md

Options:
  --base REF    Also include files changed between REF and HEAD.
  --rules PATH  Use a custom path-rules config file.
  --help, -h    Show this help.
USAGE
}

ci::impact::trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

ci::impact::json_escape() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

ci::impact::json_string_array() {
  local first=1
  local value=""
  printf '['
  for value in "$@"; do
    if [ "$first" -eq 0 ]; then
      printf ', '
    fi
    first=0
    printf '"%s"' "$(ci::impact::json_escape "$value")"
  done
  printf ']'
}

ci::impact::contains_value() {
  local needle="$1"
  shift
  local value=""
  for value in "$@"; do
    if [ "$value" = "$needle" ]; then
      return 0
    fi
  done
  return 1
}

ci::impact::add_area() {
  local area="$1"
  if [ "${#IMPACT_AREAS[@]}" -eq 0 ] || ! ci::impact::contains_value "$area" "${IMPACT_AREAS[@]}"; then
    IMPACT_AREAS+=("$area")
  fi
}

ci::impact::add_lane() {
  local lane="$1"
  if [ "${#IMPACT_LANES[@]}" -eq 0 ] || ! ci::impact::contains_value "$lane" "${IMPACT_LANES[@]}"; then
    IMPACT_LANES+=("$lane")
  fi
}

ci::impact::risk_rank() {
  case "$1" in
    low) echo 1 ;;
    medium) echo 2 ;;
    high) echo 3 ;;
    *) echo 0 ;;
  esac
}

ci::impact::merge_risk() {
  local next="$1"
  local current_rank=""
  local next_rank=""
  current_rank="$(ci::impact::risk_rank "$IMPACT_RISK")"
  next_rank="$(ci::impact::risk_rank "$next")"
  if [ "$next_rank" -gt "$current_rank" ]; then
    IMPACT_RISK="$next"
  fi
}

ci::impact::parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --base)
        if [ "$#" -lt 2 ]; then
          echo "Missing value for --base" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        CI_IMPACT_BASE_REF="$2"
        shift 2
        ;;
      --rules)
        if [ "$#" -lt 2 ]; then
          echo "Missing value for --rules" >&2
          return "$CI_RESULT_FAIL_INFRA"
        fi
        CI_IMPACT_RULES_PATH="$2"
        shift 2
        ;;
      --help|-h)
        ci::impact::usage
        CI_IMPACT_HELP_REQUESTED=1
        return "$CI_RESULT_PASS"
        ;;
      *)
        echo "Unknown impact argument: $1" >&2
        return "$CI_RESULT_FAIL_INFRA"
        ;;
    esac
  done
}

ci::impact::reset() {
  IMPACT_PATHS=()
  IMPACT_MATCH_PATHS=()
  IMPACT_MATCH_PATTERNS=()
  IMPACT_MATCH_AREAS=()
  IMPACT_MATCH_RISKS=()
  IMPACT_MATCH_LANES=()
  IMPACT_MATCH_FULL_GATE=()
  IMPACT_MATCH_REASONS=()
  IMPACT_AREAS=()
  IMPACT_LANES=()
  IMPACT_RISK="low"
  IMPACT_REQUIRES_FULL_GATE="no"
  CI_IMPACT_HELP_REQUESTED=0
}

ci::impact::collect_paths() {
  if [ -n "$CI_IMPACT_BASE_REF" ]; then
    if ! git rev-parse --verify "${CI_IMPACT_BASE_REF}^{commit}" >/dev/null 2>&1; then
      echo "Base ref not found or not a commit: $CI_IMPACT_BASE_REF" >&2
      return "$CI_RESULT_FAIL_INFRA"
    fi
  fi

  while IFS= read -r path; do
    [ -z "$path" ] && continue
    IMPACT_PATHS+=("$path")
  done < <({
    if [ -n "$CI_IMPACT_BASE_REF" ]; then
      git diff --name-only "${CI_IMPACT_BASE_REF}...HEAD"
    fi
    git diff --name-only
    git diff --cached --name-only
    git ls-files --others --exclude-standard
  } | awk 'NF && !seen[$0]++')
}

ci::impact::match_path() {
  local path="$1"
  local pattern=""
  local area=""
  local risk=""
  local lanes=""
  local full_gate=""
  local reason=""
  local old_ifs=""
  local lane=""

  while IFS='|' read -r pattern area risk lanes full_gate reason; do
    pattern="$(ci::impact::trim "${pattern:-}")"
    [ -z "$pattern" ] && continue
    case "$pattern" in \#*) continue ;; esac

    area="$(ci::impact::trim "${area:-}")"
    risk="$(ci::impact::trim "${risk:-}")"
    lanes="$(ci::impact::trim "${lanes:-}")"
    full_gate="$(ci::impact::trim "${full_gate:-}")"
    reason="$(ci::impact::trim "${reason:-}")"

    if [ -z "$area" ] || [ -z "$risk" ] || [ -z "$lanes" ] || [ -z "$full_gate" ]; then
      echo "Invalid path rule: $pattern" >&2
      return "$CI_RESULT_FAIL_INFRA"
    fi

    # shellcheck disable=SC2254
    case "$path" in
      $pattern)
        IMPACT_MATCH_PATHS+=("$path")
        IMPACT_MATCH_PATTERNS+=("$pattern")
        IMPACT_MATCH_AREAS+=("$area")
        IMPACT_MATCH_RISKS+=("$risk")
        IMPACT_MATCH_LANES+=("$lanes")
        IMPACT_MATCH_FULL_GATE+=("$full_gate")
        IMPACT_MATCH_REASONS+=("$reason")
        ci::impact::add_area "$area"
        ci::impact::merge_risk "$risk"
        if [ "$full_gate" = "yes" ]; then
          IMPACT_REQUIRES_FULL_GATE="yes"
        fi

        old_ifs="$IFS"
        IFS=','
        # shellcheck disable=SC2086
        set -- $lanes
        IFS="$old_ifs"
        for lane in "$@"; do
          lane="$(ci::impact::trim "$lane")"
          [ -n "$lane" ] && ci::impact::add_lane "$lane"
        done
        return "$CI_RESULT_PASS"
        ;;
    esac
  done < "$CI_IMPACT_RULES_PATH"

  echo "No fallback path rule matched: $path" >&2
  return "$CI_RESULT_FAIL_INFRA"
}

ci::impact::classify_paths() {
  local path=""

  if [ ! -f "$CI_IMPACT_RULES_PATH" ]; then
    echo "Missing impact rules file: $CI_IMPACT_RULES_PATH" >&2
    return "$CI_RESULT_FAIL_INFRA"
  fi

  if [ "${#IMPACT_PATHS[@]}" -eq 0 ]; then
    ci::impact::add_area "repo-clean"
    ci::impact::add_lane "git-safety"
    ci::impact::add_lane "changed-files"
    IMPACT_RISK="low"
    IMPACT_REQUIRES_FULL_GATE="no"
    return "$CI_RESULT_PASS"
  fi

  local match_rc=0
  for path in "${IMPACT_PATHS[@]}"; do
    ci::impact::match_path "$path"
    match_rc=$?
    if [ "$match_rc" -ne 0 ]; then
      return "$match_rc"
    fi
  done
}

ci::impact::write_reports() {
  local generated_at=""
  local i=0
  local lanes_array=""
  local old_ifs=""
  local area=""
  local lane=""
  generated_at="$(ci::common::now_iso_utc)"

  mkdir -p ci/reports

  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "generated_at": "%s",\n' "$(ci::impact::json_escape "$generated_at")"
    if [ -n "$CI_IMPACT_BASE_REF" ]; then
      printf '  "base_ref": "%s",\n' "$(ci::impact::json_escape "$CI_IMPACT_BASE_REF")"
    else
      printf '  "base_ref": null,\n'
    fi
    printf '  "path_count": %s,\n' "${#IMPACT_PATHS[@]}"
    printf '  "risk": "%s",\n' "$(ci::impact::json_escape "$IMPACT_RISK")"
    if [ "$IMPACT_REQUIRES_FULL_GATE" = "yes" ]; then
      printf '  "requires_full_gate": true,\n'
    else
      printf '  "requires_full_gate": false,\n'
    fi
    printf '  "areas": '
    if [ "${#IMPACT_AREAS[@]}" -gt 0 ]; then
      ci::impact::json_string_array "${IMPACT_AREAS[@]}"
    else
      ci::impact::json_string_array
    fi
    printf ',\n'
    printf '  "selected_lanes": '
    if [ "${#IMPACT_LANES[@]}" -gt 0 ]; then
      ci::impact::json_string_array "${IMPACT_LANES[@]}"
    else
      ci::impact::json_string_array
    fi
    printf ',\n'
    printf '  "paths": [\n'
    if [ "${#IMPACT_MATCH_PATHS[@]}" -gt 0 ]; then
      for i in "${!IMPACT_MATCH_PATHS[@]}"; do
        lanes_array="${IMPACT_MATCH_LANES[$i]}"
        old_ifs="$IFS"
        IFS=','
        # shellcheck disable=SC2086
        set -- $lanes_array
        IFS="$old_ifs"
        if [ "$i" -gt 0 ]; then
          printf ',\n'
        fi
        printf '    {"path": "%s", "pattern": "%s", "area": "%s", "risk": "%s", ' \
          "$(ci::impact::json_escape "${IMPACT_MATCH_PATHS[$i]}")" \
          "$(ci::impact::json_escape "${IMPACT_MATCH_PATTERNS[$i]}")" \
          "$(ci::impact::json_escape "${IMPACT_MATCH_AREAS[$i]}")" \
          "$(ci::impact::json_escape "${IMPACT_MATCH_RISKS[$i]}")"
        if [ "${IMPACT_MATCH_FULL_GATE[$i]}" = "yes" ]; then
          printf '"requires_full_gate": true, '
        else
          printf '"requires_full_gate": false, '
        fi
        printf '"lanes": '
        ci::impact::json_string_array "$@"
        printf ', "reason": "%s"}' "$(ci::impact::json_escape "${IMPACT_MATCH_REASONS[$i]}")"
      done
    fi
    printf '\n  ]\n'
    printf '}\n'
  } > "$CI_IMPACT_REPORT_JSON"

  {
    printf '# Impact Analysis\n\n'
    printf -- '- generated: %s\n' "$generated_at"
    if [ -n "$CI_IMPACT_BASE_REF" ]; then
      printf -- '- base ref: %s\n' "$CI_IMPACT_BASE_REF"
    else
      printf -- '- base ref: none\n'
    fi
    printf -- '- changed/staged/untracked path count: %s\n' "${#IMPACT_PATHS[@]}"
    printf -- '- max risk: %s\n' "$IMPACT_RISK"
    printf -- '- requires full gate: %s\n\n' "$IMPACT_REQUIRES_FULL_GATE"

    printf '## Affected Areas\n'
    if [ "${#IMPACT_AREAS[@]}" -gt 0 ]; then
      for area in "${IMPACT_AREAS[@]}"; do
        printf -- '- %s\n' "$area"
      done
    else
      printf -- '- none\n'
    fi
    printf '\n## Selected Lanes\n'
    if [ "${#IMPACT_LANES[@]}" -gt 0 ]; then
      for lane in "${IMPACT_LANES[@]}"; do
        printf -- '- %s\n' "$lane"
      done
    else
      printf -- '- none\n'
    fi

    printf '\n## Matched Paths\n'
    if [ "${#IMPACT_PATHS[@]}" -eq 0 ]; then
      printf -- '- No changed, staged, or untracked files detected.\n'
    else
      printf '| Path | Area | Risk | Full gate | Lanes | Reason |\n'
      printf '|---|---|---|---|---|---|\n'
      if [ "${#IMPACT_MATCH_PATHS[@]}" -gt 0 ]; then
        for i in "${!IMPACT_MATCH_PATHS[@]}"; do
          # shellcheck disable=SC2016
          printf '| `%s` | %s | %s | %s | `%s` | %s |\n' \
            "${IMPACT_MATCH_PATHS[$i]}" \
            "${IMPACT_MATCH_AREAS[$i]}" \
            "${IMPACT_MATCH_RISKS[$i]}" \
            "${IMPACT_MATCH_FULL_GATE[$i]}" \
            "${IMPACT_MATCH_LANES[$i]}" \
            "${IMPACT_MATCH_REASONS[$i]}"
        done
      fi
    fi
  } > "$CI_IMPACT_REPORT_MD"
}

ci::impact::print_summary() {
  [ "$CI_IMPACT_QUIET" -eq 1 ] && return 0
  ci::common::section "Impact analysis"
  echo "Paths analyzed: ${#IMPACT_PATHS[@]}"
  echo "Risk: $IMPACT_RISK"
  echo "Requires full gate: $IMPACT_REQUIRES_FULL_GATE"
  echo "Areas: ${IMPACT_AREAS[*]-}"
  echo "Selected lanes: ${IMPACT_LANES[*]-}"
  echo "Report: $CI_IMPACT_REPORT_MD"
  echo "JSON: $CI_IMPACT_REPORT_JSON"
}

ci::impact::generate() {
  local step_rc=0
  ci::impact::reset
  ci::impact::parse_args "$@"
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
  if [ "$CI_IMPACT_HELP_REQUESTED" -eq 1 ]; then
    return "$CI_RESULT_PASS"
  fi
  ci::impact::collect_paths
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
  ci::impact::classify_paths
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
  ci::impact::write_reports
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
  ci::impact::print_summary
  step_rc=$?
  if [ "$step_rc" -ne 0 ]; then
    return "$step_rc"
  fi
}
