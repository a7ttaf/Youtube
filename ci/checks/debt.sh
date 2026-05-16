#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: known debt ratchet"

DEBT_FILE="ci/debt/known-failures.yml"
DEBT_LOG="ci/reports/debt-current.log"
mkdir -p ci/reports
: > "$DEBT_LOG"

if [ ! -f "$DEBT_FILE" ]; then
  echo "Missing debt registry: $DEBT_FILE"
  exit "$CI_RESULT_FAIL_INFRA"
fi

if ! grep -Eq '^[[:space:]]*-[[:space:]]*id:' "$DEBT_FILE"; then
  echo "No active known debt entries."
  exit "$CI_RESULT_PASS"
fi

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

strip_quotes() {
  local s="$1"
  if [ "${#s}" -ge 2 ]; then
    local first_char="${s:0:1}"
    local last_char="${s: -1}"
    if { [ "$first_char" = "\"" ] && [ "$last_char" = "\"" ]; } || { [ "$first_char" = "'" ] && [ "$last_char" = "'" ]; }; then
      s="${s:1:${#s}-2}"
    fi
  fi
  printf '%s' "$s"
}

extract_value() {
  local line="$1"
  local raw="${line#*:}"
  raw="$(trim "$raw")"
  strip_quotes "$raw"
}

ENTRY_IDS=()
ENTRY_TYPES=()
ENTRY_COMMANDS=()
ENTRY_OWNERS=()
ENTRY_REASONS=()
ENTRY_ALLOWED_UNTIL=()
ENTRY_MUST_NOT_INCREASE=()
ENTRY_EXPECTED_COUNT=()
ENTRY_SIGNATURES=()

current_id=""
current_type=""
current_command=""
current_owner=""
current_reason=""
current_allowed_until=""
current_must_not_increase="false"
current_expected_count=""
current_signatures=""
in_signatures=0

is_valid_iso_date() {
  local iso_date="$1"
  local parsed_date=""
  if parsed_date="$(date -j -f "%Y-%m-%d" "$iso_date" "+%Y-%m-%d" 2>/dev/null)"; then
    [ "$parsed_date" = "$iso_date" ] && return 0
  fi
  if parsed_date="$(date -d "$iso_date" "+%Y-%m-%d" 2>/dev/null)"; then
    [ "$parsed_date" = "$iso_date" ] && return 0
  fi
  return 1
}

