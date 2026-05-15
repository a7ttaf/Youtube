#!/usr/bin/env bash
# ci/lib/yaml.sh – Minimal YAML parser for flat schemas (no anchors, no block scalars).
# Supports: flat key=value, one-level nested maps, dash-prefixed lists.
# Bash 3.2+ compatible. Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# ci::yaml::_strip_comment <line> – remove trailing # comments (respects quotes)
ci::yaml::_strip_comment() {
  local line="$1"
  # Remove inline comments: find # not inside quotes
  # Simple approach: strip from first unquoted #
  local out="" in_single=0 in_double=0 i ch prev_ch=""
  local len=${#line}
  i=0
  while [ "$i" -lt "$len" ]; do
    ch="${line:$i:1}"
    if [ "$in_single" = "1" ]; then
      out="${out}${ch}"
      [ "$ch" = "'" ] && in_single=0
    elif [ "$in_double" = "1" ]; then
      out="${out}${ch}"
      [ "$ch" = '"' ] && [ "$prev_ch" != "\\" ] && in_double=0
    else
      case "$ch" in
        "'") in_single=1; out="${out}${ch}" ;;
        '"') in_double=1; out="${out}${ch}" ;;
        '#') break ;;
        *)   out="${out}${ch}" ;;
      esac
    fi
    prev_ch="$ch"
    i=$((i + 1))
  done
  printf '%s' "$out"
}

# ci::yaml::_unquote <value> – strip surrounding quotes and unescape
ci::yaml::_unquote() {
  local val="$1"
  # Trim leading/trailing whitespace
  val="${val#"${val%%[! ]*}"}"
  val="${val%"${val##*[! ]}"}"
  case "$val" in
    \"*\") val="${val#\"}" ; val="${val%\"}" ;;
    \'*\') val="${val#\'}" ; val="${val%\'}" ;;
  esac
  printf '%s' "$val"
}

# ci::yaml::_normalize_bool <value> – convert YAML booleans to 1/0
ci::yaml::_normalize_bool() {
  local val="$1"
  case "$val" in
    true|True|TRUE|yes|Yes|YES|on|On|ON)   printf '1' ;;
    false|False|FALSE|no|No|NO|off|Off|OFF) printf '0' ;;
    *) printf '%s' "$val" ;;
  esac
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ci::yaml::get <file> <key> – get a top-level key value
ci::yaml::get() {
  local file="$1"
  local key="$2"
  [ -f "$file" ] || { printf ''; return 1; }

  local line stripped raw_val _raw_key
  while IFS= read -r line; do
    # Skip blank lines and comment-only lines
    stripped="$(ci::yaml::_strip_comment "$line")"
    stripped="${stripped#"${stripped%%[! ]*}"}"
    [ -z "$stripped" ] && continue

    # Only match top-level keys (no leading whitespace)
    case "$line" in
      ' '*|$'\t'*) continue ;;
    esac

    # Match "key: value" or "key:"
    case "$stripped" in
      "${key}:"*)
        raw_val="${stripped#"${key}:"}"
        raw_val="$(ci::yaml::_unquote "$raw_val")"
        ci::yaml::_normalize_bool "$raw_val"
        return 0
        ;;
    esac
  done < "$file"
  printf ''
  return 1
}

# ci::yaml::get_nested <file> <key1> <key2> – get one-level nested value
# Handles:
#   key1:
#     key2: value
ci::yaml::get_nested() {
  local file="$1"
  local key1="$2"
  local key2="$3"
  [ -f "$file" ] || { printf ''; return 1; }

  local line stripped in_section=0 raw_val
  while IFS= read -r line; do
    stripped="$(ci::yaml::_strip_comment "$line")"

    # Detect top-level key
    case "$line" in
      ' '*|$'\t'*) : ;;
      *)
        stripped_tl="${stripped#"${stripped%%[! ]*}"}"
        if [ -z "$stripped_tl" ]; then
          continue
        fi
        case "$stripped_tl" in
          "${key1}:"*) in_section=1; continue ;;
          *:*)         in_section=0 ;;
        esac
        ;;
    esac

    [ "$in_section" = "0" ] && continue

    # Inside section: look for indented key2
    case "$line" in
      ' '*|$'\t'*)
        local inner
        inner="${stripped#"${stripped%%[! ]*}"}"
        case "$inner" in
          "${key2}:"*)
            raw_val="${inner#"${key2}:"}"
            raw_val="$(ci::yaml::_unquote "$raw_val")"
            ci::yaml::_normalize_bool "$raw_val"
            return 0
            ;;
        esac
        ;;
    esac
  done < "$file"
  printf ''
  return 1
}

# ci::yaml::list_values <file> <key> – get list items under a key
# Handles:
#   key:
#     - item1
#     - item2
ci::yaml::list_values() {
  local file="$1"
  local key="$2"
  [ -f "$file" ] || return 1

  local line stripped in_section=0 item
  while IFS= read -r line; do
    stripped="$(ci::yaml::_strip_comment "$line")"

    # Detect top-level key
    case "$line" in
      ' '*|$'\t'*) : ;;
      *)
        local stripped_tl
        stripped_tl="${stripped#"${stripped%%[! ]*}"}"
        [ -z "$stripped_tl" ] && continue
        case "$stripped_tl" in
          "${key}:"*) in_section=1; continue ;;
          *:*)        in_section=0 ;;
          *)          in_section=0 ;;
        esac
        ;;
    esac

    [ "$in_section" = "0" ] && continue

    # Inside section: match "  - value"
    case "$line" in
      ' '*|$'\t'*)
        local inner
        inner="${stripped#"${stripped%%[! ]*}"}"
        case "$inner" in
          '- '*)
            item="${inner#'- '}"
            item="$(ci::yaml::_unquote "$item")"
            printf '%s\n' "$item"
            ;;
        esac
        ;;
    esac
  done < "$file"
  return 0
}
