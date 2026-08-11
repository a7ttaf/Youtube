#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=ci/lib/common.sh
source "$ROOT_DIR/ci/lib/common.sh"
# shellcheck source=ci/lib/git.sh
source "$ROOT_DIR/ci/lib/git.sh"

cd "$ROOT_DIR"

ci::common::section "Check: node lane"

# The lane body below handles exactly one workspace. When invoked normally we
# discover the workspaces and re-enter once per directory; CI_GATE_NODE_WORKSPACE
# marks that recursive call. A repo with a root package.json resolves to "."
# and behaves exactly as before.
if [ -z "${CI_GATE_NODE_WORKSPACE:-}" ]; then
  # Discovery can now refuse: a root manifest coexisting with nested ones is a
  # layout it cannot cover without guessing, and under `set -e` the bare
  # assignment would abort the script with status 1 rather than say so.
  _ws_rc=0
  NODE_WORKSPACES="$(ci::common::node_workspaces package.json)" || _ws_rc=$?
  if [ "$_ws_rc" -ne 0 ]; then
    echo "Workspace discovery could not resolve this layout; see above."
    exit "$CI_RESULT_FAIL_INFRA"
  fi

  # Discovery stats the filesystem, so a workspace that exists only in the index
  # is invisible to it: stage a new added/package.json whose test script fails,
  # then remove added/ from disk, and the lane never looks at it. The index is a
  # second source of workspaces, and one that describes the commit rather than
  # the tree.
  #
  # To any depth, matching discovery. `NF == 2` covered one level only, so a
  # staged packages/app/package.json was dropped here exactly as it was dropped
  # by the filesystem walk before that was made recursive — the two sources have
  # to agree about what a workspace is or the lane covers the intersection.
  if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    INDEXED_WS="$(git ls-files -- 'package.json' '*/package.json' 2>/dev/null \
      | awk -F/ 'NF == 1 { print "."; next }
                 { d = $1; for (i = 2; i < NF; i++) d = d "/" $i; print d }' \
      | sort -u || true)"
    if [ -n "$INDEXED_WS" ]; then
      NODE_WORKSPACES="$(printf '%s\n%s\n' "$NODE_WORKSPACES" "$INDEXED_WS" \
        | sed '/^$/d' | sort -u)"
    fi
  fi

  # "No manifest" and "no JavaScript here" are not the same statement. Deleting
  # frontend/package.json leaves the lockfile, the tsconfig and the vitest
  # config behind; discovery then finds nothing there, and a commit that makes
  # the workspace uninstallable ran no install, typecheck, test or build.
  #
  # Keyed on workspace *configuration*, not on source files: a lockfile or a
  # tsconfig/vite/vitest config with no manifest beside it is unambiguous
  # evidence of a removed manifest, whereas a stray .js in a non-JS repo is not.
  #
  # Runs for every directory, not only when the repository has no workspace left
  # at all. Nesting it under that condition made the scan a property of the
  # *repository*: with two sibling workspaces, deleting b/package.json still
  # left a, discovery succeeded, and b -- lockfile and all -- was never looked
  # at. An orphan is a property of the directory it sits in.
  ORPHAN_CFG="$(find . \
      \( -name 'node_modules' -o -name '.git' -o -name 'dist' -o -name 'build' \) -prune -o \
      -type f \( -name 'package-lock.json' -o -name 'npm-shrinkwrap.json' \
        -o -name 'pnpm-lock.yaml' -o -name 'yarn.lock' -o -name 'bun.lock' -o -name 'bun.lockb' \
        -o -name 'tsconfig.json' -o -name 'jsconfig.json' \
        -o -name 'vitest.config.*' -o -name 'vite.config.*' \) -print 2>/dev/null || true)"

  ORPHANS=""
  while IFS= read -r _cfg; do
    [ -n "$_cfg" ] || continue
    _cfg_dir="$(dirname "$_cfg")"
    # A manifest beside it settles the question — on disk, or in the index,
    # because a workspace being added is not an orphan either.
    [ -f "$_cfg_dir/package.json" ] && continue
    if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
      git cat-file -e ":${_cfg_dir#./}/package.json" 2>/dev/null && continue
    fi
    ORPHANS="${ORPHANS}${_cfg#./}"$'\n'
  done <<< "$ORPHAN_CFG"
  ORPHANS="$(printf '%s' "$ORPHANS" | sed '/^$/d' | head -10)"

  if [ -n "$ORPHANS" ]; then
    echo "Workspace configuration with no package.json beside it:"
    while IFS= read -r _orphan; do
      [ -n "$_orphan" ] || continue
      echo "    $_orphan"
    done <<< "$ORPHANS"
    echo "  Nothing can be installed or run against these, so the lane would"
    echo "  pass having executed no install, typecheck, test or build there."
    echo "  Restore the manifest, or remove the configuration with it."
    exit "$CI_RESULT_FAIL_NEW_ISSUE"
  fi

  if [ -z "$NODE_WORKSPACES" ]; then
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
    # The range this push carries, resolved once for the deletion lookup below.
    _gate_range=""
    if [ "${CI_GATE_MODE:-}" = "ship" ] && type ci::git::push_range >/dev/null 2>&1; then
      _gate_range="$(ci::git::push_range 2>/dev/null || true)"
    fi
    [ -n "$_gate_range" ] || _gate_range="HEAD"

    PARTIAL=""
    while IFS= read -r _ws; do
      [ -n "$_ws" ] || continue
      _scope="$_ws"
      [ "$_scope" = "." ] && _scope="."

      if [ "${CI_GATE_MODE:-}" = "ship" ]; then
        _drift="$( {
          git -c core.quotepath=false diff --name-only HEAD -- "$_scope" 2>/dev/null || true
          git -c core.quotepath=false ls-files --others --exclude-standard -- "$_scope" 2>/dev/null || true
          # And the ignored ones — but only where an ignored file *shadows a
          # path this push removes*. That is the whole of the reported defect: a
          # commit deletes a script and adds its path to .gitignore, so HEAD no
          # longer carries the path (`git diff HEAD` is silent) and
          # --exclude-standard drops the replacement, and the lane runs it.
          #
          # An earlier attempt filtered the whole ignored list through a prune
          # list of directory names. That was wrong twice, and both ways were
          # demonstrated rather than argued: the names matched at any depth, so
          # it swallowed a genuine source file under `frontend/src/build/`; and
          # it reported `frontend/.env.local`, a `tsbuildinfo` and Playwright
          # output as drift, which makes the ship gate unpassable during
          # ordinary development — while its printed remedy, "commit the rest",
          # is precisely what git-safety.sh blocks. Two checks, one file,
          # contradictory instructions. Keying on deletions needs no prune list.
          while IFS= read -r _d; do
            [ -n "$_d" ] || continue
            [ -e "$_d" ] && printf '%s\n' "$_d"
          done <<< "$(git -c core.quotepath=false log --diff-filter=D --name-only --format= "$_gate_range" -- "$_scope" 2>/dev/null || true)"
        } | sort -u | sed '/^$/d')"
        [ -n "$_drift" ] && PARTIAL="${PARTIAL}${_drift}"$'\n'
        continue
      fi

      _staged="$(git diff --cached --name-only -- "$_scope" 2>/dev/null | sort || true)"
      [ -n "$_staged" ] || continue

      # Something here is part of this commit, so the whole workspace has to
      # match the index — not merely the staged paths.
      #
      # Restricting the comparison to staged paths made the rule per-file, and
      # the lane is not per-file: it installs, typechecks, tests and builds the
      # workspace as a unit. Stage a test that imports a new helper, leave the
      # helper untracked, and every staged path matched the index while `tsc`
      # passed only because of a file the commit does not contain. Checking out
      # that commit gives an unresolved import.
      #
      # The three lists are one question asked three ways, and each was needed
      # in turn: `git diff` compares tracked content and misses a path staged
      # for deletion and recreated; `--others` finds that one but `.gitignore`
      # hides it; `--ignored` finds it but drags in every build artifact, so it
      # is pruned to the directories a workspace is expected to ignore.
      _diverged="$( {
        git -c core.quotepath=false diff --name-only -- "$_scope" 2>/dev/null || true
        git -c core.quotepath=false ls-files --others --exclude-standard -- "$_scope" 2>/dev/null || true
        git -c core.quotepath=false ls-files --others --ignored --exclude-standard -- "$_scope" 2>/dev/null \
          | grep -Ev '(^|/)(node_modules|dist|build|coverage|\.next|\.turbo|\.vite|\.git|\.ci-gate)/' \
          || true
      } | sort -u | sed '/^$/d')"
      [ -n "$_diverged" ] && PARTIAL="${PARTIAL}${_diverged}"$'\n'
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
# A numeric identifier is `0` or a digit string with no leading zero, and it has
# to stay inside the range shell arithmetic can hold. Both matter here and both
# were `[0-9]+`:
#   * `020` is octal to `$(( 020 + 1 ))` (17) and decimal to `[ 020 -gt 20 ]`, so
#     _semver_upper_bound and _semver_cmp read the same operand as two different
#     numbers; `08` aborts the arithmetic outright, and the empty result is
#     indistinguishable from "all three components stated", which silently
#     reverts <= and > to the comparison the bound exists to replace.
#   * 20 digits wraps: _semver_upper_bound 99999999999999999999 produced
#     7766279631452241920, and >= against it came back satisfied for Node 20.
# node-semver rejects both outright, which is the behaviour worth copying.
# The ceiling is node-semver's, which is Number.MAX_SAFE_INTEGER —
# 9007199254740991, sixteen digits. A flat 15-digit cap was neither that
# boundary nor a shell one (bash counts to 9223372036854775807, nineteen
# digits): it rejected the exact value node-semver documents and accepts, and
# because an oversized operand is classed *malformed* rather than merely
# unsupported, it poisoned every sibling — ">=20 || >=9007199254740991" stopped
# the lane over a digit count in an alternative npm reads fine, while the first
# alternative plainly admitted the runtime. Sixteen digits is well inside
# bash arithmetic, so the real boundary can simply be compared.
_semver_num_ok() {
  case "$1" in
    0) return 0 ;;
    ''|*[!0-9]*|0*) return 1 ;;
  esac
  case "${#1}" in
    [1-9]|1[0-5]) return 0 ;;
    16) [ "$1" -le 9007199254740991 ] ;;
    *) return 1 ;;
  esac
}

