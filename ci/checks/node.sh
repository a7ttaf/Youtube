#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"

cd "$ROOT_DIR"

ci::common::section "Check: node lane"

# The lane body below handles exactly one workspace. When invoked normally we
# discover the workspaces and re-enter once per directory; CI_GATE_NODE_WORKSPACE
# marks that recursive call. A repo with a root package.json resolves to "."
# and behaves exactly as before.
if [ -z "${CI_GATE_NODE_WORKSPACE:-}" ]; then
  NODE_WORKSPACES="$(ci::common::node_workspaces package.json)"

  # Discovery stats the filesystem, so a workspace that exists only in the index
  # is invisible to it: stage a new added/package.json whose test script fails,
  # then remove added/ from disk, and the lane never looks at it. The index is a
  # second source of workspaces, and one that describes the commit rather than
  # the tree.
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    INDEXED_WS="$(git ls-files -- 'package.json' '*/package.json' 2>/dev/null \
      | awk -F/ 'NF == 1 { print "."; next } NF == 2 { print $1 }' | sort -u || true)"
    if [ -n "$INDEXED_WS" ]; then
      NODE_WORKSPACES="$(printf '%s\n%s\n' "$NODE_WORKSPACES" "$INDEXED_WS" \
        | sed '/^$/d' | sort -u)"
    fi
  fi

  if [ -z "$NODE_WORKSPACES" ]; then
    # "No manifest" and "no JavaScript here" are not the same statement.
    # Deleting frontend/package.json leaves the lockfile, the tsconfig and the
    # vitest config behind; discovery then finds nothing and this branch used to
    # return PASS, so a commit that makes the tracked frontend uninstallable ran
    # no install, typecheck, test or build and every mode accepted it.
    #
    # Keyed on workspace *configuration*, not on source files: a lockfile or a
    # tsconfig/vite/vitest config with no manifest beside it is unambiguous
    # evidence of a removed manifest, whereas a stray .js in a non-JS repo is
    # not, and this branch is exactly where a non-JS repo lands.
    ORPHAN_WS="$(find . \
      \( -name 'node_modules' -o -name '.git' -o -name 'dist' -o -name 'build' \) -prune -o \
      -type f \( -name 'package-lock.json' -o -name 'npm-shrinkwrap.json' \
        -o -name 'pnpm-lock.yaml' -o -name 'yarn.lock' -o -name 'bun.lock' -o -name 'bun.lockb' \
        -o -name 'tsconfig.json' -o -name 'jsconfig.json' \
        -o -name 'vitest.config.*' -o -name 'vite.config.*' \) -print 2>/dev/null | head -10 || true)"

    if [ -n "$ORPHAN_WS" ]; then
      echo "No package.json anywhere, but the tree still carries workspace configuration:"
      while IFS= read -r _orphan; do
        [ -n "$_orphan" ] || continue
        echo "    ${_orphan#./}"
      done <<< "$ORPHAN_WS"
      echo "  Nothing can be installed or run against these, so the lane would"
      echo "  pass having executed no install, typecheck, test or build."
      echo "  Restore the manifest, or remove the configuration with it."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi

    echo "No package.json found. Skipping Node lane."
    exit "$CI_RESULT_PASS"
  fi

  # Discovery reads the filesystem, so a staged deletion of package.json with
  # the file restored in the worktree looked like a healthy workspace: every
  # manifest and script check then read that worktree copy, and both pre-commit
  # and pre-push passed for a commit that carries no manifest at all.
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    MISSING_FROM_INDEX=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      if [ "$_ws" = "." ]; then _mf="package.json"; else _mf="$_ws/package.json"; fi
      if ! git cat-file -e ":$_mf" 2>/dev/null; then
        MISSING_FROM_INDEX="${MISSING_FROM_INDEX}${_mf}"$'\n'
      fi
    done <<< "$NODE_WORKSPACES"

    if [ -n "$MISSING_FROM_INDEX" ]; then
      echo "A workspace manifest exists on disk but not in the git index:"
      while IFS= read -r _mf; do
        [ -n "$_mf" ] || continue
        echo "    $_mf"
      done <<< "$MISSING_FROM_INDEX"
      echo "  The commit being made would carry no manifest for that workspace,"
      echo "  so nothing there could be installed or run -- while every check"
      echo "  below would read the worktree copy and pass."
      echo "  Stage it (git add <path>), or restore it if the deletion was"
      echo "  staged by mistake."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi

    # The converse: staged into the commit but absent from the tree. The child
    # would find no package.json there, print "Skipping Node lane" and return
    # PASS -- so a workspace the commit adds, with a failing test script, is
    # never run at all.
    MISSING_FROM_DISK=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      if [ "$_ws" = "." ]; then _mf="package.json"; else _mf="$_ws/package.json"; fi
      if git cat-file -e ":$_mf" 2>/dev/null && [ ! -f "$_mf" ]; then
        MISSING_FROM_DISK="${MISSING_FROM_DISK}${_mf}"$'\n'
      fi
    done <<< "$NODE_WORKSPACES"

    if [ -n "$MISSING_FROM_DISK" ]; then
      echo "A workspace manifest is staged but missing from the worktree:"
      while IFS= read -r _mf; do
        [ -n "$_mf" ] || continue
        echo "    $_mf"
      done <<< "$MISSING_FROM_DISK"
      echo "  The commit adds that workspace, but there is nothing on disk to"
      echo "  install or run, so the lane would skip it and pass."
      echo "  Restore the workspace, or unstage it."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi

    # Existing in the index is not enough, and the manifest is not the only file
    # that matters. Every check below -- engines, packageManager, the lockfile
    # fingerprint, and then the workspace's own test, typecheck and build
    # scripts -- consumes the worktree. Stage a failing source file, restore the
    # passing copy on disk, and the lane reports on code the commit does not
    # contain.
    #
    # The rule is *partial staging*, not any divergence: a file whose index copy
    # differs from HEAD (it is part of this commit) AND whose worktree copy
    # differs from the index. An ordinary dirty worktree -- edited, not yet
    # staged -- is left alone, because there the developer is deliberately
    # testing what is on disk and nothing claims otherwise.
    #
    # That rule is written against the index, which is the right reference for
    # exactly one gate. In ship mode the commit already exists: the index
    # matches HEAD, `git diff --cached` is empty, and the rule never fires --
    # so a workspace file broken in a commit and repaired only on disk sailed
    # through the pre-push gate on the strength of the repair. The tree a run
    # vouches for is the gate's, not the changeset's, so the reference follows
    # CI_GATE_MODE: ship stands behind HEAD, everything else behind the index.
    PARTIAL=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      _scope="$_ws"
      [ "$_scope" = "." ] && _scope="."

      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        _drift="$( {
          git diff --name-only HEAD -- "$_scope" 2>/dev/null || true
          git ls-files --others --exclude-standard -- "$_scope" 2>/dev/null || true
        } | sort -u | sed '/^$/d')"
        [ -n "$_drift" ] && PARTIAL="${PARTIAL}${_drift}"$'\n'
        continue
      fi

      _staged="$(git diff --cached --name-only -- "$_scope" 2>/dev/null | sort || true)"
      [ -n "$_staged" ] || continue
      # `git diff` compares tracked content only, so it says nothing about a
      # path staged for deletion and then recreated as an untracked file:
      # `D  app.js` plus `?? app.js` produced an empty intersection, and the
      # lane tested the recreated file for a commit that deletes it. The
      # untracked list is the other half of "what is on disk here".
      _unstaged="$( {
        git diff --name-only -- "$_scope" 2>/dev/null || true
        git ls-files --others --exclude-standard -- "$_scope" 2>/dev/null || true
      } | sort -u)"
      [ -n "$_unstaged" ] || continue
      _both="$(comm -12 <(printf '%s\n' "$_staged") <(printf '%s\n' "$_unstaged") || true)"
      [ -n "$_both" ] && PARTIAL="${PARTIAL}${_both}"$'\n'
    done <<< "$NODE_WORKSPACES"
    PARTIAL="$(printf '%s' "$PARTIAL" | sed '/^$/d')"

    if [ -n "$PARTIAL" ]; then
      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        echo "Workspace files differ between HEAD and the worktree:"
      else
        echo "Workspace files are staged but changed again in the worktree:"
      fi
      while IFS= read -r _p; do
        [ -n "$_p" ] || continue
        echo "    $_p"
      done <<< "$PARTIAL"
      echo "  The lane installs, typechecks, tests and builds the worktree, so"
      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        echo "  it would report on content the pushed commits do not contain."
        echo "  Commit the rest, stash it, or discard it."
      else
        echo "  it would report on content the commit does not contain."
        echo "  Stage the rest (git add <path>), or discard it."
      fi
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  fi

  NODE_OVERALL="$CI_RESULT_PASS"
  while IFS= read -r _ws; do
    [ -n "$_ws" ] || continue
    echo ""
    echo "--- Node lane workspace: ${_ws}"
    _ws_rc=0
    CI_GATE_NODE_WORKSPACE="$_ws" bash "$SCRIPT_DIR/node.sh" "$@" || _ws_rc=$?
    # A failing package script exits 1, which is not a gate result code and
    # would otherwise be merged in at the FAIL_INFRA level — a real test,
    # typecheck or build regression reported as broken infrastructure.
    _ws_rc="$(ci::common::normalize_result "$_ws_rc")"
    NODE_OVERALL="$(ci::common::merge_results "$NODE_OVERALL" "$_ws_rc")"
  done <<< "$NODE_WORKSPACES"

  exit "$NODE_OVERALL"
