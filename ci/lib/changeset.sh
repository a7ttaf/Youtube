#!/usr/bin/env bash
# ci/lib/changeset.sh – File change detection and classification engine.
# Bash 3.2+ compatible. Source this file; do not execute directly.
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Constants (may be overridden before sourcing)
# ---------------------------------------------------------------------------
CI_CHANGESET_JSON="${CI_CHANGESET_JSON:-ci/artifacts/changeset.json}"
CI_GATEIGNORE="${CI_GATEIGNORE:-.ci-gateignore}"

# Internal state (populated by ci::changeset::detect)
_CI_CHANGESET_MODE=""
_CI_CHANGESET_FILES_RAW=""   # newline-separated "STATUS\tPATH" entries
_CI_CHANGESET_LANGUAGES=""   # space-separated unique languages
_CI_CHANGESET_CHECKS=""      # space-separated unique checks

# Always-included checks (regardless of file types)
_CI_CHANGESET_ALWAYS_CHECKS="secrets commit-hygiene"

# ---------------------------------------------------------------------------
# Auto-ignore patterns (prefix match)
# ---------------------------------------------------------------------------
_CI_CHANGESET_AUTO_IGNORE="node_modules/ .venv/ vendor/ dist/ build/ .git/"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# ci::changeset::_default_branch – best-effort default branch detection
ci::changeset::_default_branch() {
  local branch
  # Try remote HEAD pointer
  branch="$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')" || true
  if [ -n "$branch" ]; then
    printf '%s' "$branch"
    return 0
  fi
  # Fallback: check existence of common branch names
  for b in main master trunk develop; do
    if git show-ref --verify --quiet "refs/heads/$b" 2>/dev/null || \
       git show-ref --verify --quiet "refs/remotes/origin/$b" 2>/dev/null; then
      printf '%s' "$b"
      return 0
    fi
  done
  printf 'main'
}

# ci::changeset::_sniff_shebang <path> – detect language from shebang line
ci::changeset::_sniff_shebang() {
  trap - ERR
  local path="$1"
  [ -f "$path" ] || return 1
  local first_line
  first_line="$(head -n1 "$path" 2>/dev/null)" || return 1
  case "$first_line" in
    '#!/usr/bin/env python'*|'#!/usr/bin/python'*) printf 'python'; return 0 ;;
    '#!/usr/bin/env node'*|'#!/usr/bin/node'*)     printf 'javascript'; return 0 ;;
    '#!/usr/bin/env ruby'*|'#!/usr/bin/ruby'*)     printf 'ruby'; return 0 ;;
    '#!/usr/bin/env bash'*|'#!/bin/bash'*|'#!/usr/bin/env sh'*|'#!/bin/sh'*) printf 'shell'; return 0 ;;
    '#!/usr/bin/env perl'*|'#!/usr/bin/perl'*)     printf 'perl'; return 0 ;;
  esac
  return 1
}

# ci::changeset::_sniff_content <path> – last-resort content-based detection
ci::changeset::_sniff_content() {
  trap - ERR
  local path="$1"
  [ -f "$path" ] || return 1
  local head
  head="$(head -c 512 "$path" 2>/dev/null)" || return 1
  case "$head" in
    *'<?php'*) printf 'php'; return 0 ;;
    *'package main'*) printf 'go'; return 0 ;;
    *'import React'*|*'require('\''react'\'')'*) printf 'javascript'; return 0 ;;
    *'def '*'(self'*) printf 'python'; return 0 ;;
  esac
  return 1
}

# ci::changeset::_checks_for_language <lang> – echo space-separated checks
ci::changeset::_checks_for_language() {
  local lang="$1"
  case "$lang" in
    shell)      printf 'lint-shell format-shell' ;;
    javascript) printf 'lint-js typecheck-js format-js tests-js' ;;
    python)     printf 'lint-python typecheck-python format-python tests-python' ;;
    go)         printf 'lint-go typecheck-go format-go tests-go' ;;
    rust)       printf 'lint-rust typecheck-rust format-rust tests-rust' ;;
    ruby)       printf 'lint-ruby format-ruby tests-ruby' ;;
    java|kotlin) printf 'lint-java typecheck-java format-java tests-java' ;;
    csharp)     printf 'lint-csharp typecheck-csharp format-csharp tests-csharp' ;;
    php)        printf 'lint-php typecheck-php format-php tests-php' ;;
    dockerfile) printf 'lint-docker' ;;
    yaml)       printf 'lint-yaml' ;;
    markdown)   printf 'lint-markdown' ;;
    terraform)  printf 'lint-iac' ;;
    *)          printf '' ;;
  esac
}

# ci::changeset::_add_unique <current_list> <items...> – add items not already present
ci::changeset::_add_unique() {
  local current="$1"
  shift
  local item word found
  for item in "$@"; do
    found=0
    for word in $current; do
      [ "$word" = "$item" ] && found=1 && break
    done
    if [ "$found" = "0" ]; then
      if [ -n "$current" ]; then
        current="$current $item"
      else
        current="$item"
      fi
    fi
  done
  printf '%s' "$current"
}