# node-semver's PRERELEASEIDENTIFIER and BUILDIDENTIFIER, which are not the
# loose `[0-9A-Za-z.-]+` this used to accept. That looseness admitted a
# leading-zero numeric identifier and empty identifiers — "1.2.3-01",
# "1.2.3-a..b", "1.2.3+." — every one of which node-semver refuses to parse.
# Unlike the cases above this one failed *open*: "1.2.3-01 || >=20" came back
# SATISFIED against a range npm will not construct at all.
_SEMVER_PRE_ID='(0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)'
_SEMVER_PRE="(-${_SEMVER_PRE_ID}([.]${_SEMVER_PRE_ID})*)?"
_SEMVER_BUILD='([+][0-9A-Za-z-]+([.][0-9A-Za-z-]+)*)?'

# _semver_is_version <string> [x_order_ok] – 0 when the string is a usable
# semver operand. Pass a non-empty second argument to permit a stated component
# to the right of a wildcard.
#
# That second argument exists because invalidXRangeOrder is not a property of
# the operand, it is a property of where the operand sits. node-semver calls it
# from replaceXRange only, so "20.x.3" throws on its own and after any of
# = >= > < <=, and is accepted after ^ or ~ (replaceTilde and replaceCaret
# rewrite the operand before any X-range pass sees it) and as a hyphen endpoint
# (hyphenReplace never routes through it). Verified against semver 7.8.5:
# ~20.x.3, ^20.x.3 and "20.x.3 - 22" all construct; 20.x.3 and >=20.x.3 throw.
# Applying the rule to every operand made the gate refuse "~22.x.1", a range
# every published version of node-semver accepts.
_semver_is_version() {
  local s="${1#v}" x_order_ok="${2:-}" core i part seen_wildcard=0
  case "$s" in *' '*|'') return 1 ;; esac
  core="${s%%[-+]*}"
  # The grammar first: shape, characters, component count.
  printf '%s' "$s" | grep -Eq \
    "^([0-9]+|[xX*])([.]([0-9]+|[xX*]))?([.]([0-9]+|[xX*]))?${_SEMVER_PRE}${_SEMVER_BUILD}\$" \
    || return 1
  # A prerelease may only attach to a fully stated operand. node-semver's
  # XRANGEPLAIN puts the prerelease inside the third component's group, so it
  # is reachable only once all three are present -- and this holds whether they
  # are numbers or wildcards. Measured on 7.8.5: `20.1.2-alpha`, `20.x.x-alpha`
  # and `20.1.x-alpha` construct; `20-alpha`, `20.1-alpha`, `20.x-alpha` and
  # `x-alpha` all throw, under every operator. Without this, "^20.x-alpha"
  # passed the grammar, truncated at the wildcard to a bare "20", and came back
  # SATISFIED for a range npm will not build -- the same fail-open the
  # identifier grammar above closes, one component to the left. Build metadata
  # carries no such rule: "20+b" and "20.x+b" are both fine.
  if _semver_has_prerelease "$s"; then
    case "$core" in
      *.*.*) ;;
      *) return 1 ;;
    esac
  fi
  # Then each component, which the pattern above cannot express.
  for i in 1 2 3; do
    part="$(printf '%s' "$core" | cut -d. -f"$i" -s)"
    [ "$i" -eq 1 ] && part="$(printf '%s' "$core" | cut -d. -f1)"
    [ -n "$part" ] || break
    case "$part" in
      x|X|'*') seen_wildcard=1 ; continue ;;
    esac
    # A stated number to the right of a wildcard is an invalid X-range order.
    # node-semver's invalidXRangeOrder rejects "20.x.3" outright rather than
    # reading it as "20.x.x" -- it aborts the X-range rewrite and the untouched
    # comparator then throws. This gate accepted it and evaluated a range npm
    # will not install against.
    if [ "$seen_wildcard" -eq 1 ] && [ -z "$x_order_ok" ]; then return 1; fi
    _semver_num_ok "$part" || return 1
  done
  return 0
}