fi

cd "$ROOT_DIR/$CI_GATE_NODE_WORKSPACE"

if [ ! -f package.json ]; then
  echo "No package.json found in ${CI_GATE_NODE_WORKSPACE}. Skipping Node lane."
  exit "$CI_RESULT_PASS"
fi

# Dependency-cache fingerprints stay at the repo root so enabling a
# subdirectory workspace does not scatter .ci-gate/ dirs through the tree.
if [ "$CI_GATE_NODE_WORKSPACE" = "." ]; then
  WS_SLUG="root"
else
  WS_SLUG="$(printf '%s' "$CI_GATE_NODE_WORKSPACE" | tr '/' '-')"
fi
NODE_HASH_FILE="$ROOT_DIR/.ci-gate/node_modules-${WS_SLUG}.hash"

if ! ci::common::command_exists node; then
  echo "package.json exists but node is not installed."
  exit "$CI_RESULT_FAIL_INFRA"
fi

MANAGER=""
LOCKFILE=""
LOCKFILE_COUNT=0

if [ -f pnpm-lock.yaml ]; then
  MANAGER="pnpm"
  LOCKFILE="${LOCKFILE:-pnpm-lock.yaml}"
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f package-lock.json ] || [ -f npm-shrinkwrap.json ]; then
  MANAGER="${MANAGER:-npm}"
  if [ -z "$LOCKFILE" ]; then
    if [ -f npm-shrinkwrap.json ]; then
      LOCKFILE="npm-shrinkwrap.json"
    else
      LOCKFILE="package-lock.json"
    fi
  fi
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f yarn.lock ]; then
  MANAGER="${MANAGER:-yarn}"
  LOCKFILE="${LOCKFILE:-yarn.lock}"
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi
if [ -f bun.lockb ] || [ -f bun.lock ]; then
  MANAGER="${MANAGER:-bun}"
  if [ -z "$LOCKFILE" ]; then
    if [ -f bun.lock ]; then
      LOCKFILE="bun.lock"
    else
      LOCKFILE="bun.lockb"
    fi
  fi
  LOCKFILE_COUNT=$((LOCKFILE_COUNT + 1))
