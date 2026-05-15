#!/usr/bin/env bash
# ci/lib/cache.sh – Content-addressed cache layer for CI check results.
# Bash 3.2+ compatible. Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
CI_GATE_CACHE_DIR="${CI_GATE_CACHE_DIR:-${HOME}/.cache/ci-gate}"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# ci::cache::_sha256  – compute sha256 of a string
ci::cache::_sha256() {
  local input="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$input" | sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$input" | shasum -a 256 | cut -d' ' -f1
  else
    # Fallback: use cksum (not cryptographic but better than nothing)
    printf '%s' "$input" | cksum | cut -d' ' -f1
  fi
}

# ci::cache::_sha256_file  – compute sha256 of a file
ci::cache::_sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | cut -d' ' -f1
  else
    cksum "$file" | cut -d' ' -f1
  fi
}

# ci::cache::_entry_dir  – return path to a cache entry directory
ci::cache::_entry_dir() {
  printf '%s/%s' "$CI_GATE_CACHE_DIR" "$1"
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ci::cache::init – create cache root dir
ci::cache::init() {
  # Expand a leading ~ to $HOME (Bash does not expand ~ inside env-var values).
  # shellcheck disable=SC2088
  case "$CI_GATE_CACHE_DIR" in
    '~/'*) CI_GATE_CACHE_DIR="${HOME}/${CI_GATE_CACHE_DIR#~/}" ;;
    '~')   CI_GATE_CACHE_DIR="$HOME" ;;
  esac
  # Honor CI_GATE_NO_CACHE: skip all cache I/O.
  if [ "${CI_GATE_NO_CACHE:-0}" = "1" ]; then
    return 0
  fi
  mkdir -p "$CI_GATE_CACHE_DIR"
}

# ci::cache::key
# Echoes a stable 64-char hex cache key.
ci::cache::key() {
  local check_name="$1"
  local tool_version="$2"
  local files_hash="$3"
  local config_hash="$4"
  ci::cache::_sha256 "${check_name}:${tool_version}:${files_hash}:${config_hash}"
}

# ci::cache::hit  – returns 0 on cache hit, 1 on miss
ci::cache::hit() {
  [ "${CI_GATE_NO_CACHE:-0}" = "1" ] && return 1
  local key="$1"
  local entry_dir
  entry_dir="$(ci::cache::_entry_dir "$key")"
  [ -f "${entry_dir}/result" ] && [ -f "${entry_dir}/meta.json" ]
}

# ci::cache::get  – restore cached result to dest_dir
# Returns 0 on success, 1 if cache miss.
ci::cache::get() {
  [ "${CI_GATE_NO_CACHE:-0}" = "1" ] && return 1
  local key="$1"
  local dest_dir="$2"
  local entry_dir
  entry_dir="$(ci::cache::_entry_dir "$key")"

  if ! ci::cache::hit "$key"; then
    return 1
  fi

  mkdir -p "$dest_dir"
  cp -r "${entry_dir}/." "$dest_dir/"
  # Update access time for GC tracking
  touch "${entry_dir}/meta.json"
  return 0
}

# ci::cache::put  – store result in cache
ci::cache::put() {
  [ "${CI_GATE_NO_CACHE:-0}" = "1" ] && return 0
  local key="$1"
  local src_dir="$2"
  local entry_dir
  entry_dir="$(ci::cache::_entry_dir "$key")"

  mkdir -p "$entry_dir"
  cp -r "${src_dir}/." "${entry_dir}/"
  # Record store time (used by GC)
  touch "${entry_dir}/meta.json"
  return 0
}

# ci::cache::invalidate  – remove a single cache entry
ci::cache::invalidate() {
  local key="$1"
  local entry_dir
  entry_dir="$(ci::cache::_entry_dir "$key")"
  if [ -d "$entry_dir" ]; then
    rm -rf "$entry_dir"
  fi
}

# ci::cache::gc – remove entries older than max_age_days (from cache.yml or 7 days)
ci::cache::gc() {
  local cache_yml=""
  local lib_dir root_dir
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root_dir="$(cd "$lib_dir/../.." && pwd)"
  cache_yml="${root_dir}/ci/config/cache.yml"

  local max_age_days=7
  if [ -f "$cache_yml" ] && command -v grep >/dev/null 2>&1; then
    local val
    val="$(grep -E '^[[:space:]]*max_age_days[[:space:]]*:' "$cache_yml" 2>/dev/null | head -1 | sed 's/.*:[[:space:]]*//')" || true
    if [ -n "$val" ] && printf '%s' "$val" | grep -qE '^[0-9]+$'; then
      max_age_days="$val"
    fi
  fi

  [ -d "$CI_GATE_CACHE_DIR" ] || return 0

  # Find entries whose meta.json is older than max_age_days
  local entry stale_list
  # Use find with -mtime; +N means strictly older than N*24h
  stale_list="$(find "$CI_GATE_CACHE_DIR" -maxdepth 2 -name "meta.json" -mtime +"${max_age_days}" 2>/dev/null)"
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    local entry_dir
    entry_dir="$(dirname "$entry")"
    rm -rf "$entry_dir"
  done <<< "$stale_list"
}

# ci::cache::hash_files [file1 file2 ...] – sha256 of concatenated file contents
ci::cache::hash_files() {
  if [ "$#" -eq 0 ]; then
    ci::cache::_sha256 ""
    return 0
  fi

  local combined=""
  local f hash
  for f in "$@"; do
    if [ -f "$f" ]; then
      hash="$(ci::cache::_sha256_file "$f")"
      combined="${combined}${hash}"
    fi
  done
  ci::cache::_sha256 "$combined"
}

# ci::cache::tool_version  – echo tool version string for cache keys
ci::cache::tool_version() {
  local tool="$1"
  local version=""
  set +e
  case "$tool" in
    node|nodejs)   version="$(node --version 2>/dev/null)" ;;
    python|python3) version="$(python3 --version 2>/dev/null)" ;;
    go)             version="$(go version 2>/dev/null | awk '{print $3}')" ;;
    rust|rustc)     version="$(rustc --version 2>/dev/null)" ;;
    shellcheck)     version="$(shellcheck --version 2>/dev/null | grep 'version:' | awk '{print $2}')" ;;
    eslint)         version="$(eslint --version 2>/dev/null)" ;;
    pylint)         version="$(pylint --version 2>/dev/null | head -1)" ;;
    ruff)           version="$(ruff --version 2>/dev/null)" ;;
    *)              version="$(if command -v "$tool" >/dev/null 2>&1; then "$tool" --version 2>/dev/null | head -1; fi || true)" ;;
  esac
  set -e
  printf '%s' "${version:-unknown}"
}