# _semver_has_prerelease <string> – 0 when the operand or version carries one.
#
# This comparator implements numeric precedence over major.minor.patch and
# nothing else, and prerelease precedence is a genuinely different ordering:
# 1.2.3-alpha.1 is *below* 1.2.3, and alpha.7 is above alpha.3 by identifier
# rules, not by any comparison of the three numbers. Ignoring the tail did not
# make those cases unsupported, it made them wrong in the fail-open direction —
# measured against semver 7.8.5, `1.2.3-alpha.1` came back satisfied by 1.2.3
# and by 1.2.3-alpha.2, `<=1.2.3-alpha.1` by 1.2.3, and a plain `20.1.0` by a
# 20.1.0-rc.1 runtime. Every one of those is npm=false, bash=satisfied.
#
# So it is declared unsupported instead, on either side of the comparison, and
# an unsupported form stops the lane rather than passing it. A sibling
# alternative that plainly admits the runtime still wins, exactly as for a
# hyphen range.
_semver_has_prerelease() {
  local s="${1#v}"
  s="${s%%+*}"
  case "$s" in *-*) return 0 ;; esac
  return 1
}

# _semver_hyphen_endpoint_ok <string> – 0 when the string may sit either side of
# the `-` in a hyphen range.
#
# node-semver's XRANGEPLAIN starts `[v=\s]*`, so an endpoint absorbs a redundant
# leading `=` the way an operator's operand does, and hyphenReplace applies no
# X-range order rule. Reusing the bare-operand predicate here rejected "= 20 -
# 22", "20.x.3 - 22" and 16-digit endpoints — all three accepted by 7.8.5 — and
# a missed hyphen range is classed malformed, which then poisons the sibling
# alternative that the hyphen handling exists to let win.
_semver_hyphen_endpoint_ok() {
  local s="${1#v}"
  _semver_is_version "${s#=}" x_order_ok
}