fi

if [ "$LOCKFILE_COUNT" -gt 1 ]; then
  echo "Multiple lockfiles detected. Resolve package-manager ambiguity first."
  exit "$CI_RESULT_FAIL_INFRA"
fi

if [ -z "$MANAGER" ]; then
  echo "No lockfile detected for package.json. Refusing mutable install."
  exit "$CI_RESULT_FAIL_INFRA"
fi

# ---- Declared toolchain ------------------------------------------------------
#
# The workspace manifest states the toolchain its results are reproducible
# under. Accepting whatever `node` and the package manager happen to resolve to
# means a green lane vouches for nothing: the same commit can pass here and
# fail on a conforming machine, or the reverse, and neither result is evidence.
# Both assertions read the workspace's own package.json, so a manifest that
# declares nothing is unaffected; a manifest whose range cannot be evaluated is
# reported rather than assumed to hold.

# _manifest_field <dotted.path> – echo the value, or nothing when absent.
# node is already a hard requirement above and parses its own manifest format
# on any version, so this stays correct even while the version is in question.
_manifest_field() {
  node -e '
    const fs = require("fs");
    const pkg = JSON.parse(fs.readFileSync("package.json", "utf8"));
    const val = process.argv[1]
      .split(".")
      .reduce((o, k) => (o === null || o === undefined ? o : o[k]), pkg);
    if (val !== null && val !== undefined) process.stdout.write(String(val));
  ' "$1" 2>/dev/null || true
}

# _semver_part <version> <1|2|3> – echo one numeric component, defaulting to 0.
# An absent component really is 0: `cut -d. -f2` would echo the whole string for
# a version like "23", making ">=22.12.0 <23" accept 23.1.0.
_semver_part() {
  printf '%s' "${1#v}" | awk -v i="$2" '{
    v = $0
    sub(/[-+].*$/, "", v)
    n = split(v, p, ".")
    x = (i <= n ? p[i] : "0")
    gsub(/[^0-9]/, "", x)
    print (x == "" ? "0" : x)
  }'
}

# _semver_spec_part <range-token> <1|2|3> – echo the component AS WRITTEN, or
# nothing when the token does not state it. Deliberately distinct from
# _semver_part: for a *version* an absent component is 0, but for a *range* it
# is unconstrained, so "20" must admit 20.20.2 the way npm's X-ranges do.
_semver_spec_part() {
  printf '%s' "${1#v}" | awk -v i="$2" '{
    v = $0
    sub(/[-+].*$/, "", v)
    n = split(v, p, ".")
    print (i <= n ? p[i] : "")
  }'
}

# _semver_is_version <string> – 0 when the string is a usable semver operand.
#
# The component parser strips non-numeric text and defaults to 0, which is right
# for a runtime that reports "v20.20.2" but catastrophic for a range: ">=banana"
# and a bare ">=" both parsed as >=0.0.0 and admitted every version there is,
# switching the whole toolchain boundary off. Operands are checked before they
# reach the comparator so a malformed one is reported as unverifiable instead.
# The whole grammar, not just the first character and the character set:
# ">=20banana", ">=20..1" and ">=20.1.2.3" all begin with a digit and carry only
# legal characters, and node-semver rejects every one of them. Checking the
# shape piecemeal let each of those through as >=20.0.0.
_semver_is_version() {
  printf '%s' "$1" | grep -Eq \
    '^v?([0-9]+|[xX*])(\.([0-9]+|[xX*]))?(\.([0-9]+|[xX*]))?(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
}

