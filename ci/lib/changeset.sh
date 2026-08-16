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
_CI_CHANGESET_FILES_RAW=""   # newline-separated "STATUS\tPATH" entries,
                             # paths %-escaped for tab, newline and % itself
_CI_CHANGESET_PATH=""        # out-parameter of ci::changeset::_decode_path
_CI_CHANGESET_ESC=""         # out-parameter of ci::changeset::_escape_non_utf8
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

# ci::changeset::_in_node_workspace <path> – true when the file sits under a
# directory anchored by a package.json.
#
# An extension list cannot cover a workspace's build inputs: frontend/.env
# supplies the VITE_ variables the bundle is compiled against, and a file under
# frontend/public/ is copied verbatim into the shipped output. Neither has an
# extension that says "JavaScript", so both fall through to `unknown`, emit no
# check ids, and skip the node lane entirely — no install, no typecheck, no
# tests, no build, for a change that alters what ships.
#
# Anchoring on package.json keeps this from over-reaching: it fires only inside
# a real Node workspace, so a repo-root .env or a Python package's data file is
# still classified by the rules above (or left unknown).
ci::changeset::_in_node_workspace() {
  local dir
  # Scheduling has to mean the same thing by "workspace" as discovery does, or
  # a lane is scheduled for a package node.sh will never look at — or, the way
  # round this was found, run against a test fixture nobody meant as a package.
  # ci::common::is_vendored_path is that shared definition.
  if type ci::common::is_vendored_path >/dev/null 2>&1 \
    && ci::common::is_vendored_path "$1"; then
    return 1
  fi
  # The index counts as well as the disk.
  #
  # A filesystem-only walk answers "is there a manifest there now", and what
  # decides whether the Node lane is scheduled for a commit is whether the
  # commit has one. Delete ws/package.json from the worktree while it is still
  # tracked, stage a change to ws/data.json, and the asset stopped being a
  # workspace input: the changeset emitted `json` alone, preflight filtered the
  # Node lane out, and the lane's own missing-worktree guards -- which exist for
  # exactly this state and would have failed it -- never got the chance to run.
  # A guard removed from the schedule by the condition it detects.
  local _git=0
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    _git=1
  fi
  # And *which* tree, which is the gate's question here as everywhere else.
  #
  # In ship mode the commit already exists, so the disk and the index are the
  # commit being built, not the one being pushed. With frontend/package.json
  # deleted or staged-for-deletion locally while HEAD still carries it, this
  # answered "not a workspace" for an outgoing frontend/public/*.svg change: the
  # changeset emitted no JS check ids, preflight filtered the node lane out, and
  # the pushed frontend asset got no install, typecheck, test or build.
  #
  # Seventh reader of this one question across ci/checks/node.sh,
  # ci/checks/test-layout.sh and here -- and the first of them in the scheduler,
  # where being wrong removes a lane rather than misreporting inside one.
  local _ref=":"
  if [ "${CI_GATE_MODE:-}" = "ship" ] && [ "$_git" -eq 1 ] \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    _ref="HEAD:"
    # HEAD alone, not HEAD plus the worktree: a manifest present only on disk
    # describes a commit this push is not sending.
    _git=2
  fi
  dir="$(dirname "$1")"
  while [ -n "$dir" ] && [ "$dir" != "." ] && [ "$dir" != "/" ]; do
    if [ "$_git" -ne 2 ] && [ -f "$dir/package.json" ]; then
      return 0
    fi
    if [ "$_git" -ne 0 ] && git cat-file -e "${_ref}${dir}/package.json" 2>/dev/null; then
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  if [ "$_git" -ne 0 ] && git cat-file -e "${_ref}package.json" 2>/dev/null; then
    return 0
  fi
  [ "$_git" -eq 2 ] && return 1
  # The walk stopped one directory short of the only one it never examined.
  # ci::common::node_workspaces treats a root package.json as the workspace ".",
  # so in a repository laid out that way every path is inside a Node workspace
  # and none of them were recognised as such: src/data.json stayed classified as
  # json alone, emitted no node checks, and an imported asset could change with
  # no install, typecheck, test or build.
  [ -f "./package.json" ]
}