# _semver_is_concrete_version <string> – 0 when the string is a real version, as
# opposed to a range operand: three numbers, no wildcards.
#
# Nothing validated the *runtime* at all. `_semver_part` strips non-digits and
# defaults to 0, so "banana" became 0.0.0; and an empty string gives awk no
# record at all, so it printed nothing, `[ "" -gt "20" ]` errored, and every
# component compared equal — reporting a match. `node --version` emitting a shim
# banner, a wrapper's noise, or nothing at all therefore turned a `>=` gate into
# a pass, which is the one failure that defeats the whole check.
_semver_is_concrete_version() {
  local s="${1#v}" core i part
  case "$s" in *' '*|'') return 1 ;; esac
  core="${s%%[-+]*}"
  printf '%s' "$s" | grep -Eq \
    "^[0-9]+[.][0-9]+[.][0-9]+${_SEMVER_PRE}${_SEMVER_BUILD}\$" || return 1
  for i in 1 2 3; do
    part="$(printf '%s' "$core" | cut -d. -f"$i")"
    _semver_num_ok "$part" || return 1
  done
  return 0
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
    # A multi-component all-wildcard token is the same range written longer:
    # `x.x`, `*.*.*`, `vx`, `v*`. The fast path above matched only the exact
    # one-character spellings, and the bare-version branch below demands a
    # leading digit, so these fell through to the catch-all and stopped the
    # lane on a range npm reads as "any version".
    [xX*]|v[xX*]|[xX*].*|v[xX*].*)
      case "$(printf '%s' "${tok#v}" | tr -d 'xX*.')" in
        '') return 0 ;;
      esac
      ;;
    '>='*) want="${tok#>=}" ;;
    '<='*) want="${tok#<=}" ;;
    '>'*)  want="${tok#>}" ;;
    '<'*)  want="${tok#<}" ;;
    '^'*)  want="${tok#^}" ;;
    # LONETILDE is `~>?`, so `~>20` is `~20` — Ruby's spelling, which npm
    # accepts. Stripping only the tilde left `>20`, which the grammar check
    # then rejected, and a valid range came back unverifiable.
    '~>'*) want="${tok#\~>}" ;;
    '~'*)  want="${tok#\~}" ;;
    '='*)  want="${tok#=}" ;;
    *)     want="" ;;
  esac

  # An operator with a malformed operand is unverifiable, not satisfied.
  # _semver_part strips non-numeric text and defaults to 0, so ">=banana" and a
  # bare ">=" both became >=0.0.0 and admitted everything -- the boundary
  # switched off by a typo in a manifest.
  # node-semver's XRANGEPLAIN begins `[v=\s]*`, so it absorbs a redundant `=`
  # on the operand: `>==20` is `>=20`. Stripping only `>=` left `=20`, which the
  # grammar check rejected.
  case "$tok" in
    '>='*|'<='*|'>'*|'<'*|'^'*|'~'*) want="${want#=}" ;;
  esac

  case "$tok" in
    # ^ and ~ rewrite their operand before any X-range pass, so an X-range
    # order that throws bare is legal after them. See _semver_is_version.
    '^'*|'~'*)
      if ! _semver_is_version "$want" x_order_ok; then return 3; fi
      want="$(_semver_truncate_wildcard "$want")"
      ;;
    '>='*|'<='*|'>'*|'<'*|'='*)
      if ! _semver_is_version "$want"; then return 3; fi
      # Checked for grammar first, then normalised: "20..1" must still be
      # rejected rather than truncated into something legal.
      want="$(_semver_truncate_wildcard "$want")"
      ;;
  esac

  # Valid, and not something this comparator can order. Deliberately after the
  # grammar check, and only on the operator paths -- a bare operand is not
  # validated until its own branch below, and testing it here classed the
  # malformed "1.2.3-01" as merely unsupported, which is sibling-rescuable:
  # "1.2.3-01 || >=20" came back satisfied for a range npm refuses to build.
  case "$tok" in
    '>='*|'<='*|'>'*|'<'*|'^'*|'~'*|'='*)
      _semver_has_prerelease "$want" && return 2
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
      # Grammar first, then orderability: see the operator paths above.
      _semver_has_prerelease "$spec" && return 2
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
      # An unsupported *form* and a typo are different failures, and only the
      # first may lose to a satisfied sibling in a `||`. This catch-all returned
      # 2 for both, so `banana || >=20` came back satisfied while
      # `>=20banana || >=20` -- the same typo, one character further along --
      # correctly refused. A hyphen range is the form this comparator does not
      # implement; everything else here is malformed.
      # `1.2.3 - 2.3.4` reaches here as three tokens, and its middle one is a
      # bare `-`. That shape is recognised by the caller, which is the only
      # place with enough context to tell it from a lone `-` typo.
      return 3
      ;;
  esac
}