# _semver_upper_bound <operand> – echo the version one past everything a partial
# operand covers, or nothing when the operand states all three components.
#
# node-semver reads a partial as an X-range, so "20" spans all of major 20 and
# "20.1" all of minor 20.1. Both comparators that care about the top of that
# span were comparing against the zero-filled floor instead: "<=20" rejected
# 20.20.2, and ">20" admitted it. The bound is the same value for both.
_semver_upper_bound() {
  local stated=0 i
  for i in 1 2 3; do
    case "$(_semver_spec_part "$1" "$i")" in
      ''|'x'|'X'|'*') break ;;
    esac
    stated="$i"
  done
  [ "$stated" -eq 0 ] && return 0
  [ "$stated" -ge 3 ] && return 0
  case "$stated" in
    1) printf '%s.0.0' "$(( $(_semver_part "$1" 1) + 1 ))" ;;
    2) printf '%s.%s.0' "$(_semver_part "$1" 1)" "$(( $(_semver_part "$1" 2) + 1 ))" ;;
  esac
}

# _semver_spec_states <operand> <i> – 0 when the operand states component i as a
# number.
#
# "20.x" states a minor textually but as a wildcard, so the tilde and caret
# rules must not pin against it: ~20.x is the whole of major 20, exactly as ~20
# is. Testing for non-empty rather than numeric compared the runtime's minor
# against 0 and rejected a conforming version.
_semver_spec_states() {
  case "$(_semver_spec_part "$1" "$2")" in
    ''|*[!0-9]*) return 1 ;;
  esac
  return 0
}

# _semver_truncate_wildcard <operand> – echo the operand with every component
# from the first wildcard onwards removed; echo it unchanged when it has none.
#
# node-semver reads any X in an operand as making everything to its right an X
# too ("we know patch is an x, because we have any x at all"), so "20.*.3" is
# "20.x.x" and ">=20.*.3" is ">=20.0.0". _semver_part strips the "*" to 0 but
# keeps the stated 3, so the comparison ran against 20.0.3 and rejected 20.0.1 —
# a runtime the range admits. _semver_upper_bound already stopped at the first
# wildcard, which is why "<=20.*.3" was right and ">=20.*.3" was not; truncating
# the operand once makes every comparator read the same range.
_semver_truncate_wildcard() {
  local i part out=""
  for i in 1 2 3; do
    part="$(_semver_spec_part "$1" "$i")"
    case "$part" in
      '') break ;;
      'x'|'X'|'*')
        # A wildcard *major* states nothing at all, and the empty string is not
        # a usable operand: "=x" came back malformed, when node-semver reads it
        # as "*". Left as written, so the callers that ask whether a component
        # is stated still get an answer.
        [ "$i" -eq 1 ] && break
        printf '%s' "$out"
        return 0
        ;;
    esac
    out="${out:+$out.}$part"
  done
  printf '%s' "$1"
}

# _semver_cmp <a> <b> – echo -1, 0 or 1. Pre-release suffixes are dropped: a
# gate that accepted 22.12.0-rc for a >=22.12.0 requirement would be lying by a
# hair, but the check exists to catch whole-version drift, not tag splitting.
_semver_cmp() {
  local i av bv
  for i in 1 2 3; do
    av="$(_semver_part "$1" "$i")"
    bv="$(_semver_part "$2" "$i")"
    if [ "$av" -gt "$bv" ]; then printf '1'; return 0; fi
    if [ "$av" -lt "$bv" ]; then printf -- '-1'; return 0; fi
  done
  printf '0'
}