# ci::changeset::_checks_for_language <lang> – echo space-separated checks
ci::changeset::_checks_for_language() {
  local lang="$1"
  case "$lang" in
    shell)      printf 'lint-shell format-shell tests-shell' ;;
    javascript) printf 'lint-js typecheck-js format-js tests-js' ;;
    python)     printf 'lint-python typecheck-python format-python tests-python' ;;
    go)         printf 'lint-go typecheck-go format-go tests-go' ;;
    rust)       printf 'lint-rust typecheck-rust format-rust tests-rust' ;;
    actions)    printf 'lint-actions' ;;
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

# JSON is defined over text and a Git path is defined over bytes, and the two do
# not always meet: `bad\xff.py` is a perfectly legal POSIX filename that no
# sequence of Unicode characters encodes. Written through unchanged it makes the
# whole of changeset.json invalid UTF-8, and `python3 -m json.tool` refuses to
# read the report -- the entire audit artifact lost to one byte in one name.
#
# Well-formed UTF-8 goes through untouched, so `café.ts` stays readable and
# stays itself. Only the bytes that cannot be text are escaped, as \u00XX --
# their Latin-1 code point, which is a lossy rendering of something that was
# never text and is marked as such by being escaped at all. The alternative,
# refusing to write the report, throws away the record of every other file over
# a name the gate has no other complaint about.
#
# Well-formed means strictly well-formed: the ranges below reject an overlong
# encoding and a lone surrogate as well as a stray byte, because a strict
# decoder -- which is what reads this file -- rejects all three.
#
# Sets _CI_CHANGESET_ESC rather than printing, so escaping a path costs no
# process; emit_json already spends one subshell per field.
ci::changeset::_escape_non_utf8() {
  # Byte-oriented deliberately: ${#s} and ${s:i:1} count *characters* in a UTF-8
  # locale, and the bytes this has to find are precisely the ones that do not
  # form characters. `local` restores the caller's locale on return.
  #
  # Read by the shell itself rather than by this code, which is what the
  # unused-variable warning cannot see.
  # shellcheck disable=SC2034
  local LC_ALL=C LANG=C LC_CTYPE=C
  local s="$1"
  # Every ordinary path, and no walk for it.
  case "${s//[$'\001'-$'\177']/}" in
    '') _CI_CHANGESET_ESC="$s"; return 0 ;;
  esac
  local n=${#s} i=0 out="" c v need lo hi j ok cv hex
  while [ "$i" -lt "$n" ]; do
    c="${s:$i:1}"
    printf -v v '%d' "'$c"
    if [ "$v" -lt 128 ]; then
      out="$out$c"
      i=$((i + 1))
      continue
    fi
    # How many continuation bytes this lead announces, and what the first of
    # them is allowed to be. C0/C1 and F5..FF announce nothing: they cannot
    # begin a sequence at all.
    need=0 lo=128 hi=191
    if   [ "$v" -ge 194 ] && [ "$v" -le 223 ]; then need=1
    elif [ "$v" -eq 224 ]; then need=2 lo=160
    elif [ "$v" -ge 225 ] && [ "$v" -le 236 ]; then need=2
    elif [ "$v" -eq 237 ]; then need=2 hi=159
    elif [ "$v" -ge 238 ] && [ "$v" -le 239 ]; then need=2
    elif [ "$v" -eq 240 ]; then need=3 lo=144
    elif [ "$v" -ge 241 ] && [ "$v" -le 243 ]; then need=3
    elif [ "$v" -eq 244 ]; then need=3 hi=143
    fi
    ok=0
    if [ "$need" -gt 0 ] && [ "$((i + need))" -lt "$n" ]; then
      ok=1
      j=1
      while [ "$j" -le "$need" ]; do
        printf -v cv '%d' "'${s:$((i + j)):1}"
        if [ "$j" -eq 1 ]; then
          if [ "$cv" -lt "$lo" ] || [ "$cv" -gt "$hi" ]; then ok=0; break; fi
        elif [ "$cv" -lt 128 ] || [ "$cv" -gt 191 ]; then
          ok=0
          break
        fi
        j=$((j + 1))
      done
    fi
    if [ "$ok" -eq 1 ]; then
      out="$out${s:$i:$((need + 1))}"
      i=$((i + need + 1))
      continue
    fi
    # One byte at a time on failure, so the walk resynchronises on the next
    # lead byte instead of swallowing whatever followed a bad one.
    printf -v hex '%02x' "$v"
    out="$out\\u00$hex"
    i=$((i + 1))
  done
  _CI_CHANGESET_ESC="$out"
}