# _semver_satisfies <version> <range> – 0 satisfied, 1 unsatisfied,
# 2 unverifiable. Alternatives split on "||"; tokens within one alternative are
# conjunctive, as npm defines them.
_semver_satisfies() {
  local version="$1" rest="$2"
  # The runtime is checked before any range is. An operand this comparator
  # cannot read is reported unverifiable; a *version* it cannot read was
  # coerced to 0.0.0, or worse to nothing at all, and quietly satisfied ">=".
  # Reported as its own status. Folding it into "unverifiable range" sent an
  # operator to stare at a perfectly good manifest while the actual fault was a
  # runtime that printed a shim banner, or nothing at all.
  if ! _semver_is_concrete_version "$version"; then
    return 4
  fi
  # A prerelease runtime is readable but not orderable here; see
  # _semver_has_prerelease. Reported separately so the message names the
  # runtime rather than accusing the manifest.
  if _semver_has_prerelease "$version"; then
    return 5
  fi
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
    # A hyphen range is `A - B`: three tokens, versions on both sides. Detected
    # here because a bare `-` is indistinguishable from a typo at token level,
    # and the two must not share a status -- an unsupported form may lose to a
    # satisfied sibling in a `||`, a typo may not.
    if [ "${#alt_toks[@]}" -eq 3 ] && [ "${alt_toks[1]}" = "-" ] \
      && _semver_hyphen_endpoint_ok "${alt_toks[0]}" \
      && _semver_hyphen_endpoint_ok "${alt_toks[2]}"; then
      unrecognised=1
      set +f
      continue
    fi

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
    # An empty alternative comes from a trailing or doubled "||", and this is a
    # deliberate, evidence-backed divergence from node-semver.
    #
    # What npm does: Range keeps the empty comparator set (`.filter(c =>
    # c.length)` runs over the *sets*, and an empty set parses to ANY), so the
    # whole range collapses to "". Verified against 7.8.5, not reasoned:
    # `new Range(">=999.0.0 ||").range` is "" and `.test("20.1.0")` is true.
    #
    # Why this gate refuses anyway: that reading nullifies the entire declared
    # constraint. A manifest that says ">=999.0.0" and, because of one stray
    # keystroke, admits every runtime in existence is the enforcement boundary
    # switched off by a typo -- the exact failure this comparator was hardened
    # against everywhere else. Copying npm here would be the single fail-open
    # left in the file, and it would be the loudest one. So an empty
    # alternative is classed malformed: not sibling-rescuable, reported as
    # "Cannot evaluate", naming the manifest so the typo gets fixed.
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
    5)
      echo "Node reports a prerelease version: '${ACTUAL_NODE}'"
      echo "  Prerelease precedence is not implemented here, so this gate cannot"
      echo "  say whether it satisfies ${CI_GATE_NODE_WORKSPACE}/package.json's"
      echo "  engines.node (${DECLARED_NODE}). Run the lane on a release build."
      exit "$CI_RESULT_FAIL_INFRA"
      ;;
    4)
      echo "Node reported an unreadable version: '${ACTUAL_NODE}'"
      echo "  The engines.node range declared by ${CI_GATE_NODE_WORKSPACE}/package.json"
      echo "  (${DECLARED_NODE}) cannot be checked against it, so the lane stops."
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