# _semver_token_ok <version> <token> – 0 satisfied, 1 unsatisfied,
# 2 unsupported range form, 3 invalid operand.
#
# The last two are different failures and must not share a status. An exotic but
# VALID form -- a hyphen range -- is something this comparator does not
# implement, and a sibling alternative that plainly admits the runtime should
# still win. A malformed operand is a typo in the manifest, and no sibling
# should rescue it: ">=20banana || >=20" came back satisfied while
# _semver_is_version was rejecting the first operand outright.
_semver_token_ok() {
  local version="$1" tok="$2" want i spec
  case "$tok" in
    '*'|'x'|'X'|'')
      return 0
      ;;
    '>='*) want="${tok#>=}" ;;
    '<='*) want="${tok#<=}" ;;
    '>'*)  want="${tok#>}" ;;
    '<'*)  want="${tok#<}" ;;
    '^'*)  want="${tok#^}" ;;
    '~'*)  want="${tok#\~}" ;;
    '='*)  want="${tok#=}" ;;
    *)     want="" ;;
  esac

  # An operator with a malformed operand is unverifiable, not satisfied.
  # _semver_part strips non-numeric text and defaults to 0, so ">=banana" and a
  # bare ">=" both became >=0.0.0 and admitted everything -- the boundary
  # switched off by a typo in a manifest.
  case "$tok" in
    '>='*|'<='*|'>'*|'<'*|'^'*|'~'*|'='*)
      if ! _semver_is_version "$want"; then return 3; fi
      # Checked for grammar first, then normalised: "20..1" must still be
      # rejected rather than truncated into something legal.
      want="$(_semver_truncate_wildcard "$want")"
      ;;
  esac

  # node-semver resolves an X-range with an unstated major before any comparator
  # sees it: ">=x" and "<=x" become "*", "^x" and "~x" become "*", and ">x" and
  # "<x" become "<0.0.0-0", which nothing satisfies. Falling through to the
  # numeric path read the wildcard as 0 instead and got four of the six
  # backwards -- "<=x", "^x" and "~x" rejected every runtime, and ">x" admitted
  # every one. Bare "x" is not listed because the X-range branch below already
  # returns 0 for it, which is the same answer.
  if ! _semver_spec_states "$want" 1; then
    case "$tok" in
      '>='*|'<='*|'^'*|'~'*) return 0 ;;
      '>'*|'<'*)             return 1 ;;
    esac
  fi

  case "$tok" in
    '>='*)
      # >=20 is >=20.0.0: zero-filling is already the right reading.
      if [ "$(_semver_cmp "$version" "$want")" != "-1" ]; then return 0; fi
      return 1
      ;;
    '<='*)
      # <=20 admits every 20.x: node-semver expands it to <21.0.0-0. Comparing
      # against a zero-filled 20.0.0 rejected 20.20.2, a conforming runtime.
      if [ -n "$(_semver_upper_bound "$want")" ]; then
        if [ "$(_semver_cmp "$version" "$(_semver_upper_bound "$want")")" = "-1" ]; then return 0; fi
        return 1
      fi
      if [ "$(_semver_cmp "$version" "$want")" != "1" ]; then return 0; fi
      return 1
      ;;
    '>'*)
      # >20 requires 21 or later; against a zero-filled 20.0.0 it wrongly
      # admitted 20.20.2. The bound is the same one <= uses, from the other side.
      if [ -n "$(_semver_upper_bound "$want")" ]; then
        if [ "$(_semver_cmp "$version" "$(_semver_upper_bound "$want")")" != "-1" ]; then return 0; fi
        return 1
      fi
      if [ "$(_semver_cmp "$version" "$want")" = "1" ]; then return 0; fi
      return 1
      ;;
    '<'*)
      # <20 is <20.0.0: zero-filling is already the right reading.
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 0; fi
      return 1
      ;;
    '^'*)
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 1; fi
      if [ "$(_semver_part "$version" 1)" != "$(_semver_part "$want" 1)" ]; then return 1; fi
      # Below 1.0.0 the minor carries the breaking change, so ^0.2.3 admits
      # 0.2.x only. Comparing majors alone would accept 0.3.0 -- but only when
      # the operand states a minor: bare "^0" is >=0.0.0 <1.0.0.
      if [ "$(_semver_part "$want" 1)" = "0" ] \
        && _semver_spec_states "$want" 2 \
        && [ "$(_semver_part "$version" 2)" != "$(_semver_part "$want" 2)" ]; then
        return 1
      fi
      # The rule recurses one level further down: "^0.0.3" is ">=0.0.3 <0.0.4",
      # because below 0.1.0 the patch is what carries the breaking change.
      # Constraining the minor alone admitted 0.0.9 for a range that means
      # 0.0.3 and nothing else.
      if [ "$(_semver_part "$want" 1)" = "0" ] \
        && _semver_spec_states "$want" 2 && [ "$(_semver_part "$want" 2)" = "0" ] \
        && _semver_spec_states "$want" 3 \
        && [ "$(_semver_part "$version" 3)" != "$(_semver_part "$want" 3)" ]; then
        return 1
      fi
      return 0
      ;;
    '~'*)
      if [ "$(_semver_cmp "$version" "$want")" = "-1" ]; then return 1; fi
      if [ "$(_semver_part "$version" 1)" != "$(_semver_part "$want" 1)" ]; then return 1; fi
      # "~20" is the whole of major 20; only "~20.1" pins the minor. Comparing
      # unconditionally read the omitted minor as 0 and rejected 20.20.2, and
      # then "~20.x" -- a minor stated as a wildcard -- did the same thing.
      if _semver_spec_states "$want" 2 \
        && [ "$(_semver_part "$version" 2)" != "$(_semver_part "$want" 2)" ]; then
        return 1
      fi
      return 0
      ;;
    '='*|[0-9]*|v[0-9]*)
      # An X-range: compare only the components the range actually states. "22"
      # means >=22.0.0 <23.0.0 and "22.x" the same, so an unstated component
      # ends the comparison rather than defaulting to 0 — reading "20" as
      # "20.0.0" rejected Node 20.20.2, a conforming runtime.
      #
      # "=20" is the same range. Routing it through exact comparison instead
      # reintroduced the defaulting this branch exists to avoid, for the one
      # spelling that makes the intent explicit.
      case "$tok" in '='*) spec="$want" ;; *) spec="$tok" ;; esac
      # A bare operand needs the same grammar check as an operator's: "20banana"
      # starts with a digit, so it reached the loop, matched on the major and
      # returned satisfied on the unstated minor.
      if ! _semver_is_version "$spec"; then return 3; fi
      for i in 1 2 3; do
        case "$(_semver_spec_part "$spec" "$i")" in
          ''|'x'|'X'|'*') return 0 ;;
        esac
        if [ "$(_semver_part "$version" "$i")" != "$(_semver_part "$spec" "$i")" ]; then
          return 1
        fi
      done
      return 0
      ;;
    *)
      return 2
      ;;
  esac
}

