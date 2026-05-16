#!/usr/bin/env bash
# ci/lib/affected.sh - Affected/incremental test selection library.
# Maps changed source files to owning test files using ci/config/affected.yml.
# Bash 3.2+ compatible. Source this file; do not execute directly.
set -Eeuo pipefail

# shellcheck disable=SC2034
AFFECTED_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CI_AFFECTED_CONFIG="${CI_AFFECTED_CONFIG:-}"

_ci_affected_resolve_config() {
  if [ -n "$CI_AFFECTED_CONFIG" ] && [ -f "$CI_AFFECTED_CONFIG" ]; then
    printf '%s' "$CI_AFFECTED_CONFIG"
    return 0
  fi
  local lib_dir root_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root_dir="$(cd "$lib_dir/../.." && pwd)"
  printf '%s' "${root_dir}/ci/config/affected.yml"
}

# ci::affected::map_file_to_tests
# Echoes newline-separated test patterns that match this source file.
ci::affected::map_file_to_tests() {
  local source_file="$1"
  local config_file
  config_file="$(_ci_affected_resolve_config)"

  [ -f "$config_file" ] || return 0

  local in_rules=0
  local current_source=""
  local current_tests=""
  local current_also=""
  local line _key value

  while IFS= read -r line; do
    # Detect rules section
    case "$line" in
      "rules:"*) in_rules=1; continue ;;
    esac
    [ "$in_rules" -eq 0 ] && continue

    # Detect new rule entry
    case "$line" in
      *"- source:"*)
        value="${line#*- source:}"
        value="${value#"${value%%[![:space:]]*}"}" # ltrim
        value="${value#\"}"
        value="${value%\"}"
        # Flush previous rule
        if [ -n "$current_source" ]; then
          _ci_affected_emit_if_match "$source_file" "$current_source" "$current_tests" "$current_also"
        fi
        current_source="${line#*- source:}"
        current_source="${current_source#"${current_source%%[![:space:]]*}"}"
        current_source="${current_source#\"}"
        current_source="${current_source%\"}"
        current_tests=""
        current_also=""
        ;;
      *"tests:"*)
        current_tests="${line#*tests:}"
        current_tests="${current_tests#"${current_tests%%[![:space:]]*}"}"
        current_tests="${current_tests#\"}"
        current_tests="${current_tests%\"}"
        ;;
      *"also:"*)
        current_also="${line#*also:}"
        current_also="${current_also#"${current_also%%[![:space:]]*}"}"
        current_also="${current_also#\"}"
        current_also="${current_also%\"}"
        ;;
    esac
  done < "$config_file"

  # Flush last rule
  if [ -n "$current_source" ]; then
    _ci_affected_emit_if_match "$source_file" "$current_source" "$current_tests" "$current_also"
  fi
}

# _ci_affected_emit_if_match
_ci_affected_emit_if_match() {
  local file="$1"
  local src_pattern="$2"
  local tests_pattern="$3"
  local also_pattern="$4"

  # Convert glob to case pattern (basic: ** -> *, keep rest)
  local case_pattern
  case_pattern="$(printf '%s' "$src_pattern" | sed 's|\*\*|*|g')"

  # shellcheck disable=SC2254
  case "$file" in
    $case_pattern)
      [ -n "$tests_pattern" ] && printf '%s
' "$tests_pattern"
      [ -n "$also_pattern" ] && printf '%s
' "$also_pattern"
      ;;
  esac
}

# ci::affected::get_affected_tests [file1 file2 ...]
# Given a list of changed files, echoes newline-separated test patterns to run.
ci::affected::get_affected_tests() {
  local unique_patterns=""
  local f pattern found word

  for f in "$@"; do
    while IFS= read -r pattern; do
      [ -z "$pattern" ] && continue
      # Deduplicate
      found=0
      for word in $unique_patterns; do
        [ "$word" = "$pattern" ] && found=1 && break
      done
      if [ "$found" -eq 0 ]; then
        if [ -n "$unique_patterns" ]; then
          unique_patterns="${unique_patterns}
${pattern}"
        else
          unique_patterns="$pattern"
        fi
      fi
    done < <(ci::affected::map_file_to_tests "$f")
  done

  printf '%s
' "$unique_patterns"
}

# ci::affected::find_test_files [file1 file2 ...]
# Like get_affected_tests but actually expands patterns to existing files.
ci::affected::find_test_files() {
  local pattern
  while IFS= read -r pattern; do
    [ -z "$pattern" ] && continue
    # Use find-based glob expansion (portable)
    local dir_part file_part
    dir_part="$(dirname "$pattern")"
    file_part="$(basename "$pattern")"
    # Convert ** glob to find -name
    local find_name
    find_name="$(printf '%s' "$file_part" | sed 's|\*\*|*|g')"
    local search_root
    search_root="$(printf '%s' "$dir_part" | sed 's|/\*\*.*||; s|\*\*.*||')"
    search_root="${search_root:-.}"
    if [ -d "$search_root" ]; then
      find "$search_root" -name "$find_name" -type f 2>/dev/null || true
    fi
  done < <(ci::affected::get_affected_tests "$@")
}