script_command() {
  node -e "try{const p=require('./package.json');process.stdout.write(String((p.scripts||{})['$1']||''))}catch(e){}" 2>/dev/null || true
}

# A test script that narrows the suite is a suite that does not run.
#
# The layout guard rejects a persistent filter written into vitest.config.ts,
# and this is the same filter one layer out: `vitest run -t no-such-name` exits
# 0 with every collected test skipped, and the config the guard inspects is
# untouched. Checking that a `test` script *exists* said nothing about whether
# it runs anything, in exactly the way that checking a `typecheck` script exists
# said nothing about whether tsc ran.
#
# Matched as whole arguments so a path or a test name containing "-t" is not
# mistaken for the flag.
# _script_names_a_checker <command> [runner-list-name] – 0 when the command
# either invokes a known tool or delegates to something it names.
#
# Two ways to be acceptable, and the second is where the first attempt at this
# went wrong. Naming a tool is obvious. Delegating is legitimate too — the
# script it hands to is one the gate cannot read either way — but only when it
# *names* what it hands to. Accepting any token from a runner list meant
# `bash -c true` satisfied the guard: the exact no-op the rule exists to
# reject, one wrapper out.
#
# So an inline `-c` command disqualifies delegation. `bash scripts/check.sh`
# and `npm run check:all` name a target; `bash -c true` names nothing, and its
# contents are then judged as the command they are.
_script_names_a_checker() {
  local cmd="$1" tok has_runner=0 has_target=0
  local tools="${2:-tsc tsc.cmd tsgo vue-tsc svelte-check astro tsd attw}"
  local runners="npm pnpm yarn bun npx pnpx turbo nx lerna make bash sh zsh"
  local t

  # shellcheck disable=SC2086
  for tok in $cmd; do
    # Quotes cling to the token when the command is split on whitespace, so
    # `sh -c 'tsc --noEmit'` yields `'tsc` and would miss its own checker.
    tok="${tok#[\"\']}"
    tok="${tok%[\"\']}"
    for t in $tools; do
      [ "${tok##*/}" = "$t" ] && return 0
    done
  done

  # An inline shell command names no target; it just ran, and nothing above
  # found a tool in it.
  case " $cmd " in
    *" -c "*) return 1 ;;
  esac

  # shellcheck disable=SC2086
  for tok in $cmd; do
    case "$tok" in -*) continue ;; esac
    for t in $runners; do
      [ "${tok##*/}" = "$t" ] && has_runner=1 && continue 2
    done
    case "$tok" in
      run|exec|dlx|--) continue ;;
    esac
    [ "$has_runner" -eq 1 ] && has_target=1 && break
  done
  [ "$has_runner" -eq 1 ] && [ "$has_target" -eq 1 ]
}

_filter_reject() {
  echo "Workspace ${CI_GATE_NODE_WORKSPACE} narrows its own suite in the '$1' script:"
  echo "    $1: $2"
  echo "  '$3' selects a subset, so the lane can exit 0 with the rest of the"
  echo "  suite never collected. Remove it, or move the selection into a"
  echo "  separate script this gate does not run."
  exit "$CI_RESULT_FAIL_NEW_ISSUE"
}