# _semver_satisfies <version> <range> – 0 satisfied, 1 unsatisfied,
# 2 unverifiable. Alternatives split on "||"; tokens within one alternative are
# conjunctive, as npm defines them.
_semver_satisfies() {
  local version="$1" rest="$2"
  local alt tok ok alt_tokens rc more=1
  local satisfied=0 malformed=0 unrecognised=0 seen_alt=0
  local -a alt_toks
  while [ "$more" -eq 1 ]; do
    case "$rest" in
      *'||'*) alt="${rest%%||*}"; rest="${rest#*||}" ;;
      *)      alt="$rest"; more=0 ;;
    esac
    seen_alt=1
    ok=1
    alt_tokens=0
    # Word-splitting is the point: the tokens of one alternative are whitespace
    # separated and conjunctive, as npm defines them. Globbing is disabled
    # around the split because a bare "*" range would otherwise expand to the
    # directory listing and stop looking like a range at all.
    set -f
    # shellcheck disable=SC2086
    set -- $alt
    # npm allows whitespace between a comparator and its operand: ">= 20" is
    # the same range as ">=20". Splitting on whitespace alone left a bare ">=",
    # which the grammar check correctly calls malformed -- and so a conforming
    # environment was blocked by an infrastructure failure over a space.
    alt_toks=()
    while [ "$#" -gt 0 ]; do
      case "$1" in
        '>='|'<='|'>'|'<'|'='|'^'|'~')
          if [ "$#" -ge 2 ]; then
            alt_toks+=("$1$2")
            shift 2
            continue
          fi
          ;;
      esac
      alt_toks+=("$1")
      shift
    done
    for tok in ${alt_toks[@]+"${alt_toks[@]}"}; do
      alt_tokens=$((alt_tokens + 1))
      rc=0
      _semver_token_ok "$version" "$tok" || rc=$?
      if [ "$rc" -eq 2 ]; then
        unrecognised=1
      fi
      if [ "$rc" -eq 3 ]; then
        malformed=1
      fi
      # No early exit: a later token in this alternative may be the
      # unrecognised one, and "cannot evaluate" must win over "does not
      # satisfy" so the message names the real problem.
      if [ "$rc" -ne 0 ]; then
        ok=0
      fi
    done
    set +f
    # An empty alternative comes from a trailing or doubled "||". It is
    # malformed input, not a wildcard: counting tokens across all alternatives
    # let ">=999.0.0 ||" come back satisfied, because the empty alternative
    # inherited the previous one's token count and its untouched ok=1.
    if [ "$alt_tokens" -eq 0 ]; then
      malformed=1
      continue
    fi
    if [ "$ok" -eq 1 ]; then
      satisfied=1
    fi
  done
  # Malformed input is unverifiable however well one of its alternatives
  # matches -- there is no early return above, so a satisfied alternative cannot
  # hide a broken one. That covers an empty alternative from a stray "||" and an
  # invalid operand alike, because both are typos rather than range forms.
  # An *unrecognised* alternative is different: a valid range form this
  # comparator does not implement must not fail a runtime that another
  # alternative plainly admits.
  if [ "$malformed" -eq 1 ] || [ "$seen_alt" -eq 0 ]; then
    return 2
  fi
  if [ "$satisfied" -eq 1 ]; then
    return 0
  fi
  if [ "$unrecognised" -eq 1 ]; then
    return 2
  fi
  return 1
}

DECLARED_NODE="$(_manifest_field engines.node)"
if [ -n "$DECLARED_NODE" ]; then
  ACTUAL_NODE="$(node --version 2>/dev/null || true)"
  _node_rc=0
  _semver_satisfies "$ACTUAL_NODE" "$DECLARED_NODE" || _node_rc=$?
  case "$_node_rc" in
    0) ;;
    1)
      echo "Node ${ACTUAL_NODE:-<unknown>} does not satisfy the engines.node range"
      echo "  declared by ${CI_GATE_NODE_WORKSPACE}/package.json: ${DECLARED_NODE}"
      echo "  Results produced under an undeclared runtime are not reproducible,"
      echo "  so the lane stops before installing or running anything."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
    *)
      echo "Cannot evaluate the engines.node range declared by"
      echo "  ${CI_GATE_NODE_WORKSPACE}/package.json: ${DECLARED_NODE}"
      echo "  The lane refuses to run rather than assume the toolchain conforms."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
  esac