flush_entry() {
  if [ -z "$current_id" ]; then
    return 0
  fi

  if [ -z "$current_command" ]; then
    echo "Debt entry '$current_id' is missing required field: command"
    return 1
  fi
  if [ -z "$current_owner" ]; then
    echo "Debt entry '$current_id' is missing required field: owner"
    return 1
  fi
  if [ -z "$current_reason" ]; then
    echo "Debt entry '$current_id' is missing required field: reason"
    return 1
  fi
  if [ -z "$current_allowed_until" ]; then
    echo "Debt entry '$current_id' is missing required field: allowed_until"
    return 1
  fi
  if ! [[ "$current_allowed_until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "Debt entry '$current_id' has invalid allowed_until format (expected YYYY-MM-DD): $current_allowed_until"
    return 1
  fi
  if ! is_valid_iso_date "$current_allowed_until"; then
    echo "Debt entry '$current_id' has invalid allowed_until date: $current_allowed_until"
    return 1
  fi
  if [ -z "$current_signatures" ]; then
    echo "Debt entry '$current_id' must include at least one signature."
    return 1
  fi
  if [ "$current_must_not_increase" = "true" ] && [ -z "$current_expected_count" ]; then
    echo "Debt entry '$current_id' has must_not_increase=true but no expected_count."
    return 1
  fi

  ENTRY_IDS+=("$current_id")
  ENTRY_TYPES+=("$current_type")
  ENTRY_COMMANDS+=("$current_command")
  ENTRY_OWNERS+=("$current_owner")
  ENTRY_REASONS+=("$current_reason")
  ENTRY_ALLOWED_UNTIL+=("$current_allowed_until")
  ENTRY_MUST_NOT_INCREASE+=("$current_must_not_increase")
  ENTRY_EXPECTED_COUNT+=("$current_expected_count")
  ENTRY_SIGNATURES+=("$current_signatures")

  current_id=""
  current_type=""
  current_command=""
  current_owner=""
  current_reason=""
  current_allowed_until=""
  current_must_not_increase="false"
  current_expected_count=""
  current_signatures=""
  in_signatures=0
  return 0
}

# NOTE: This parser intentionally supports only a constrained YAML subset used
# by ci/debt/known-failures.yml:
# - one field per line (no block scalars, no anchors/aliases),
# - signatures as dash-prefixed list items under "signatures:",
# - values parsed through extract_value/strip_quotes for simple scalars.
# Extend trim/extract_value/signature handling if richer YAML shapes are needed.
while IFS= read -r line || [ -n "$line" ]; do
  line="$(trim "$line")"
  [ -z "$line" ] && continue
  [[ "$line" == \#* ]] && continue

  if [[ "$line" =~ ^-[[:space:]]id: ]]; then
    if ! flush_entry; then
      exit "$CI_RESULT_FAIL_INFRA"
    fi
    current_id="$(extract_value "$line")"
    in_signatures=0
    continue
  fi

  if [ -z "$current_id" ]; then
    continue
  fi

  if [[ "$line" == signatures:* ]]; then
    in_signatures=1
    continue
  fi

  if [ "$in_signatures" -eq 1 ] && [[ "$line" =~ ^-[[:space:]] ]]; then
    signature="$(strip_quotes "$(trim "${line#-}")")"
    if [ -n "$signature" ]; then
      if [ -z "$current_signatures" ]; then
        current_signatures="$signature"
      else
        current_signatures="${current_signatures}"$'\n'"$signature"
      fi
    fi
    continue
  fi

  in_signatures=0

  case "$line" in
    type:*)
      current_type="$(extract_value "$line")"
      ;;
    command:*)
      current_command="$(extract_value "$line")"
      ;;
    owner:*)
      current_owner="$(extract_value "$line")"
      ;;
    reason:*)
      current_reason="$(extract_value "$line")"
      ;;
    allowed_until:*)
      current_allowed_until="$(extract_value "$line")"
      ;;
    must_not_increase:*)
      value="$(extract_value "$line")"
      if [ "$value" = "true" ] || [ "$value" = "false" ]; then
        current_must_not_increase="$value"
      else
        echo "Debt entry '$current_id' has invalid must_not_increase value: $value"
        exit "$CI_RESULT_FAIL_INFRA"
      fi
      ;;
    expected_count:*)
      current_expected_count="$(extract_value "$line")"
      if [ -n "$current_expected_count" ] && ! [[ "$current_expected_count" =~ ^[0-9]+$ ]]; then
        echo "Debt entry '$current_id' has non-numeric expected_count: $current_expected_count"
        exit "$CI_RESULT_FAIL_INFRA"
      fi
      ;;
  esac
done < "$DEBT_FILE"

if ! flush_entry; then
  exit "$CI_RESULT_FAIL_INFRA"
fi

if [ "${#ENTRY_IDS[@]}" -eq 0 ]; then
  echo "No active known debt entries."
  exit "$CI_RESULT_PASS"
fi

today="$(date '+%Y-%m-%d')"
expired=0
for i in "${!ENTRY_ALLOWED_UNTIL[@]}"; do
  until="${ENTRY_ALLOWED_UNTIL[$i]}"
  entry_id="${ENTRY_IDS[$i]}"
  if [ "$today" \> "$until" ]; then
    echo "Known debt expiry passed: $until (entry: $entry_id)"
    expired=1
  fi
done
if [ "$expired" -eq 1 ]; then
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

fail_new_issue=0
fail_infra=0
any_known_debt_present=0
any_reduced_debt=0

echo "Running known debt commands..." | tee -a "$DEBT_LOG"