assert_no_persistent_filter() {
  local script_name="$1" cmd tok runner="" prev="" is_test_runner=0
  cmd="$(script_command "$script_name")"
  [ -n "$cmd" ] || return 0

  # Which program is this script actually running? Skip the package-manager
  # wrappers a script may be written through, then take the first real word.
  # shellcheck disable=SC2086
  for tok in $cmd; do
    case "$tok" in
      npx|pnpm|bun|yarn|npm|exec|run|dlx|--yes|-y) continue ;;
      -*) continue ;;
      *) runner="${tok##*/}"; break ;;
    esac
  done
  case "$runner" in
    vitest|jest|mocha|ava|jasmine|tap|node) is_test_runner=1 ;;
  esac

  # And if it is not a runner, it has to be something that could reach one.
  # `"test": "true"` runs, exits 0, and collects nothing -- the whole suite
  # removed from the gate by a one-word manifest edit, with the lane still
  # reporting PASS over a workspace that still contains tests. The presence of
  # a `test` key was never the property worth asserting, exactly as it was not
  # for `typecheck`; this is that rule one script over.
  #
  # Delegation is accepted because the script it hands to is either another
  # package script this rule has already seen, or a shell script the gate does
  # not read either way. A no-op is not delegation.
  if [ "$is_test_runner" -ne 1 ]; then
    # Same question as the typecheck script, same answer: naming a tool, or
    # delegating to something it names. `"test": "bash -c true"` is the no-op
    # this rule rejects wearing a wrapper, so an inline `-c` command does not
    # count as delegation.
    if _script_names_a_checker "$cmd" \
      "vitest jest mocha ava jasmine tap node playwright cypress wdio karma tsx ts-node deno"; then
      : # names a runner, or delegates to a script it names
    else
        echo "Workspace ${CI_GATE_NODE_WORKSPACE} defines a '${script_name}' script that does"
        echo "  not appear to run a test runner:"
        echo "    ${script_name}: ${cmd}"
        echo "  A script named ${script_name} that exits 0 without collecting anything"
        echo "  removes the whole suite from the gate while the lane still reports"
        echo "  PASS. Recognised: vitest, jest, mocha, ava, jasmine, tap, node, a"
      echo "  wrapper script, or a runner that delegates to one. Add yours here"
      echo "  if it belongs."
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  fi

  # The flags are an allow-list for a known runner, not a deny-list.
  #
  # Enumerating the narrowing ones lost this race three times: -t, then a bare
  # positional file, then `--exclude` and `--passWithNoTests`, which together
  # make `vitest run` print "No test files found", exit 0 and satisfy every
  # earlier rule. So the question is inverted, as it already was for the config
  # properties: a flag that cannot reduce what is collected or run is named
  # here, and anything else stops the guard until someone decides which it is.
  #
  # `--passWithNoTests` is the sharpest of them and would be excluded by name
  # even if the list were kept: it converts "collected nothing" into success,
  # which is the precise failure this whole lane exists to catch.
  if [ "$is_test_runner" -eq 1 ]; then
    # shellcheck disable=SC2086
    for tok in $cmd; do
      case "$tok" in
        -*) ;;
        *) continue ;;
      esac
      case "${tok%%=*}" in
        --run|--watch|--no-watch|--coverage|--no-coverage|--reporter|--reporters \
          |--outputFile|--outputTruncateLength|--config|-c|--root|--mode|--silent \
          |--color|--no-color|--logHeapUsage|--pool|--poolOptions|--isolate \
          |--no-isolate|--threads|--no-threads|--file-parallelism \
          |--no-file-parallelism|--maxWorkers|--minWorkers|--maxConcurrency \
          |--environment|--globals|--allowOnly|--no-allowOnly|--testTimeout \
          |--hookTimeout|--teardownTimeout|--sequence|--update|--no-update \
          |--disable-console-intercept|--printConsoleTrace|--typecheck \
          |--no-typecheck|--yes|-y)
          ;;
        *)
          _filter_reject "$script_name" "$cmd" "$tok"
          ;;
      esac
    done
  fi

  # A positional argument to a test runner *is* a filter — `vitest run [...filters]`
  # is the documented syntax, and `vitest run tests/lib/confidence.test.ts` ran 4
  # tests instead of 314 while every token in the deny-list above was absent. An
  # enumerated list of narrowing flags loses this race by construction, so the
  # rule is inverted here: for a known runner, nothing but flags and their values
  # may follow. Restricted to known runners because a positional means something
  # else entirely for `bash scripts/test.sh`, which is a legitimate test script.
  [ "$is_test_runner" -eq 1 ] || return 0
  prev=""
  # shellcheck disable=SC2086
  set -- $cmd
  shift                                  # the runner or its wrapper
  while [ "$#" -gt 0 ]; do
    tok="$1"
    case "$tok" in
      # Wrapper words, the runner itself, subcommands, and shell operators are
      # not filters.
      npx|pnpm|bun|yarn|npm|exec|dlx|--yes|-y|run|watch|related|"$runner"|'&&'|'||'|';'|'|')
        prev="" ; shift ; continue ;;
      -*)
        prev="$tok" ; shift ; continue ;;
    esac
    # A bare word: the value of the flag before it, or a filter.
    case "$prev" in
      -*) case "$prev" in *=*) _filter_reject "$script_name" "$cmd" "$tok" ;; esac ;;
      *)  _filter_reject "$script_name" "$cmd" "$tok" ;;
    esac
    prev=""
    shift
  done
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
  # And it has to be a typecheck, not merely a key with that name. `"typecheck":
  # "true"` satisfies a presence test, exits 0, and never invokes a compiler --
  # a workspace containing `const n: number = "not a number"` passed the lane
  # with no type checker installed at all. That is the same defect the presence
  # rule above was written to close, reached by editing the command instead of
  # deleting the key, so presence is not the property worth asserting.
  #
  # An allow-list, like the test-script filter: a command that cannot typecheck
  # is not something to classify, and a checker this does not know about stops
  # the lane until someone says which it is.
  #
  # Delegation counts only when it delegates to something. Accepting any token
  # from a runner list meant `typecheck: "bash -c true"` satisfied the guard --
  # the same no-op the rule was written to reject, one wrapper out. So a
  # delegation has to *name* what it hands to: a script path, or a package
  # script by name. `bash scripts/typecheck.sh` and `npm run typecheck:all`
  # both do; `bash -c true` and `npm --version` do not, and an inline `-c`
  # command is judged on its own contents like any other.
  _tc_cmd="$(script_command typecheck)"
  _tc_ok=0
  # Word-splitting is the point; globbing is not. `tsc -p tsconfig.*.json` would
  # otherwise expand against the workspace and be matched as filenames — the
  # same reason the range splitter in _semver_satisfies disables it.
  set -f
  _script_names_a_checker "$_tc_cmd" && _tc_ok=1
  set +f
  if [ "$_tc_ok" -ne 1 ]; then
    echo "Workspace ${CI_GATE_NODE_WORKSPACE} defines a 'typecheck' script that does not"
    echo "  appear to run a type checker:"
    echo "    typecheck: ${_tc_cmd}"
    echo "  A script named typecheck that exits 0 without invoking a compiler is"
    echo "  the enabled typecheck lane silently switched off by a manifest edit."
    echo "  Recognised: tsc, tsgo, vue-tsc, svelte-check, astro, tsd, attw, or a"
    echo "  runner that delegates to one. Add yours here if it belongs."
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
fi