fi

DECLARED_PM="$(_manifest_field packageManager)"
if [ -n "$DECLARED_PM" ]; then
  # corepack's format is name@version, optionally with a +integrity suffix. The
  # version is a pin, not a range: two patch releases of a package manager can
  # resolve a lockfile differently.
  PM_NAME="${DECLARED_PM%%@*}"
  PM_VERSION="${DECLARED_PM#*@}"
  PM_VERSION="${PM_VERSION%%+*}"

  if [ "$PM_NAME" != "$MANAGER" ]; then
    echo "The lockfile in ${CI_GATE_NODE_WORKSPACE} selects ${MANAGER}, but"
    echo "  package.json declares packageManager ${DECLARED_PM}."
    echo "  Installing with a manager the manifest does not declare produces a"
    echo "  tree the lockfile does not describe."
    exit "$CI_RESULT_FAIL_INFRA"
  fi

  ci::common::command_exists "$PM_NAME" || {
    echo "package.json declares packageManager ${DECLARED_PM} but ${PM_NAME} is missing."
    exit "$CI_RESULT_FAIL_INFRA"
  }
  ACTUAL_PM="$("$PM_NAME" --version 2>/dev/null || true)"
  ACTUAL_PM="${ACTUAL_PM#v}"
  if [ "$ACTUAL_PM" != "$PM_VERSION" ]; then
    echo "${PM_NAME} ${ACTUAL_PM:-<unknown>} is installed, but"
    echo "  ${CI_GATE_NODE_WORKSPACE}/package.json pins packageManager ${DECLARED_PM}."
    echo "  Install the declared version (corepack, or the manager's own"
    echo "  upgrade path) so the install is the one the lockfile describes."
    exit "$CI_RESULT_FAIL_INFRA"
  fi
fi

# The fingerprint covers the manifest as well as the lockfile. Keyed on the
# lockfile alone, a package.json edited without a matching lockfile update looks
# cached, the frozen install that would have caught the mismatch is skipped, and
# tests pass as long as they do not touch the changed dependency.
_deps_fingerprint() {
  printf '%s %s\n' \
    "$(ci::common::hash_file "$LOCKFILE")" \
    "$(ci::common::hash_file package.json)"
}

SKIP_INSTALL=0
if [ -n "$LOCKFILE" ] && [ -d "node_modules" ] && [ -f "$NODE_HASH_FILE" ]; then
  CURRENT_HASH="$(_deps_fingerprint)"
  CACHED_HASH="$(cat "$NODE_HASH_FILE")"
  if [ "$CURRENT_HASH" = "$CACHED_HASH" ]; then
    echo "node_modules up to date. Skipping install."
    SKIP_INSTALL=1
  fi
fi

# Dependency installation is provisioning, not a code result. Under set -e a
# registry outage or auth failure exits 1, which normalize_result would map to
# FAIL_NEW_ISSUE and blame the code for a broken environment. Each install is
# pinned to FAIL_INFRA explicitly so the classification survives that mapping.
if [ "$SKIP_INSTALL" = "0" ]; then
  case "$MANAGER" in
  pnpm)
    ci::common::command_exists pnpm || { echo "pnpm lockfile found but pnpm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: pnpm install --frozen-lockfile"
    pnpm install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  npm)
    ci::common::command_exists npm || { echo "npm lockfile found but npm is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: npm ci --quiet"
    npm ci --quiet || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  yarn)
    ci::common::command_exists yarn || { echo "yarn lockfile found but yarn is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    YARN_VERSION="$(yarn --version 2>/dev/null || echo "0")"
    YARN_MAJOR="${YARN_VERSION%%.*}"
    if [ "$YARN_MAJOR" = "1" ]; then
      echo "Installing dependencies: yarn install --frozen-lockfile"
      yarn install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    else
      echo "Installing dependencies: yarn install --immutable"
      yarn install --immutable || exit "$CI_RESULT_FAIL_INFRA"
    fi
    ;;
  bun)
    ci::common::command_exists bun || { echo "bun lockfile found but bun is missing."; exit "$CI_RESULT_FAIL_INFRA"; }
    echo "Installing dependencies: bun install --frozen-lockfile"
    bun install --frozen-lockfile || exit "$CI_RESULT_FAIL_INFRA"
    ;;
  esac

  mkdir -p "$(dirname "$NODE_HASH_FILE")"
  _deps_fingerprint > "$NODE_HASH_FILE"
fi

case "$MANAGER" in
  pnpm) ci::common::command_exists pnpm || { echo "pnpm is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  npm) ci::common::command_exists npm || { echo "npm is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  yarn) ci::common::command_exists yarn || { echo "yarn is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
  bun) ci::common::command_exists bun || { echo "bun is required to run package scripts."; exit "$CI_RESULT_FAIL_INFRA"; } ;;