# ci::changeset::_checks_triggered_for_lang <lang> – echo space-sep check list
ci::changeset::_checks_triggered_for_lang() {
  local lang="$1"
  local checks
  checks="$(_CI_CHANGESET_ALWAYS_CHECKS="" ci::changeset::_checks_for_language "$lang")"
  if [ -n "$checks" ]; then
    printf '%s' "$checks"
  fi
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# ci::changeset::classify_file <path> – echo language string
ci::changeset::classify_file() {
  local path="$1"
  local base ext
  base="$(basename "$path")"
  # Extension-based matching (portable: no ${var,,})
  ext="${path##*.}"
  # Portable lowercase
  ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"

  # Special basenames first
  case "$base" in
    Dockerfile|Dockerfile.*|*.dockerfile) printf 'dockerfile'; return 0 ;;
    Makefile|GNUmakefile|makefile)        printf 'make'; return 0 ;;
    Jenkinsfile)                          printf 'groovy'; return 0 ;;
  esac

  case "$ext" in
    js|ts|tsx|jsx|mjs|cjs) printf 'javascript' ;;
    py|pyi)                 printf 'python' ;;
    go)                     printf 'go' ;;
    rs)                     printf 'rust' ;;
    rb)                     printf 'ruby' ;;
    java|kt|kts)            printf 'java' ;;
    cs)                     printf 'csharp' ;;
    php)                    printf 'php' ;;
    sh|bash|zsh|ksh)        printf 'shell' ;;
    yaml|yml)               printf 'yaml' ;;
    json|jsonc)             printf 'json' ;;
    toml)                   printf 'toml' ;;
    md|mdx|markdown)        printf 'markdown' ;;
    tf|tfvars)              printf 'terraform' ;;
    sql)                    printf 'sql' ;;
    proto)                  printf 'proto' ;;
    c|h)                    printf 'c' ;;
    cpp|cc|cxx|hpp|hxx)     printf 'cpp' ;;
    swift)                  printf 'swift' ;;
    *)
      # Try shebang sniff, then content sniff
      local lang
      if lang="$(ci::changeset::_sniff_shebang "$path" 2>/dev/null)"; then
        printf '%s' "$lang"
      elif lang="$(ci::changeset::_sniff_content "$path" 2>/dev/null)"; then
        printf '%s' "$lang"
      else
        printf 'unknown'
      fi
      ;;
  esac
  return 0
}

# ci::changeset::should_ignore <path> – returns 0 to ignore, 1 to include
ci::changeset::should_ignore() {
  local path="$1"

  # Auto-ignore prefixes
  local prefix
  for prefix in $_CI_CHANGESET_AUTO_IGNORE; do
    case "$path" in
      "$prefix"*) return 0 ;;
    esac
  done

  # .ci-gateignore patterns
  if [ -f "$CI_GATEIGNORE" ]; then
    local pattern
    while IFS= read -r pattern; do
      # Skip blank lines and comments
      case "$pattern" in
        ''|'#'*) continue ;;
      esac
      # Negation patterns not supported – skip
      case "$pattern" in
        '!'*) continue ;;
      esac
      # Simple glob match using case
      # shellcheck disable=SC2254
      case "$path" in
        $pattern) return 0 ;;
      esac
      # Also match if pattern ends without / and path contains it as prefix
      case "$pattern" in
        */) case "$path" in "$pattern"*) return 0 ;; esac ;;
      esac
    done < "$CI_GATEIGNORE"
  fi

  # path-rules.conf patterns (if present)
  if [ -f "ci/config/path-rules.conf" ]; then
    local pr_pattern
    while IFS='|' read -r pr_pattern _pr_area _pr_risk _pr_lanes _pr_full _pr_reason; do
      pr_pattern="$(echo "$pr_pattern" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -z "$pr_pattern" ] && continue
      case "$pr_pattern" in '#') continue ;; esac
      # shellcheck disable=SC2254
      case "$path" in
        $pr_pattern) return 0 ;;
      esac
    done < "ci/config/path-rules.conf"
  fi

  return 1
}