# A workspace that had a suite must still have one. Deleting every file under
# tests/ leaves nothing to be orphaned, test-layout reports "0 file(s)" quite
# happily, and a successful build carries the gate to exit 0 after the suite has
# disappeared.
#
# Deliberately NOT nested under "and no test script": that made losing the whole
# suite safe as long as the script survived, and `vitest run --passWithNoTests`
# -- documented as exiting 0 when it collects nothing -- is exactly the script
# that gets left behind. The presence of a runner says nothing about whether
# there is anything left for it to run.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1 \
  && git rev-parse --verify HEAD >/dev/null 2>&1; then
  WORKTREE_TESTS="$(find . \( -name 'node_modules' -o -name 'dist' -o -name 'build' \) -prune -o \
    -type f \( -name '*.test.*' -o -name '*.spec.*' \) -print 2>/dev/null | head -1 || true)"
  if [ -z "$WORKTREE_TESTS" ]; then
    HEAD_TESTS="$(git ls-tree -r --name-only HEAD -- . 2>/dev/null \
      | grep -E '\.(test|spec)\.[cm]?[jt]sx?$' | head -5 || true)"
    if [ -n "$HEAD_TESTS" ]; then
      echo "Workspace ${CI_GATE_NODE_WORKSPACE} has lost its entire test suite."
      echo "  HEAD carries test files here and this tree has none, so the lane"
      echo "  would pass having run no tests at all -- a test script left behind"
      echo "  does not change that, because there is nothing for it to run."
      echo "  Present at HEAD, for example:"
      while IFS= read -r _gone; do
        [ -n "$_gone" ] || continue
        echo "    $_gone"
      done <<< "$HEAD_TESTS"
      exit "$CI_RESULT_FAIL_NEW_ISSUE"
    fi
  fi
fi

assert_no_persistent_filter "test"
assert_no_persistent_filter "test:unit"

run_script "format:check"
run_script "lint"
run_script "typecheck"
run_script "test"
run_script "test:unit"
run_script "build"

echo "Node lane passed."
exit "$CI_RESULT_PASS"