esac

PACKAGE_SCRIPTS="$(node -e "try{const p=require('./package.json');console.log(Object.keys(p.scripts||{}).join('\n'))}catch(e){console.error('Invalid package.json:',e.message);process.exit(1)}" 2>&1)" || {
  echo "$PACKAGE_SCRIPTS" >&2
  echo "Failed to read scripts from package.json."
  exit "$CI_RESULT_FAIL_INFRA"
}

script_exists() {
  local script_name="$1"
  printf '%s\n' "$PACKAGE_SCRIPTS" | grep -Fx -- "$script_name" >/dev/null 2>&1
}

run_script() {
  local script_name="$1"
  local rc=0
  if script_exists "$script_name"; then
    echo "Running script: $script_name"
    case "$MANAGER" in
      pnpm) pnpm run "$script_name" || rc=$? ;;
      npm) npm run "$script_name" || rc=$? ;;
      yarn) yarn run "$script_name" || rc=$? ;;
      bun) bun run "$script_name" || rc=$? ;;
    esac
    if [ "$rc" -ne 0 ]; then
      # A package script does not implement the gate's result contract, and its
      # exit code collides with it: `vitest` or any tool it wraps exiting 10
      # propagated straight through as PASS_WITH_KNOWN_DEBT, so preflight
      # recorded the failed lane as passed, skipped the remaining scripts and
      # exited 0. Every nonzero script status is a failing script.
      echo "Script '${script_name}' failed with status ${rc}."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  else
    echo "Skipping missing script: $script_name"
  fi
}

# A workspace that declares TypeScript must be able to typecheck it. `vite build`
# does not run tsc -- frontend/README.md says so explicitly -- so with the
# `typecheck` script removed or renamed, run_script logs "Skipping missing
# script" and the lane exits 0 after tests and a build that never looked at a
# type. The one check that would have caught it is the one that silently
# stopped running.
if [ -f tsconfig.json ] || [ -f jsconfig.json ]; then
  if ! script_exists "typecheck"; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} declares TypeScript configuration but"
    echo "  defines no 'typecheck' script, so nothing here ever runs tsc:"
    [ -f tsconfig.json ] && echo "    ${CI_GATE_NODE_WORKSPACE}/tsconfig.json"
    [ -f jsconfig.json ] && echo "    ${CI_GATE_NODE_WORKSPACE}/jsconfig.json"
    echo "  A bundler build is not a typecheck. Restore the script, or remove"
    echo "  the TypeScript configuration with it."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi
fi

# A workspace that ships tests must be able to run them. run_script only logs
# "Skipping missing script", so deleting or renaming `test` would remove the
# whole suite from the gate while the lane still exits 0 — the suite passing by
# never running, which is the failure this gate exists to catch. Keyed on the
# presence of test files rather than on checks.yml, so the rule travels with the
# workspace and a genuinely test-free workspace is unaffected.
if ! script_exists "test" && ! script_exists "test:unit"; then
  ORPHAN_TESTS="$(find . \( -name 'node_modules' -o -name 'dist' -o -name 'build' \) -prune -o \
    -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print 2>/dev/null | head -5 || true)"
  if [ -n "$ORPHAN_TESTS" ]; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} ships tests but defines no 'test' or 'test:unit' script."
    echo "  These would never run:"
    # Read line by line: a path with a space is one file, not two.
    while IFS= read -r _orphan; do
      [ -n "$_orphan" ] || continue
      echo "    $_orphan"
    done <<< "$ORPHAN_TESTS"
    echo "  Restore the script in package.json, or remove the tests."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi

  # "No test script AND no test files" is the one arrangement the rule above
  # cannot see, and it is the worst one: deleting every file under tests/ and
  # both scripts leaves nothing to be orphaned, test-layout reports "0 file(s)"
  # quite happily, and a successful build carries the whole gate to exit 0 after
  # the suite has disappeared. A workspace that had tests must still have them.
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
    && git rev-parse --verify HEAD >/dev/null 2>&1; then
    HEAD_TESTS="$(git ls-tree -r --name-only HEAD -- . 2>/dev/null \
      | grep -E '\.(test|spec)\.[cm]?[jt]sx?$' | head -5 || true)"
    if [ -n "$HEAD_TESTS" ]; then
      echo "Workspace ${CI_GATE_NODE_WORKSPACE} has lost its entire test suite."
      echo "  HEAD carries test files here, this tree has none, and the manifest"
      echo "  defines no 'test' or 'test:unit' script -- so nothing is reported"
      echo "  as orphaned and the lane would pass having run no tests at all."
      echo "  Present at HEAD, for example:"
      while IFS= read -r _gone; do
        [ -n "$_gone" ] || continue
        echo "    $_gone"
      done <<< "$HEAD_TESTS"
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  fi
fi

run_script "format:check"
run_script "lint"
run_script "typecheck"
run_script "test"
run_script "test:unit"
run_script "build"

echo "Node lane passed."
exit "$CI_RESULT_PASS"