# ci::changeset::detect <mode> – populate internal state
# mode: pre-commit | pre-push | pr | all
ci::changeset::detect() {
  local mode="${1:-pre-commit}"
  _CI_CHANGESET_MODE="$mode"
  _CI_CHANGESET_FILES_RAW=""

  local raw_entries=""
  case "$mode" in
    pre-commit)
      raw_entries="$(git diff --cached --name-status 2>/dev/null)" || true
      ;;
    pre-push)
      local push_range
      set +e
      push_range="$(git rev-parse --abbrev-ref '@{push}' 2>/dev/null)"
      local rc=$?
      set -e
      if [ $rc -ne 0 ] || [ -z "$push_range" ]; then
        set +e
        push_range="$(git rev-parse HEAD~1 2>/dev/null)"
        rc=$?
        set -e
        [ $rc -ne 0 ] && push_range=""
      fi
      if [ -n "$push_range" ]; then
        raw_entries="$(git diff --name-status "${push_range}..HEAD" 2>/dev/null)" || true
      fi
      ;;
    pr)
      local default_branch merge_base
      default_branch="$(ci::changeset::_default_branch)"
      set +e
      merge_base="$(git merge-base HEAD "origin/${default_branch}" 2>/dev/null)"
      local rc=$?
      set -e
      if [ $rc -ne 0 ] || [ -z "$merge_base" ]; then
        set +e
        merge_base="$(git merge-base HEAD "${default_branch}" 2>/dev/null)"
        rc=$?
        set -e
        [ $rc -ne 0 ] && merge_base=""
      fi
      if [ -n "$merge_base" ]; then
        raw_entries="$(git diff --name-status "${merge_base}..HEAD" 2>/dev/null)" || true
      fi
      ;;
    all)
      # Full tree scan – emit all tracked files as "M\tpath"
      local f
      while IFS= read -r f; do
        if [ -n "$raw_entries" ]; then
          raw_entries="${raw_entries}
M	${f}"
        else
          raw_entries="M	${f}"
        fi
      done < <(git ls-files 2>/dev/null || true)
      ;;
    *)
      printf '[changeset] Unknown mode: %s\n' "$mode" >&2
      return 1
      ;;
  esac

  _CI_CHANGESET_FILES_RAW="$raw_entries"
}

# ci::changeset::emit_json – write structured JSON to CI_CHANGESET_JSON
ci::changeset::emit_json() {
  local out_dir
  out_dir="$(dirname "$CI_CHANGESET_JSON")"
  mkdir -p "$out_dir"

  local ts generated_languages="" generated_checks=""
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  # Seed always-present checks
  generated_checks="$_CI_CHANGESET_ALWAYS_CHECKS"

  # Build the "files" JSON array
  local files_json="" first=1
  local status path lang file_checks checks_json

  if [ -n "$_CI_CHANGESET_FILES_RAW" ]; then
    while IFS=$'\t' read -r status path; do
      [ -z "$path" ] && continue
      # Rename status: R100\t... -> R
      case "$status" in R*) status="R" ;; esac

      ci::changeset::should_ignore "$path" && continue

      lang="$(ci::changeset::classify_file "$path")"
      generated_languages="$(ci::changeset::_add_unique "$generated_languages" "$lang")"

      # Compute checks triggered for this file
      file_checks="$(ci::changeset::_checks_for_language "$lang")"
      # Build checks_json array
      checks_json=""
      local ck ck_first=1
      for ck in $file_checks; do
        generated_checks="$(ci::changeset::_add_unique "$generated_checks" "$ck")"
        if [ "$ck_first" = "1" ]; then
          checks_json="\"${ck}\""
          ck_first=0
        else
          checks_json="${checks_json},\"${ck}\""
        fi
      done

      # Escape path for JSON
      local escaped_path="${path//\\/\\\\}"
      escaped_path="${escaped_path//\"/\\\"}"

      local file_entry
      file_entry="{\"path\":\"${escaped_path}\",\"language\":\"${lang}\",\"change_type\":\"${status}\",\"checks_triggered\":[${checks_json}]}"

      if [ "$first" = "1" ]; then
        files_json="$file_entry"
        first=0
      else
        files_json="${files_json},${file_entry}"
      fi
    done <<< "$_CI_CHANGESET_FILES_RAW"
  fi

  # Build languages JSON array
  local languages_json="" lfirst=1
  local lword
  for lword in $generated_languages; do
    if [ "$lfirst" = "1" ]; then
      languages_json="\"${lword}\""
      lfirst=0
    else
      languages_json="${languages_json},\"${lword}\""
    fi
  done

  # Build checks JSON array
  local checks_json_out="" cfirst=1
  local cword
  for cword in $generated_checks; do
    if [ "$cfirst" = "1" ]; then
      checks_json_out="\"${cword}\""
      cfirst=0
    else
      checks_json_out="${checks_json_out},\"${cword}\""
    fi
  done

cat > "$CI_CHANGESET_JSON" <<EOF
{
  "mode": "$_CI_CHANGESET_MODE",
  "generated_at": "$ts",
  "languages": [${languages_json}],
  "checks": [${checks_json_out}],
  "files": [${files_json}]
}
EOF
}

# ci::changeset::get_languages – echo space-separated list of detected languages
ci::changeset::get_languages() {
  printf '%s' "$_CI_CHANGESET_LANGUAGES"
}

# ci::changeset::get_checks_needed – echo space-separated list of checks to run
ci::changeset::get_checks_needed() {
  printf '%s' "$_CI_CHANGESET_CHECKS"
}