for i in "${!ENTRY_IDS[@]}"; do
  entry_id="${ENTRY_IDS[$i]}"
  entry_type="${ENTRY_TYPES[$i]}"
  entry_command="${ENTRY_COMMANDS[$i]}"
  entry_must_not_increase="${ENTRY_MUST_NOT_INCREASE[$i]}"
  entry_expected_count="${ENTRY_EXPECTED_COUNT[$i]}"
  entry_signatures="${ENTRY_SIGNATURES[$i]}"

  echo "" | tee -a "$DEBT_LOG"
  echo ">>> [$entry_id] $entry_command" | tee -a "$DEBT_LOG"

  set +e
  command_output="$(bash -c "$entry_command" 2>&1)"
  command_rc=$?
  set -e

  printf '%s\n' "$command_output" >> "$DEBT_LOG"
  echo "[rc=$command_rc]" >> "$DEBT_LOG"

  if [ "$command_rc" -eq 127 ]; then
    echo "Command not found for debt entry '$entry_id': $entry_command"
    fail_infra=1
    continue
  fi

  matched_for_entry=0
  entry_total_count=0

  # Note: grep -F -o counts all occurrences, including multiple matches on one
  # line. expected_count must track total occurrences (not unique lines) when
  # must_not_increase=true so ratchet comparisons remain deterministic.
  while IFS= read -r signature; do
    [ -z "$signature" ] && continue
    count="$(printf '%s\n' "$command_output" | grep -F -o -- "$signature" | wc -l | tr -d '[:space:]')"
    count="${count:-0}"
    echo "Signature '$signature' count=$count" | tee -a "$DEBT_LOG"
    entry_total_count=$((entry_total_count + count))

    if [ "$count" -gt 0 ]; then
      matched_for_entry=1
      any_known_debt_present=1
    fi
  done <<< "$entry_signatures"

  if [ "$entry_must_not_increase" = "true" ] && [ -n "$entry_expected_count" ] && [ "$entry_total_count" -gt "$entry_expected_count" ]; then
    echo "Debt increased for '$entry_id': total_signature_count=$entry_total_count expected_max=$entry_expected_count"
    fail_new_issue=1
  fi

  if [ "$matched_for_entry" -eq 0 ]; then
    echo "No known signatures matched for debt entry '$entry_id' (debt may be reduced or changed)." | tee -a "$DEBT_LOG"
    any_reduced_debt=1
    if [ "$command_rc" -ne 0 ]; then
      echo "Command still failed but signature set did not match for '$entry_id'. Treating as new issue."
      fail_new_issue=1
    fi
  fi

  if [ "$entry_type" = "ruff" ]; then
    allowed_codes="$(printf '%s\n' "$entry_signatures" | grep -E '^[A-Z]{1,3}[0-9]{3,4}$' || true)"
    observed_codes="$(printf '%s\n' "$command_output" | grep -Eo '(^|[^A-Za-z0-9_])[A-Z]{1,3}[0-9]{3,4}([^A-Za-z0-9_]|$)' | sed -E 's/^[^A-Za-z0-9_]*//; s/[^A-Za-z0-9_]*$//' | sort -u || true)"
    if [ -n "$observed_codes" ]; then
      while IFS= read -r code; do
        [ -z "$code" ] && continue
        if ! printf '%s\n' "$allowed_codes" | grep -Fx -- "$code" >/dev/null 2>&1; then
          echo "New Ruff code detected for '$entry_id': $code"
          fail_new_issue=1
        fi
      done <<< "$observed_codes"
    fi
  fi
done

if [ "$fail_infra" -eq 1 ]; then
  echo "Debt result: FAIL_INFRA"
  exit "$CI_RESULT_FAIL_INFRA"
fi

if [ "$fail_new_issue" -eq 1 ]; then
  echo "Debt result: FAIL_NEW_ISSUE"
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
fi

if [ "$any_known_debt_present" -eq 1 ]; then
  if [ "$any_reduced_debt" -eq 1 ]; then
    echo "Debt signatures partially reduced. Review and update debt registry."
  fi
  echo "Debt result: PASS_WITH_KNOWN_DEBT"
  exit "$CI_RESULT_PASS_WITH_KNOWN_DEBT"
fi

echo "Debt result: PASS"
exit "$CI_RESULT_PASS"