ci::changeset::_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/\\r}"
  s="${s//$'\t'/\\t}"
  s="${s//$'\b'/\\b}"
  s="${s//$'\f'/\\f}"
  # JSON forbids every unescaped byte below 0x20, not only the five that have
  # short names. A path may legally carry any of them, and one literal control
  # byte makes the entire report unparseable -- the audit artifact lost over a
  # filename. The remainder go out as \u00XX, after the backslash escape above
  # so the escapes this introduces are not escaped again.
  local cc ch
  for cc in 01 02 03 04 05 06 07 0b 0e 0f 10 11 12 13 14 15 16 17 18 19 1a 1b 1c 1d 1e 1f; do
    # The byte comes from the argument, not the format. A format string built
    # from a variable is a format string that can carry directives, and this
    # one is a loop away from taking its value from somewhere else.
    printf -v ch '%b' "\\x${cc}"
    s="${s//"$ch"/\\u00${cc}}"
  done
  # Last, so the escapes it introduces are not escaped again by the passes
  # above -- and so it only ever sees bytes that are still the argument's own.
  ci::changeset::_escape_non_utf8 "$s"
  printf '%s' "$_CI_CHANGESET_ESC"
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
  case "$path" in
    .github/workflows/*.yml|.github/workflows/*.yaml|.github/actions/*/action.yml|.github/actions/*/action.yaml)
      printf 'actions'
      return 0
      ;;
  esac

  case "$base" in
    Dockerfile|Dockerfile.*|*.dockerfile) printf 'dockerfile'; return 0 ;;
    Makefile|GNUmakefile|makefile)        printf 'make'; return 0 ;;
    Jenkinsfile)                          printf 'groovy'; return 0 ;;
    # A package manifest or lockfile IS the JavaScript workspace. Classifying
    # package.json as plain `json` (and a lockfile as `unknown`) emits no JS
    # check ids, so a dependency bump or a changed script would skip the node
    # lane entirely — no install, tests, typecheck or build.
    package.json|package-lock.json|npm-shrinkwrap.json|pnpm-lock.yaml|yarn.lock|bun.lock|bun.lockb)
      printf 'javascript'; return 0 ;;
    # tsconfig drives tsc directly; it is TypeScript configuration, not data.
    #
    # `jsconfig.*.json` joins its tsconfig counterpart: this list is one of
    # three that answer "is this a TypeScript project", beside _ts_project_files
    # in ci/checks/node.sh and the discovery in ci/checks/typecheck.sh, and a
    # spelling that is in one and not the others is how `tsconfig.app.json` came
    # to be scheduled by this list and then found by neither lane.
    tsconfig.json|tsconfig.*.json|jsconfig.json|jsconfig.*.json)
      printf 'javascript'; return 0 ;;
  esac

  case "$ext" in
    # mts/cts are TypeScript's module-specific extensions: real source, and
    # test-layout.sh does not cover non-test files, so there is no other guard.
    js|ts|tsx|jsx|mjs|cjs|mts|cts) printf 'javascript' ;;
    # Web build inputs: the node lane is the only one that can validate these,
    # so leaving them `unknown` means a change to the entry document or the
    # stylesheet schedules nothing at all — no typecheck, no tests, no build.
    html|htm|css|scss|sass|less|vue|svelte|astro) printf 'javascript' ;;
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
      elif ci::changeset::_in_node_workspace "$path"; then
        # Extensionless or asset-typed, but inside a Node workspace: it is a
        # build input for that workspace, so the node lane must run.
        printf 'javascript'
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

  return 1
}

# ci::changeset::_entry_paths <status> <path> <extra> – echo every path an entry
# affects, one per line.
#
# A rename affects BOTH sides. `R100 frontend/src/x.ts Docs/x.md` was reduced to
# its destination before classification, yielding lint-markdown alone -- so a
# rename that removes a module the bundle imports schedules neither the tests,
# the typecheck nor the build that would surface the broken import.
ci::changeset::_entry_paths() {
  local status="$1" path="$2" extra="$3"
  case "$status" in
    R*|C*)
      if [ -n "$extra" ]; then
        printf '%s\n%s\n' "$path" "$extra"
        return 0
      fi
      ;;
  esac
  printf '%s\n' "$path"
}

# ci::changeset::_workspace_checks <path> – node-lane check ids when the file
# lives inside a Node workspace, whatever its language says.
#
# Workspace membership was consulted only in classify_file's unknown fallback,
# so a *recognised* type never reached it: frontend/src/data.json classifies as
# json, emits no node ids, and an imported JSON change ships without the tests,
# typecheck or build that would catch it. Membership is a second signal, not a
# fallback -- a file in a workspace is a build input for that workspace whether
# or not its extension is one this table knows.
ci::changeset::_workspace_checks() {
  ci::changeset::_in_node_workspace "$1" || return 0
  ci::changeset::_checks_for_language javascript
}

ci::changeset::_encode_path() {
  local p="$1"
  p="${p//%/%25}"
  p="${p//	/%09}"
  p="${p//$'\n'/%0A}"
  printf '%s' "$p"
}

# Sets _CI_CHANGESET_PATH rather than printing: a decoded path can end in a
# newline, and `$(...)` strips exactly that.
ci::changeset::_decode_path() {
  local p="$1"
  p="${p//%09/	}"
  p="${p//%0A/$'\n'}"
  _CI_CHANGESET_PATH="${p//%25/%}"
}

ci::changeset::_populate_state_from_raw() {
  local generated_languages="" generated_checks=""
  generated_checks="$_CI_CHANGESET_ALWAYS_CHECKS"

  if [ -n "$_CI_CHANGESET_FILES_RAW" ]; then
    local status path extra p lang file_checks ck
    while IFS=$'\t' read -r status path extra; do
      [ -z "$path" ] && continue

      while IFS= read -r p; do
        [ -z "$p" ] && continue
        ci::changeset::_decode_path "$p"
        p="$_CI_CHANGESET_PATH"
        ci::changeset::should_ignore "$p" && continue

        lang="$(ci::changeset::classify_file "$p")"
        generated_languages="$(ci::changeset::_add_unique "$generated_languages" "$lang")"

        file_checks="$(ci::changeset::_checks_for_language "$lang")
$(ci::changeset::_workspace_checks "$p")"
        for ck in $file_checks; do
          generated_checks="$(ci::changeset::_add_unique "$generated_checks" "$ck")"
        done
      done <<< "$(ci::changeset::_entry_paths "$status" "$path" "$extra")"
    done <<< "$_CI_CHANGESET_FILES_RAW"
  fi

  _CI_CHANGESET_LANGUAGES="$generated_languages"
  _CI_CHANGESET_CHECKS="$generated_checks"
}

# ci::changeset::_read_name_status – convert NUL-delimited `--name-status -z`
# on stdin into the internal newline/tab form.
#
# `--name-status` without -z emits git's *quoted* representation for any path
# outside plain ASCII: staging frontend/src/café.ts produced
# "frontend/src/caf\303\251.ts", which classify_file read as ending in a quote
# character rather than in .ts. Its language became unknown, the emitted checks
# were the always-list alone, and the Node tests, typecheck and build were
# filtered out of a commit that changes TypeScript. Same defect the git-safety
# scan had, on the scheduler instead of the scanner.
#
# Renames and copies carry two paths; both are kept, because a changeset that
# lists only the destination describes a different change from the one gated.
#
# Reading NUL-delimited and then writing tab- and newline-delimited records gave
# the quoting defect back in another spelling: a path is any byte sequence
# without a NUL, so `backend/foo<newline>bar.py` split into two records, both
# nonsense, and the commit's language came out `unknown` -- every Python lint,
# typecheck, format and test check dropped from a change that is Python. The
# internal format is kept, since two readers and the JSON report share it, and
# the two bytes it cannot carry are escaped on the way in and restored on the
# way out. `%` goes first so the escaping is reversible.
ci::changeset::_read_name_status() {
  local st p1 p2 out=""
  while IFS= read -r -d '' st; do
    [ -n "$st" ] || continue
    IFS= read -r -d '' p1 || break
    case "$st" in
      R*|C*)
        IFS= read -r -d '' p2 || break
        out="${out}${st}	$(ci::changeset::_encode_path "$p1")	$(ci::changeset::_encode_path "$p2")
"
        ;;
      *)
        out="${out}${st}	$(ci::changeset::_encode_path "$p1")
"
        ;;
    esac
  done
  printf '%s' "$out"
}

# ci::changeset::_name_status <git diff args...> – the same query, NUL-safe,
# with git's exit status preserved.
#
# A temp file rather than a pipe: `$(...)` cannot hold NUL bytes, and a pipe
# would discard the status that tells detection apart from an empty result.
ci::changeset::_name_status() {
  local tmp rc=0 out
  # No predictable fallback: see branch-protection.sh. A guessable path opened
  # for writing is a symlink target waiting to happen, and detection failing is
  # already a condition this reports rather than papers over.
  tmp="$(mktemp "${TMPDIR:-/tmp}/ums-changeset.XXXXXX" 2>/dev/null)" || tmp=""
  [ -n "$tmp" ] || return 1
  git "$@" --name-status -z > "$tmp" 2>/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f "$tmp"
    return "$rc"
  fi
  out="$(ci::changeset::_read_name_status < "$tmp")"
  rm -f "$tmp"
  printf '%s' "$out"
}

# ci::changeset::detect <mode> – populate internal state
# mode: pre-commit | pre-push | pr | all
ci::changeset::detect() {
  local mode="${1:-pre-commit}"
  _CI_CHANGESET_MODE="$mode"
  _CI_CHANGESET_FILES_RAW=""
  _CI_CHANGESET_LANGUAGES=""
  _CI_CHANGESET_CHECKS=""

  local raw_entries=""
  local _rc=0
  case "$mode" in
    pre-commit)
      # The status is kept rather than discarded. `|| true` made "git is
      # broken" and "nothing is staged" the same observation, and the caller
      # reads an empty changeset as "nothing relevant changed" and skips the
      # whole gate. A detection failure has to be able to say so.
      raw_entries="$(ci::changeset::_name_status diff --cached)" || _rc=$?
      [ "$_rc" -eq 0 ] || return 1
      if [ -z "$raw_entries" ]; then
        raw_entries="$(ci::changeset::_name_status diff)" || _rc=$?
        [ "$_rc" -eq 0 ] || return 1
      fi
      ;;
    pre-push)
      local push_range=""
      local committed_entries=""
      # ci::git::push_range, not a second hand-rolled resolution. git.sh's
      # comment claimed this function "resolves the same question the same
      # way"; it did not. push_range falls back to the whole of HEAD when it
      # finds no base, this fell back to nothing — and nothing became an empty
      # changeset, which the caller read as a pass. On a branch with no remote
      # and no default-branch merge base, `ci/checks/git-safety.sh` exited 20
      # on a commit carrying a token while the gate exited 0.
      if type ci::git::push_range >/dev/null 2>&1; then
        push_range="$(ci::git::push_range 2>/dev/null)" || push_range=""
      fi
      case "$push_range" in
        # Used whole, not split apart and re-joined to HEAD. The tip is not
        # always HEAD -- the pre-push hook supplies the sha of the ref actually
        # being pushed, which is the whole point of reading its stdin, and
        # rebuilding the range against HEAD would scope the changeset to
        # whichever branch the developer happens to be standing on.
        *..*) ;;
        # A bare tip means push_range found no base: every commit reachable
        # from HEAD is being pushed. There is no diff that scopes that down, so
        # the honest answer is that this changeset cannot narrow anything —
        # reported as a failure so the caller runs the full set.
        *) return 1 ;;
      esac
      # The pushed range and nothing else. The index is the commit being made,
      # not the commit being pushed, and unioning it in scheduled lanes for work
      # that is not going out: a README-only outgoing commit with a staged
      # frontend/src/wip.ts emitted lint-js, typecheck-js, format-js and
      # tests-js, so unrelated local work decided what a push had to survive.
      #
      # Nothing is lost by dropping it. The lanes the union existed to reach all
      # stand behind HEAD in ship mode themselves -- ci/checks/node.sh and
      # ci/checks/test-layout.sh read the pushed tree and were corrected to stop
      # reading the worktree beside it -- and the checks that must run whatever
      # changed are on _CI_CHANGESET_ALWAYS_CHECKS, which no filter touches.
      # The union was added in d3f1a71f, when those lanes did read the worktree.
      #
      # Same rule as the checks it schedules, one level up: which tree a run
      # vouches for chooses the sources, and it does not add to them.
      committed_entries="$(ci::changeset::_name_status diff "$push_range")" || _rc=$?
      [ "$_rc" -eq 0 ] || return 1
      raw_entries="$committed_entries"
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
        raw_entries="$(ci::changeset::_name_status diff "${merge_base}..HEAD")" || _rc=$?
        [ "$_rc" -eq 0 ] || return 1
      fi
      ;;
    all)
      # Full tree scan – emit all tracked files as "M\tpath"
      # NUL-delimited for the same reason the diff modes are: `git ls-files`
      # quotes a non-ASCII path, and a quoted path classifies as unknown.
      local f _ls_tmp
      _ls_tmp="$(mktemp "${TMPDIR:-/tmp}/ums-changeset-ls.XXXXXX" 2>/dev/null)" || _ls_tmp=""
      [ -n "$_ls_tmp" ] || return 1
      git ls-files -z > "$_ls_tmp" 2>/dev/null || _rc=$?
      if [ "$_rc" -ne 0 ]; then
        rm -f "$_ls_tmp"
        return 1
      fi
      local _enc
      while IFS= read -r -d '' f; do
        [ -n "$f" ] || continue
        _enc="$(ci::changeset::_encode_path "$f")"
        if [ -n "$raw_entries" ]; then
          raw_entries="${raw_entries}
M	${_enc}"
        else
          raw_entries="M	${_enc}"
        fi
      done < "$_ls_tmp"
      rm -f "$_ls_tmp"
      ;;
    *)
      printf '[changeset] Unknown mode: %s\n' "$mode" >&2
      return 1
      ;;
  esac

  _CI_CHANGESET_FILES_RAW="$raw_entries"
  ci::changeset::_populate_state_from_raw
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
  local status path lang file_checks checks_json entry_paths

  if [ -n "$_CI_CHANGESET_FILES_RAW" ]; then
    while IFS=$'\t' read -r status path extra; do
      [ -z "$path" ] && continue
      # Both sides of a rename, exactly as the scheduler reads them: a report
      # that lists only the destination describes a different change set from
      # the one being gated.
      entry_paths="$(ci::changeset::_entry_paths "$status" "$path" "$extra")"
      case "$status" in
        R*) status="R" ;;
        C*) status="C" ;;
      esac

      while IFS= read -r path; do
      [ -z "$path" ] && continue
      ci::changeset::_decode_path "$path"
      path="$_CI_CHANGESET_PATH"
      ci::changeset::should_ignore "$path" && continue

      lang="$(ci::changeset::classify_file "$path")"

      # Compute checks triggered for this file. Workspace membership is a second
      # signal alongside the language, in step with _populate_state_from_raw;
      # the two disagreeing is how the JSON report and the scheduler drift.
      file_checks="$(ci::changeset::_checks_for_language "$lang")
$(ci::changeset::_workspace_checks "$path")"
      # The two signals overlap for a .ts file inside a workspace; the report
      # should list each check once.
      # shellcheck disable=SC2086
      file_checks="$(printf '%s\n' $file_checks | awk 'NF && !seen[$0]++')"
      # Build checks_json array
      checks_json=""
      local ck ck_first=1
      for ck in $file_checks; do
        local escaped_ck
        escaped_ck="$(ci::changeset::_json_escape "$ck")"
        if [ "$ck_first" = "1" ]; then
          checks_json="\"${escaped_ck}\""
          ck_first=0
        else
          checks_json="${checks_json},\"${escaped_ck}\""
        fi
      done

      local escaped_path escaped_lang escaped_status
      escaped_path="$(ci::changeset::_json_escape "$path")"
      escaped_lang="$(ci::changeset::_json_escape "$lang")"
      escaped_status="$(ci::changeset::_json_escape "$status")"

      local file_entry
      file_entry="{\"path\":\"${escaped_path}\",\"language\":\"${escaped_lang}\",\"change_type\":\"${escaped_status}\",\"checks_triggered\":[${checks_json}]}"

      if [ "$first" = "1" ]; then
        files_json="$file_entry"
        first=0
      else
        files_json="${files_json},${file_entry}"
      fi
      done <<< "$entry_paths"
    done <<< "$_CI_CHANGESET_FILES_RAW"
  fi

  # Build languages JSON array
  local languages_json="" lfirst=1
  local lword
  for lword in $_CI_CHANGESET_LANGUAGES; do
    local escaped_lword
    escaped_lword="$(ci::changeset::_json_escape "$lword")"
    if [ "$lfirst" = "1" ]; then
      languages_json="\"${escaped_lword}\""
      lfirst=0
    else
      languages_json="${languages_json},\"${escaped_lword}\""
    fi
  done

  # Build checks JSON array
  local checks_json_out="" cfirst=1
  local cword
  for cword in $_CI_CHANGESET_CHECKS; do
    local escaped_cword
    escaped_cword="$(ci::changeset::_json_escape "$cword")"
    if [ "$cfirst" = "1" ]; then
      checks_json_out="\"${escaped_cword}\""
      cfirst=0
    else
      checks_json_out="${checks_json_out},\"${escaped_cword}\""
    fi
  done

  # Deliberately no assignment back into _CI_CHANGESET_LANGUAGES / _CHECKS.
  # This is the report generator, and preflight.sh calls it immediately after
  # detect: recomputing the sets here and writing them back meant the report's
  # view -- which collapsed a rename to its destination -- replaced the
  # scheduler's, so classifying both rename paths in the scheduler was undone a
  # line later. The arrays above are serialised from the scheduler's state, so
  # the report cannot describe a gate other than the one being run.

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
